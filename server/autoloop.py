"""Autoloop: the scheduled trading cycle + the kill-switch halt endpoints.

# ai-note: split declined again — this pass stayed inside the futures discovery/refill block (AUTO_FUTURES_*, _screen_with_timeout, _preflight_candidates*, _select_cycle_candidates, _futures_discovery_limit, _refill_futures_candidates). That block is now self-contained and is the seam to extract into its own module once futures orchestration needs independent changes; the cycle/scheduler/endpoint concerns above and below it do not interact with it.

One concern: run the engine's own once-a-day cycle on a timer (plus run-now)
and stop everything on demand. The cycle mirrors server/daily.py's auto scan
(screen a universe, run the LLM pipeline on the top candidates) but resolves
every plan through the engine's own confirm policy — ``_handle_plan`` from
``tradingagents/execution/daily.py``: auto-confirm DRY only when the config
``exec_auto_confirm`` literal AND the ``TRADINGAGENTS_EXEC_AUTO_CONFIRM`` env
flag read true at call time, LIVE additionally requires the FR5 double gate.
This cycle NEVER executes outside that policy, and a plan it does not
execute is only audited (the approval queue belongs to the daily-job
surface). Enabling the loop through the API is fail-closed behind the same
config+env gates: ``409 gate_closed`` — the UI can never arm an unattended
loop the engine's policy itself would refuse.

The kill switch is deliberately ungated: ``POST /api/halt`` appends a
``{"event": "halt"}`` line to the shared execution audit log, which the
RiskGuard of EVERY process replays — the rest of today is refused everywhere
until a reset or the UTC day rolls over (risk.py:163-173). Disabling the
loop is likewise never gated: turning a thing off is the safe direction.

State: ``enabled``/``interval_minutes`` persist in PostgreSQL (``control_settings``,
see ``server/control_store.py``) and the scheduler re-reads them every wake, so
edits need no restart; ``last_cycle`` is an in-memory summary of the most recent
cycle only.
"""

from __future__ import annotations

import inspect
import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from server import (
    audit as audit_log,
    control_store,
    daily as daily_mod,
    job_store,
    runs as runs_mod,
)
from server.autoloop_telemetry import CycleTelemetry
from server.contract import ApiError, json_body, utc_now_iso
from server.futures_cycle_telemetry import (
    claim_cycle as claim_futures_cycle,
    cycle_identity as futures_cycle_identity,
    decision_telemetry as futures_decision_telemetry,
    effective_caps as futures_effective_caps,
    persist_progress as persist_futures_cycle_progress,
    persist_rejection as persist_futures_cycle_rejection,
    redact_payload as redact_futures_payload,
    redact_proposal as redact_futures_proposal,
    snapshot_payload as futures_snapshot_payload,
)
from server.futures_decision_context import (
    build_futures_portfolio,
    render_futures_decision_context,
)
from tradingagents.agents.rating import RATINGS_5_TIER, parse_rating
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution.audit import append_event
from tradingagents.execution.daily import DailyResult, _handle_plan
from tradingagents.execution.futures_account import read_futures_account
from tradingagents.execution.futures_scheduler import closed_candle_pair
from tradingagents.execution.futures_signals import FuturesSignalEnvelope

router = APIRouter(prefix="/api", tags=["autoloop"])

# Auto-cycle tuning: how many universe entries to pull per market and how
# many screened candidates graduate into the expensive LLM pipeline.
AUTO_UNIVERSE_PER_ASSET = 30
# One candidate costs 8-10 sequential LLM calls. Eight of them overran the
# 60-minute cycle budget in every observed run, so the cycle timed out with the
# last candidates never analysed. Three leaves headroom for the retries a flaky
# relay needs and still fills a cycle.
AUTO_TOP_N = 3
# Screen a wider pool than we run: preflight drops stocks (advisory-only under
# futures execution) and symbols with no live contract, and a pool equal to
# AUTO_TOP_N would leave nothing to run after those drops.
AUTO_SCREEN_POOL = AUTO_TOP_N * 4
# Futures must inspect the full discovery pool before selecting the executable
# subset. Otherwise high-scoring stocks or spot-only crypto can consume the
# screen's top-N slots and hide lower-ranked contracts.
AUTO_FUTURES_SCREEN_POOL = AUTO_UNIVERSE_PER_ASSET * 2
# When the broad pool still cannot fill the cycle after preflight drops
# contractless pairs, widen discovery instead of running short. Each refill
# round doubles the inspected universe; the cap keeps one cycle from turning
# into a full-market sweep, and the round cap bounds vendor cost.
AUTO_FUTURES_REFILL_ROUNDS = 2
AUTO_FUTURES_MAX_UNIVERSE = AUTO_UNIVERSE_PER_ASSET * 8
SCREENING_TIMEOUT_SECONDS = 180.0
# 3 candidates × ~10 min sequential LLM work through a slow relay fits inside
# one hour; the scheduler skips interval ticks while the lock is held, so a
# generous budget only delays the next cycle, it never overlaps one.
CYCLE_TIMEOUT_SECONDS = 3600.0
# A refill round is optional work, so it may spend at most this share of the
# cycle budget that remains. The rest is kept for running the candidates the
# earlier rounds already vetted; without the cap a widening round could eat the
# remainder and the previous post-refill deadline check would then discard a
# full selection instead of running it.
AUTO_REFILL_BUDGET_SHARE = 0.5

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 1440

# Hard system ceiling on the leverage an auto cycle will accept from the
# agents' FuturesExecutionProposal, regardless of the configured risk cap. The
# RiskGuard's own ``risk_max_derivatives_leverage`` stays the authoritative gate
# (it can still refuse below this); this constant only bounds the proposal the
# cycle is willing to execute, and an over-cap proposal is refused rather than
# clamped because a resized order would no longer be the decision made.
MAX_AUTO_LEVERAGE = 3.0

# Analyst sets per asset type, exactly like server/daily.py _analyze.
_ANALYSTS_CRYPTO = ("market", "social", "news")
_ANALYSTS_STOCK = ("market", "social", "news", "fundamentals")

logger = logging.getLogger(__name__)

# The 5-tier scale, unpacked like sizing.py/derivatives.py so the short-side
# mapping can never drift from the rating vocabulary.
_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER

# One cycle at a time. The lock is held for the whole cycle; a concurrent
# spawn (scheduler tick + run-now) loses the acquire and is a silent no-op.
_cycle_lock = threading.Lock()
_execution_lock = threading.Lock()
_screening_worker_lock = threading.Lock()
_cycle_thread: threading.Thread | None = None  # last spawned cycle worker
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()
_wakeup = threading.Event()  # set on every state write so the loop re-reads at once
_last_cycle: dict | None = None
_last_telemetry: CycleTelemetry | None = None
_last_cycle_lock = threading.Lock()


def _cycle_timeout(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(f"cycle timed out during {stage} after {CYCLE_TIMEOUT_SECONDS:.0f}s")


def _execution_allowed(deadline: float | None, cancelled: threading.Event | None) -> bool:
    return (
        (deadline is None or time.monotonic() < deadline)
        and (cancelled is None or not cancelled.is_set())
    )


def _cycle_writes_allowed(
    deadline: float | None, cancelled: threading.Event | None
) -> bool:
    """Whether a candidate may still persist rows for its cycle.

    ``_run_candidate_with_timeout`` abandons the worker on timeout: it sets
    ``cancelled`` and returns, but the thread keeps running until its blocking
    call (an LLM graph, a venue read) finishes. Every write after that point
    would land in a cycle that already closed -- a stale funnel row, and in
    tests a row for a schema that has since been dropped or replaced. Writes
    are therefore gated by the same predicate as order submission.
    """
    return _execution_allowed(deadline, cancelled)


def _raise_if_execution_closed(
    deadline: float | None, cancelled: threading.Event | None, stage: str
) -> None:
    if not _execution_allowed(deadline, cancelled):
        raise TimeoutError(f"cycle execution closed during {stage}")


def _execute_with_deadline(
    execute: Callable[[], Any],
    deadline: float | None,
    cancelled: threading.Event | None,
    execution_lock: threading.Lock | None,
) -> Any:
    if execution_lock is None:
        _raise_if_execution_closed(deadline, cancelled, "order submission")
        return execute()
    with execution_lock:
        _raise_if_execution_closed(deadline, cancelled, "order submission")
        return execute()


def _call_crypto_universe(
    crypto_universe: Callable, config: dict, limit: int, futures_mode: bool,
) -> list[str]:
    """Call the universe seam, opting into the futures-only filter when armed.

    ``crypto_universe`` is injected by tests and by the daily spot path; only
    the autoloop's futures mode wants contract-bearing bases, so the flag is
    passed only when the callable declares it. A seam that predates the flag
    keeps its two-argument shape (same pattern as ``cancel_event`` below).
    """
    if not futures_mode:
        return crypto_universe(config, limit)
    try:
        parameters = inspect.signature(crypto_universe).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_flag = "futures_only" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_flag:
        return crypto_universe(config, limit, futures_only=True)
    return crypto_universe(config, limit)


def _screen_with_timeout(
    crypto_universe: Callable,
    stock_universe: Callable,
    screen_universe: Callable,
    config: dict,
    trade_date: str,
    *,
    telemetry: CycleTelemetry | None = None,
    universe_limit: int | None = None,
    screen_pool: int | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Run external screening off-cycle so a vendor stall cannot block forever.

    ``universe_limit``/``screen_pool`` widen the inspected pool for a discovery
    refill round; ``None`` keeps the mode default (the full futures pool in
    futures mode, ``AUTO_SCREEN_POOL`` in spot mode). ``timeout_seconds`` caps
    this call so a refill round can never outlive the cycle's own budget.
    """
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    telemetry = telemetry or CycleTelemetry()
    cancel_screening = threading.Event()
    if not _screening_worker_lock.acquire(blocking=False):
        raise TimeoutError("screening worker from a timed-out cycle is still stopping")

    def worker() -> None:
        try:
            futures_mode = config.get("exec_derivatives") is True
            crypto_limit = (
                universe_limit
                if universe_limit is not None
                else (AUTO_FUTURES_SCREEN_POOL if futures_mode else AUTO_UNIVERSE_PER_ASSET)
            )
            with telemetry.span("screening:crypto_universe"):
                crypto = _call_crypto_universe(crypto_universe, config, crypto_limit, futures_mode)
            if cancel_screening.is_set():
                return
            if futures_mode:
                # Futures candidates must come from the crypto universe only.
                stocks = []
            else:
                with telemetry.span("screening:stock_universe"):
                    stocks = stock_universe(config, AUTO_UNIVERSE_PER_ASSET)
            if cancel_screening.is_set():
                return
            with telemetry.span("screening:metrics"):
                pool_limit = (
                    screen_pool
                    if screen_pool is not None
                    else (
                        AUTO_FUTURES_SCREEN_POOL
                        if futures_mode
                        else AUTO_SCREEN_POOL
                    )
                )
                try:
                    screen_parameters = inspect.signature(screen_universe).parameters
                except (TypeError, ValueError):
                    screen_parameters = {}
                accepts_cancel_event = "cancel_event" in screen_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in screen_parameters.values()
                )
                if accepts_cancel_event:
                    candidates = screen_universe(
                        crypto, stocks, trade_date, pool_limit,
                        cancel_event=cancel_screening,
                    )
                else:
                    candidates = screen_universe(crypto, stocks, trade_date, pool_limit)
            if not cancel_screening.is_set():
                result_queue.put((crypto, stocks, candidates))
        except BaseException as exc:  # hand exception back to cycle thread
            if not cancel_screening.is_set():
                result_queue.put(exc)
        finally:
            _screening_worker_lock.release()

    threading.Thread(target=worker, name="server-autoloop-screen", daemon=True).start()
    budget = SCREENING_TIMEOUT_SECONDS if timeout_seconds is None else min(
        SCREENING_TIMEOUT_SECONDS, max(0.01, timeout_seconds)
    )
    try:
        outcome = result_queue.get(timeout=budget)
    except queue.Empty as exc:
        cancel_screening.set()
        telemetry.close()
        snapshot = telemetry.snapshot()
        stage = snapshot["active_stage"] or snapshot["last_stage"] or "screening"
        raise TimeoutError(
            f"screening timed out during {stage} after {budget:.0f}s"
        ) from exc
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


# --- gates ------------------------------------------------------------------


def _env_flag(name: str) -> bool:
    """Env gate read at call time, fail-closed.

    The exact read the engine's own policy does for both halves
    (execution/daily.py:67-76, bridge.py:592-603), so the UI's gate display
    can never disagree with what ``_handle_plan`` will decide.
    """
    raw = os.environ.get(name)
    if not raw:
        return False
    return raw.strip().lower() in _BOOL_TRUE


def gates() -> dict:
    """The confirm-policy gate state as GET /api/autoloop reports it.

    ``all_open`` (the enable precondition) is only the two auto-confirm
    halves: config literal True AND env true — matching ``_handle_plan``'s
    first two checks. The FR5 live halves and the FR-D derivatives halves are
    reported for observability but never unlock this API surface: live and
    futures remain the engine policy's decision.
    """
    config = get_config()
    auto_confirm_config = config.get("exec_auto_confirm") is True
    auto_confirm_env = _env_flag("TRADINGAGENTS_EXEC_AUTO_CONFIRM")
    return {
        "auto_confirm_config": auto_confirm_config,
        "auto_confirm_env": auto_confirm_env,
        "exec_live_config": config.get("exec_live") is True,
        "exec_live_env": _env_flag("TRADINGAGENTS_EXEC_LIVE"),
        "exec_derivatives_config": config.get("exec_derivatives") is True,
        "exec_derivatives_env": _env_flag("TRADINGAGENTS_EXEC_DERIVATIVES"),
        "all_open": auto_confirm_config and auto_confirm_env,
    }


# --- state file ---------------------------------------------------------------


def _load_state() -> dict:
    """The persisted {enabled, interval_minutes, updated_at}; defaults when the
    record is absent — a fresh install has the loop OFF, not an error."""
    try:
        data = control_store.load_autoloop_state()
    except Exception:  # an unreadable store must not arm the loop
        logger.warning("autoloop state unreadable; treating as disabled", exc_info=True)
        return {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}
    if not isinstance(data, dict) or not data:
        return {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}
    return data


def _save_state(state: dict) -> None:
    control_store.save_autoloop_state(state)


def _interval_from(state: dict) -> int:
    raw = state.get("interval_minutes")
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not MIN_INTERVAL_MINUTES <= raw <= MAX_INTERVAL_MINUTES
    ):
        return DEFAULT_INTERVAL_MINUTES
    return raw


def _set_last_cycle(summary: dict, telemetry: CycleTelemetry | None = None) -> None:
    global _last_cycle, _last_telemetry
    with _last_cycle_lock:
        _last_cycle = summary
        _last_telemetry = telemetry


def _last_cycle_snapshot() -> dict | None:
    with _last_cycle_lock:
        cycle = dict(_last_cycle) if _last_cycle is not None else None
        telemetry = _last_telemetry
    if cycle is not None and telemetry is not None and "telemetry" in cycle:
        cycle["telemetry"] = telemetry.snapshot()
    return cycle


# --- scheduler + cycle --------------------------------------------------------


def ensure_started() -> None:
    """Start the scheduler daemon once; idempotent — every enable calls it."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="server-autoloop-scheduler", daemon=True
    )
    _scheduler_thread.start()


def stop() -> None:
    """Stop the scheduler during application shutdown. Never raises: a stuck
    scheduler is daemon-owned and dies with the process, and shutdown must
    always reach the monitors that follow."""
    global _scheduler_thread
    _scheduler_stop.set()
    _wakeup.set()
    if _scheduler_thread is not None:
        _scheduler_thread.join(timeout=1.0)
        if _scheduler_thread.is_alive():
            logger.warning("autoloop scheduler did not stop within 1s; leaving it to daemon exit")
        else:
            _scheduler_thread = None


def _scheduler_loop() -> None:
    """Run an enabled loop immediately, then wait between later cycles.

    First cycle runs at startup if enabled; subsequent cycles wait the full
    interval before running. The wakeup event allows immediate re-reads when
    settings change but does not bypass the interval between actual cycles.
    A stop request never launches a new cycle, even one already waited on.
    """
    processed_update = object()
    while not _scheduler_stop.is_set():
        state = _load_state()
        updated_at = state.get("updated_at")
        if updated_at != processed_update:
            processed_update = updated_at
            if state.get("enabled") and not _scheduler_stop.is_set():
                _spawn_cycle()
        timed_out = not _wakeup.wait(timeout=_interval_from(state) * 60.0)
        _wakeup.clear()
        if timed_out and not _scheduler_stop.is_set():
            latest = _load_state()
            latest_update = latest.get("updated_at")
            if latest_update != processed_update:
                processed_update = latest_update
                if latest.get("enabled"):
                    _spawn_cycle()
            elif latest.get("enabled"):
                _spawn_cycle()


def _spawn_cycle(graph_builder: Callable | None = None) -> str:
    """Run one cycle on a worker thread unless one is already running."""
    global _cycle_thread
    if _cycle_lock.locked():
        return "already_running"
    thread = threading.Thread(
        target=_run_cycle_locked, args=(graph_builder,), name="server-autoloop-cycle",
        daemon=True,
    )
    _cycle_thread = thread
    thread.start()
    return "started"


# --- candidate preflight ----------------------------------------------------


def _preflight_candidates(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    telemetry: CycleTelemetry | None = None,
) -> tuple[list[dict], list[dict]]:
    runnable, skipped, _metadata_available = _preflight_candidates_with_metadata(
        candidates, derivatives, telemetry=telemetry
    )
    return runnable, skipped


def _preflight_candidates_with_metadata(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    telemetry: CycleTelemetry | None = None,
    rejections_recorded: set[str] | None = None,
) -> tuple[list[dict], list[dict], bool | None]:
    """Drop candidates that can never become an order, before the LLM pipeline.

    In futures mode two kinds of screened candidate are guaranteed to end with
    no order: stocks (the venue bridge only plans crypto) and symbols with no
    live USDT-settled contract. Each one still costs a full 8-10 minute LLM
    pipeline, which is how a cycle used to time out with its executable
    candidates never analysed.

    Returns ``(runnable, skipped, market_metadata_available)``. ``skipped``
    entries are ``{"ticker", "reason"}`` for the cycle summary and audit. A
    metadata failure fails open for crypto only: the per-candidate path
    re-checks and rejects it on its own, while stocks never enter the futures
    path.

    ``rejections_recorded`` carries tickers a previous discovery round already
    persisted a rejection for. A refill round re-screens the same pool, so it
    must report them in ``skipped`` without writing the same cycle row again;
    the set is extended with the tickers this call records.
    """
    if derivatives is None:
        return candidates, [], None  # spot mode: stocks still carry advisory value

    bridge = getattr(derivatives, "_bridge", None)
    if bridge is None:
        skipped = []
        for candidate in candidates:
            asset_type = candidate.get("asset_type")
            if asset_type == "crypto":
                continue
            reason = (
                "advisory only in futures mode — the venue bridge cannot plan a stock order"
                if asset_type == "stock"
                else f"unsupported asset_type in futures mode: {asset_type!r}"
            )
            skipped.append({"ticker": str(candidate.get("ticker")), "reason": reason})
        return [candidate for candidate in candidates if candidate.get("asset_type") == "crypto"], skipped, None

    telemetry = telemetry or CycleTelemetry()
    markets: dict[str, Any] | None
    try:
        with telemetry.span("preflight:futures_markets"):
            markets = bridge.client().load_markets()
    except Exception as exc:  # venue unreachable: leave the decision to the candidate path
        logger.warning("autoloop preflight: futures markets unavailable (%s)", exc)
        markets = None

    runnable: list[dict] = []
    skipped: list[dict] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker"))
        asset_type = candidate.get("asset_type")
        if asset_type != "crypto":
            reason = (
                "advisory only in futures mode — the venue bridge cannot plan a stock order"
                if asset_type == "stock"
                else f"unsupported asset_type in futures mode: {asset_type!r}"
            )
            skipped.append({"ticker": ticker, "reason": reason})
            continue
        if markets is None:
            # Keep crypto candidates for the per-candidate resolver/context path,
            # but never let an unavailable futures venue route a stock into the
            # derivatives pipeline.
            runnable.append(candidate)
            continue
        try:
            bridge.futures_symbol_for(ticker, markets=markets)
        except ValueError as exc:
            reason = f"no active futures contract: {exc}"
            skipped.append({"ticker": ticker, "reason": reason})
            if rejections_recorded is None or ticker not in rejections_recorded:
                _record_futures_cycle_rejection(
                    ticker,
                    market_rejection_reason=str(exc),
                    phase="market",
                    telemetry={"execution_status": "not_started"},
                )
                if rejections_recorded is not None:
                    rejections_recorded.add(ticker)
            continue
        runnable.append(candidate)
    return runnable, skipped, markets is not None


def _select_cycle_candidates(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    limit: int,
    telemetry: CycleTelemetry | None = None,
    rejections_recorded: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict | None]:
    runnable, skipped, metadata_available = _preflight_candidates_with_metadata(
        candidates, derivatives, telemetry=telemetry,
        rejections_recorded=rejections_recorded,
    )
    runnable.sort(key=lambda entry: str(entry.get("asset_type") or "crypto") != "crypto")
    selected = runnable[:limit]
    if derivatives is None:
        return selected, skipped, None
    return selected, skipped, {
        "mode": "futures",
        "requested": limit,
        "screened": len(candidates),
        "executable": len(runnable) if metadata_available else None,
        "selected": len(selected),
        "shortfall": max(0, limit - len(runnable)) if metadata_available else None,
        "pool_exhausted": len(runnable) < limit if metadata_available else None,
        "market_metadata_available": metadata_available,
    }


def _futures_discovery_limit(round_index: int) -> int:
    """Universe size inspected on discovery round ``round_index`` (0 = initial).

    Each refill doubles the inspected pool so lower-ranked contracts can
    surface, capped so one cycle never becomes a full-market sweep.
    """
    widened = AUTO_FUTURES_SCREEN_POOL * (2 ** max(0, round_index))
    return min(widened, AUTO_FUTURES_MAX_UNIVERSE)


def _refill_futures_candidates(
    crypto_universe: Callable,
    stock_universe: Callable,
    screen_universe: Callable,
    config: dict,
    trade_date: str,
    derivatives: Any | None,
    *,
    limit: int,
    selected: list[dict],
    skipped: list[dict],
    selection: dict | None,
    crypto: list[str],
    inspected: int,
    deadline: float | None = None,
    telemetry: CycleTelemetry | None = None,
) -> tuple[list[str], list[str], list[dict], list[dict], dict | None]:
    """Widen discovery until ``limit`` executable futures exist, or the pool ends.

    The first screen inspects one broad pool, and its top can be all
    contractless pairs, so preflight leaves the cycle short while the LLM
    budget goes unspent. Each refill round re-discovers a wider crypto
    universe and re-screens it, making contracts ranked below the first cut
    reachable. Screening keeps each ticker's own ``asset_type`` and futures
    mode never queries the stock universe, so no stock can enter the futures
    path.

    Rounds MERGE into the already-vetted set and never replace it.
    ``crypto_universe`` refetches the venue on every call (screening.py:138-167),
    so a wider round is a fresh snapshot rather than a superset; replacing would
    drop a contract that already passed preflight and misreport the shortfall.

    Stops when the target is met, when the venue metadata is unavailable (the
    per-candidate path must then decide), when discovery returned fewer
    tickers than requested (the pool is genuinely exhausted), at the round
    cap, or when the remaining cycle budget is spent. The remaining shortfall
    stays in ``selection`` as ``shortfall``/``pool_exhausted``.

    Returns ``(crypto, stocks, selected, skipped, selection)`` so the caller
    reports the discovery that actually produced the cycle's candidates.

    The refill is an optimisation layered on top of a working selection: a
    failed round is reported in ``selection["refill_error"]`` and keeps every
    candidate the earlier rounds already vetted, and each round is capped by the
    remaining cycle budget so it can never outlive the run.
    """
    if selection is None:
        return crypto, [], selected, skipped, selection

    rounds = 0
    requested = inspected
    discovered = len(crypto)
    # Distinct tickers discovery returned across every round: the union is what
    # actually got inspected, so the summary never under-reports it. A ticker can
    # appear in one round's snapshot and vanish from the next (screening.py:138-167).
    inspected_tickers: set[str] = set(crypto)
    stocks: list[str] = []
    vetted: list[dict] = list(selected)
    seen_vetted = {str(entry.get("ticker")) for entry in vetted}
    seen_skipped = {str(entry.get("ticker")) for entry in skipped}
    # The first round already persisted a rejection for every ticker it
    # dropped; a refill re-screens the same pool, so those rows must not be
    # written a second time for the same cycle.
    rejections_recorded = set(seen_skipped)
    metadata_available = selection.get("market_metadata_available")
    screened_total = int(selection.get("screened") or 0)
    refill_error: str | None = None
    while len(vetted) < limit:
        if metadata_available is not True:
            break  # cannot tell contractless from unreachable: fail open
        if discovered < requested:
            break  # discovery offered no wider pool than the one just inspected
        if rounds >= AUTO_FUTURES_REFILL_ROUNDS:
            break
        widened = _futures_discovery_limit(rounds + 1)
        if widened <= requested:
            break  # widening cap reached
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None:
            # A round is optional work: it may spend at most a share of what is
            # left, so the candidates already vetted keep a budget to run in.
            # Without this a round could eat the remainder and the post-refill
            # deadline check would discard a full selection.
            remaining *= AUTO_REFILL_BUDGET_SHARE
            if remaining <= 0:
                break
        try:
            round_crypto, stocks, candidates = _screen_with_timeout(
                crypto_universe,
                stock_universe,
                screen_universe,
                config,
                trade_date,
                telemetry=telemetry,
                universe_limit=widened,
                screen_pool=widened,
                timeout_seconds=remaining,
            )
            round_vetted, round_skipped, metadata_available = (
                _preflight_candidates_with_metadata(
                    candidates, derivatives, telemetry=telemetry,
                    rejections_recorded=rejections_recorded,
                )
            )
        except Exception as exc:
            # A refill round is optional: report it and keep what round 1
            # vetted instead of failing the whole cycle (or discarding it).
            logger.warning("autoloop refill round %d failed (%s)", rounds + 1, exc)
            refill_error = f"{type(exc).__name__}: {exc}"
            break
        rounds += 1
        crypto = round_crypto
        requested = widened
        discovered = len(crypto)
        inspected_tickers.update(crypto)
        screened_total += len(candidates)
        for entry in round_vetted:
            ticker = str(entry.get("ticker"))
            if ticker not in seen_vetted:
                seen_vetted.add(ticker)
                vetted.append(entry)
        for entry in round_skipped:
            ticker = str(entry.get("ticker"))
            if ticker not in seen_skipped:
                seen_skipped.add(ticker)
                skipped.append(entry)
    selected = vetted[:limit]
    selection.update({
        "screened": screened_total,
        "executable": len(vetted) if metadata_available else None,
        "selected": len(selected),
        "shortfall": max(0, limit - len(vetted)) if metadata_available else None,
        "pool_exhausted": len(vetted) < limit if metadata_available else None,
        "market_metadata_available": metadata_available,
        "refill_rounds": rounds,
        "discovery_universe": requested,
        "discovery_pool": discovered,
        "discovery_inspected": len(inspected_tickers),
    })
    if refill_error is not None:
        selection["refill_error"] = refill_error
    return crypto, stocks, selected, skipped, selection


def _run_cycle_locked(graph_builder: Callable | None = None) -> None:
    if not _cycle_lock.acquire(blocking=False):
        return  # a concurrent spawn won: it runs, this one is a no-op
    try:
        _run_cycle(graph_builder)
    finally:
        _cycle_lock.release()


def _run_candidate_with_timeout(
    bridge: Any,
    candidate: dict,
    config: dict,
    build_graph: Callable,
    derivatives: Any | None,
    deadline: float,
    cancelled: threading.Event,
    execution_lock: threading.Lock,
    telemetry: CycleTelemetry,
) -> DailyResult:
    """Run one candidate off-cycle so an LLM stall cannot hold the lock."""
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(_process_candidate(
                bridge, candidate, config, build_graph, derivatives,
                deadline=deadline, cancelled=cancelled,
                execution_lock=execution_lock, telemetry=telemetry,
            ))
        except BaseException as exc:
            result_queue.put(exc)

    threading.Thread(target=worker, name="server-autoloop-candidate", daemon=True).start()
    remaining = max(0.0, deadline - time.monotonic())
    try:
        outcome = result_queue.get(timeout=remaining)
    except queue.Empty as exc:
        cancelled.set()
        telemetry.close()
        snapshot = telemetry.snapshot()
        stage = snapshot["active_stage"] or snapshot["last_stage"] or "candidate_start"
        node = snapshot.get("active_node")
        node_context = f" in node {node}" if node and stage != f"graph_node:{node}" else ""
        with execution_lock:
            pass
        raise TimeoutError(
            f"cycle timed out during candidate {candidate.get('ticker')} "
            f"stage {stage}{node_context} after {CYCLE_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _run_cycle(graph_builder: Callable | None = None) -> list[DailyResult]:
    """One auto cycle: screen the universe, run the pipeline per candidate,
    hand every plan to the engine's confirm policy. Returns one DailyResult
    per candidate. A cycle requested while a run/job is active is skipped and
    audited (set_config is process-wide — trading_graph.py:65 — so the cycle
    must not fight another worker over config); a candidate's failure never
    stops the cycle (execution/daily.py:170-173)."""
    started_at = utc_now_iso()
    deadline = time.monotonic() + CYCLE_TIMEOUT_SECONDS
    build_graph = graph_builder if graph_builder is not None else _default_graph_builder

    busy_run = runs_mod.active_run_id()
    busy_job = None if busy_run is not None else daily_mod.active_job_id()
    if busy_run is not None or busy_job is not None:
        occupied = f"run {busy_run}" if busy_run is not None else f"daily job {busy_job}"
        audit_log.record("autoloop_cycle_skipped", reason=f"another worker is active: {occupied}")
        _set_last_cycle({
            "started_at": started_at, "finished_at": utc_now_iso(),
            "status": "skipped_busy", "reason": occupied,
            "candidates": [], "results": [], "error": None,
        })
        return []

    audit_log.record("autoloop_cycle_started")
    config = get_config()  # snapshot: later settings changes do not touch this cycle
    summary: dict = {
        "started_at": started_at, "finished_at": None, "status": "running",
        "candidates": [], "results": [], "error": None,
    }

    def publish_telemetry(snapshot: dict) -> None:
        summary["telemetry"] = snapshot
        _set_last_cycle(dict(summary, results=list(summary["results"])), telemetry)

    telemetry = CycleTelemetry(on_change=publish_telemetry)
    publish_telemetry(telemetry.snapshot())
    results: list[DailyResult] = []
    cancelled = threading.Event()
    try:
        from tradingagents.dataflows.screening import (
            crypto_universe,
            screen_universe,
            stock_universe,
        )

        with telemetry.span("screening"):
            crypto, stocks, candidates = _screen_with_timeout(
                crypto_universe, stock_universe, screen_universe, config, _today_iso(),
                telemetry=telemetry,
            )
            _cycle_timeout(deadline, "screening")
        _cycle_timeout(deadline, "venue setup")
        with telemetry.span("venue_setup"):
            bridge = _build_bridge(config)
            derivatives = _build_derivatives_executor(config) if config.get("exec_derivatives") is True else None

        # Drop candidates that cannot end in an order before spending LLM time
        # on them (futures mode: stocks and symbols without a live contract).
        _cycle_timeout(deadline, "candidate preflight")
        candidates, skipped, candidate_selection = _select_cycle_candidates(
            candidates,
            derivatives,
            limit=AUTO_TOP_N,
            telemetry=telemetry,
        )
        if derivatives is not None:
            # The broad pool's top can be all contractless pairs. Widen
            # discovery until the cycle is full so lower-ranked contracts are
            # reachable, and report whatever remains short instead of silently
            # running fewer candidates. Spot/daily selection is untouched.
            # A failed round is reported inside ``candidate_selection`` and
            # keeps the vetted candidates, so no timeout is re-raised here: the
            # refill reserves the candidates' budget itself.
            with telemetry.span("screening:refill"):
                crypto, stocks, candidates, skipped, candidate_selection = (
                    _refill_futures_candidates(
                        crypto_universe,
                        stock_universe,
                        screen_universe,
                        config,
                        _today_iso(),
                        derivatives,
                        limit=AUTO_TOP_N,
                        selected=candidates,
                        skipped=skipped,
                        selection=candidate_selection,
                        crypto=crypto,
                        inspected=AUTO_FUTURES_SCREEN_POOL,
                        deadline=deadline,
                        telemetry=telemetry,
                    )
                )
        summary["universe"] = {"crypto": len(crypto), "stock": len(stocks)}
        if candidate_selection is not None and candidate_selection.get("discovery_inspected"):
            # The cycle inspected the union of every round, not just the last
            # snapshot (screening.py:138-167 refetches the venue per call), so
            # reporting the last round's count would under-state the discovery.
            summary["universe"]["crypto"] = int(candidate_selection["discovery_inspected"])
        if skipped:
            summary["skipped"] = skipped
            audit_log.record("autoloop_candidates_skipped", skipped=skipped)
        if candidate_selection is not None:
            summary["candidate_selection"] = candidate_selection

        summary["candidates"] = [str(entry["ticker"]) for entry in candidates]
        for candidate in candidates:
            _cycle_timeout(deadline, f"candidate {candidate.get('ticker')}")
            result = _run_candidate_with_timeout(
                bridge, candidate, config, build_graph, derivatives, deadline, cancelled,
                _execution_lock, telemetry,
            )
            results.append(result)
            summary["results"].append(_result_summary(result))
        summary["status"] = "completed"
    except TimeoutError as exc:
        cancelled.set()
        telemetry.close()
        summary["status"] = "timeout"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["telemetry"] = telemetry.snapshot()
        audit_log.record("autoloop_cycle_timeout", error=summary["error"], telemetry=summary["telemetry"])
    except Exception as exc:
        # outside the candidate loop: the screen or the bridge itself failed.
        # The scheduler thread must survive to try the next interval.
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        audit_log.record("autoloop_cycle_failed", error=summary["error"])
    telemetry.close()
    summary["telemetry"] = telemetry.snapshot()
    summary["finished_at"] = utc_now_iso()
    _set_last_cycle(summary)
    if summary["status"] == "completed":
        audit_log.record("autoloop_cycle_completed", candidates=summary["candidates"],
                         results=summary["results"])
    return results


def _process_candidate(
    bridge: Any, candidate: dict, config: dict, build_graph: Callable,
    derivatives: Any | None = None, *, deadline: float | None = None,
    cancelled: threading.Event | None = None,
    execution_lock: threading.Lock | None = None,
    telemetry: CycleTelemetry | None = None,
) -> DailyResult:
    """Analyze → plan → the engine's confirm policy for one candidate.

    Stock candidates are advisory: analysis runs, but the ccxt bridge cannot
    plan for them (bridge.py:174-186), so the signal is the deliverable.
    Crypto candidates go through ``DerivativesExecutor`` when the derivatives
    executor is supplied (the cycle arms it when ``exec_derivatives`` is a
    literal True in config), else through the spot bridge. Either way plans
    are NEVER resolved here beyond ``_handle_plan`` — the 3-gate auto-confirm
    policy alone decides execute-vs-wait, and what it does not execute is
    only audited.
    """
    ticker = str(candidate["ticker"])
    asset_type = str(candidate.get("asset_type") or "crypto")
    result = DailyResult(ticker=ticker)
    telemetry = telemetry or CycleTelemetry()
    futures_cycle_id: str | None = None
    futures_cycle_payload: dict = {}
    futures_cycle_timestamps: dict[str, str] = {}
    futures_caps: dict[str, float] = {}
    agent_note: str | None = None
    # The executor's risk_guard_decisions list is shared by every candidate in
    # the cycle, so mark where it stands before this candidate can append to
    # it: a row must only ever carry the decisions made FOR this candidate.
    guard_baseline = len(getattr(derivatives, "risk_guard_decisions", None) or [])
    try:
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} start")
        futures_context = None
        futures_account = None
        futures_portfolio = None
        execution_context = ""
        if asset_type == "crypto" and derivatives is not None:
            if deadline is not None:
                _cycle_timeout(deadline, f"candidate {ticker} market context")
            try:
                with telemetry.span("futures_market_context", ticker):
                    futures_context = _closed_futures_context(derivatives, ticker)
            except PermissionError as exc:
                result.reason = f"{type(exc).__name__}: {exc}"
                _record_futures_cycle_rejection(
                    ticker, market_rejection_reason=result.reason,
                    phase="market", telemetry={"execution_status": "not_started"},
                )
                return result
            except ValueError as exc:
                result.reason = f"market_rejected: {exc}"
                _record_futures_cycle_rejection(
                    ticker, market_rejection_reason=str(exc), phase="market",
                )
                return result
            if futures_context is None:
                result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
                _record_futures_cycle_rejection(
                    ticker,
                    confirmation_reason="closed 1h/4h candle pair unavailable",
                    phase="confirmation",
                    telemetry={"requested_ticker": ticker, "unified_symbol": None},
                )
                return result
            futures_cycle_id, four_hour_open, one_hour_open = futures_cycle_identity(futures_context)
            futures_cycle_timestamps = {"market_context_finished_at": utc_now_iso()}
            futures_caps = futures_effective_caps(
                derivatives, auto_cap=MAX_AUTO_LEVERAGE, config=config,
            )
            futures_cycle_payload = {
                "requested_ticker": ticker,
                "unified_symbol": futures_context["unified_symbol"],
                "snapshot": futures_snapshot_payload(futures_context),
                "effective_caps": futures_caps,
                "confirmation_result": None,
                "confirmation_reason": "pending_agent_signal",
                "confirmation": {"result": None, "reason": "pending_agent_signal"},
                "execution_status": "in_progress",
                "phase_timestamps": dict(futures_cycle_timestamps),
            }
            if not claim_futures_cycle(
                futures_cycle_id,
                symbol=futures_context["unified_symbol"],
                four_hour_open=four_hour_open,
                one_hour_open=one_hour_open,
                payload=futures_cycle_payload,
            ):
                result.reason = "duplicate futures signal skipped"
                return result
            persist_futures_cycle_progress(
                futures_cycle_id, phase="market", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["market_context_finished_at"],
            )
            with telemetry.span("futures_account", ticker):
                futures_account = read_futures_account(derivatives._bridge)
            futures_portfolio = build_futures_portfolio(futures_account)
            with telemetry.span("protective_bracket", ticker):
                # Both directions: the agents need the engine's reference levels
                # before they decide, and the entry path must reuse these reads
                # rather than hitting the vendor a second time.
                brackets = {
                    "long": _bracket_prices(ticker, direction="long"),
                    "short": _bracket_prices(ticker, direction="short"),
                }
            with telemetry.span("futures_decision_context", ticker):
                execution_context = render_futures_decision_context(
                    futures_context,
                    futures_account,
                    ticker,
                    config,
                    brackets,
                    max_leverage=futures_caps["effective_leverage"],
                )
            futures_cycle_timestamps["decision_context_finished_at"] = utc_now_iso()
            futures_cycle_payload["phase_timestamps"] = dict(futures_cycle_timestamps)
            futures_cycle_payload["futures_account"] = {
                "equity": futures_account.equity,
                "available_balance": futures_account.available_balance,
                "equity_currency": futures_account.equity_currency,
                "available_balance_currency": futures_account.available_balance_currency,
                "position_count": len(futures_account.positions),
            }
            persist_futures_cycle_progress(
                futures_cycle_id, phase="context", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["decision_context_finished_at"],
            )
        with telemetry.span("graph_build", ticker):
            graph = build_graph(config, _ANALYSTS_CRYPTO if asset_type == "crypto" else _ANALYSTS_STOCK)
        with telemetry.span("graph_analysis", ticker):
            propagate = getattr(graph, "propagate_with_telemetry", graph.propagate)
            kwargs: dict = {"asset_type": asset_type}
            parameters = inspect.signature(propagate).parameters
            if "telemetry" in parameters:
                kwargs["telemetry"] = telemetry
            if execution_context and _accepts_execution_context(propagate):
                kwargs["execution_context"] = execution_context
            state, signal = propagate(ticker, _today_iso(), **kwargs)
            if deadline is not None:
                _cycle_timeout(deadline, f"candidate {ticker} analysis")
        result.signal = signal
        pm_decision = state.get("final_trade_decision") if isinstance(state, dict) else None
        agent_note = (str(pm_decision).strip() or None) if pm_decision else None
        if derivatives is not None and asset_type == "crypto":
            decision_data = futures_decision_telemetry(
                state if isinstance(state, dict) else {}, signal, parse_rating,
            )
            confirmation_ok, confirmation_reason = _confirmation_state(
                futures_context, signal,
            )
            decision_data["confirmation_result"] = confirmation_ok
            decision_data["confirmation_reason"] = confirmation_reason
            decision_data["confirmation"] = {
                "result": confirmation_ok,
                "reason": confirmation_reason,
            }
            futures_cycle_payload.update(decision_data)
            # The PM's verbatim decision travels with every later phase write
            # (success, rejection and failure), so a completed entry keeps the
            # rationale the UI renders (futures-cycle-funnel.tsx AgentDecisionNote).
            if agent_note:
                futures_cycle_payload["agent_decision_note"] = redact_futures_payload(
                    agent_note[:6000]
                )
            futures_cycle_payload["signal_reason"] = (
                "tradeable_signal" if signal in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT)
                else "signal_not_tradeable"
            )
            futures_cycle_payload["phase_timestamps"] = {
                **futures_cycle_timestamps,
                "analysis_finished_at": utc_now_iso(),
            }
            futures_cycle_timestamps = dict(futures_cycle_payload["phase_timestamps"])
            persist_futures_cycle_progress(
                futures_cycle_id, phase="analysis", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["analysis_finished_at"],
            )
        if asset_type != "crypto":
            result.reason = (
                "advisory only — signal from the stock pipeline; the venue "
                "bridge only executes crypto, so no order plan"
            )
            return result
        if derivatives is not None:
            proposal = (
                state.get("futures_execution_proposal")
                if isinstance(state, dict) else None
            )
            intent = (
                state.get("futures_execution_intent")
                if isinstance(state, dict) else None
            )
            if intent is None:
                intent = "structured_terms_unavailable"
            if intent != "enter":
                result.reason = {
                    "abstain": "agent_abstained",
                    "manage_existing": "position_management_only",
                    "structured_terms_unavailable": "structured_terms_unavailable",
                }.get(str(intent), f"invalid_futures_intent: {intent}")
                _record_futures_cycle_rejection(
                    ticker,
                    unified_symbol=futures_context.get("unified_symbol"),
                    signal=signal,
                    signal_reason=result.reason,
                    phase="proposal",
                    snapshot=futures_context,
                    agent_note=agent_note,
                    cycle_id=futures_cycle_id,
                    telemetry={
                        **decision_data,
                        "effective_caps": futures_caps,
                        "proposal_validation": {"valid": False, "reason": result.reason},
                        "execution_status": "not_started",
                    },
                    phase_timestamps=futures_cycle_timestamps,
                )
                return result
            with telemetry.span("futures_planning", ticker):
                _process_futures_candidate(
                    derivatives, ticker, signal, config, result,
                    context=futures_context, proposal=proposal,
                    portfolio=futures_portfolio, brackets=brackets, intent=intent,
                    deadline=deadline, cancelled=cancelled,
                    execution_lock=execution_lock, telemetry=telemetry,
                    agent_note=agent_note,
                    cycle_id=futures_cycle_id,
                    decision_data=decision_data,
                    cycle_payload=futures_cycle_payload,
                    phase_timestamps=futures_cycle_timestamps,
                    guard_baseline=guard_baseline,
                )
            return result
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} planning")
        with telemetry.span("portfolio_sync", ticker):
            portfolio = bridge.sync_portfolio()
        with telemetry.span("planning", ticker):
            plan = bridge.plan_order(ticker, signal, portfolio=portfolio)
        if plan.side == "buy":
            with telemetry.span("protective_bracket", ticker):
                plan = _attach_bracket(plan, ticker)
        result.plan = plan
        with telemetry.span("execution_policy", ticker):
            _handle_plan(
                bridge,
                plan,
                portfolio,
                result,
                execute_guard=lambda execute: _execute_with_deadline(
                    execute, deadline, cancelled, execution_lock
                ),
            )
    except TimeoutError:
        raise
    except Exception as exc:  # one candidate's failure never stops the cycle
        result.reason = f"{type(exc).__name__}: {exc}"
        if futures_cycle_id is not None:
            current_phase = (
                "context" if not futures_cycle_timestamps.get("decision_context_finished_at")
                else "analysis" if "decision_data" not in locals()
                else "execution"
            )
            phase_finished = utc_now_iso()
            risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
            payload = {
                **futures_cycle_payload,
                "failure_reason": result.reason,
                "execution_status": "failed" if current_phase == "execution" else "not_started",
                "risk_guard_decisions": risk_decisions,
                "risk_guard": risk_decisions[-1] if risk_decisions else {
                    "allowed": None, "halted": None, "reason": "not_called",
                },
                "order_id": result.order_id,
                "phase_timestamps": {
                    **futures_cycle_timestamps,
                    f"{current_phase}_failed_at": phase_finished,
                },
            }
            if agent_note and "agent_decision_note" not in payload:
                payload["agent_decision_note"] = redact_futures_payload(agent_note[:6000])
            persist_futures_cycle_progress(
                futures_cycle_id,
                status="failed",
                phase=current_phase,
                payload=payload,
                terminal=True,
            )
    return result


def _closed_futures_context(derivatives: Any, ticker: str) -> dict | None:
    now = datetime.now(UTC)
    one_hour, four_hour = closed_candle_pair(now)
    symbol = derivatives.resolve_market(ticker) if hasattr(derivatives, "resolve_market") else derivatives.symbol_for(ticker)
    exchange = derivatives._bridge.client()
    candles_1h = fetch_closed_candles(exchange, symbol, "1h", as_of=now, limit=2)
    candles_4h = fetch_closed_candles(exchange, symbol, "4h", as_of=now, limit=2)
    if not candles_1h or not candles_4h:
        return None
    candle_1h, candle_4h = candles_1h[-1], candles_4h[-1]
    if (candle_1h.open_time != int(one_hour[0].timestamp() * 1000)
            or candle_4h.open_time != int(four_hour[0].timestamp() * 1000)):
        return None
    return {
        "requested_ticker": ticker,
        "symbol": symbol,
        "unified_symbol": symbol,
        "one_hour": candle_1h,
        "four_hour": candle_4h,
        "one_hour_open": candle_1h.open_time,
        "one_hour_close": candle_1h.close,
        "four_hour_open": candle_4h.open_time,
        "four_hour_close": candle_4h.close,
        "one_hour_direction": _candle_direction(candle_1h),
        "four_hour_direction": _candle_direction(candle_4h),
        **_futures_derivative_metrics(exchange, symbol),
    }


def _futures_derivative_metrics(exchange: Any, symbol: str) -> dict:
    """Funding rate and open interest for the resolved contract.

    The futures agents are asked to justify an entry against funding and
    risk/reward, and without this read they report "funding unavailable" and
    abstain. Each field degrades to None instead of failing the cycle: a venue
    does not serve every metric on every market, and a missing metric must not
    block the analysis that is the deliverable.
    """
    metrics: dict[str, Any] = {
        "funding_rate": None,
        "funding_datetime": None,
        "mark_price": None,
        "open_interest": None,
        "open_interest_value": None,
    }
    try:
        funding = exchange.fetch_funding_rate(symbol)
    except Exception as exc:  # venue may not publish funding for this market
        logger.info("funding rate unavailable for %s (%s)", symbol, exc)
        funding = None
    if isinstance(funding, dict):
        metrics["funding_rate"] = funding.get("fundingRate")
        metrics["funding_datetime"] = funding.get("fundingDatetime")
        metrics["mark_price"] = funding.get("markPrice")
    try:
        interest = exchange.fetch_open_interest(symbol)
    except Exception as exc:  # venue may not publish open interest
        logger.info("open interest unavailable for %s (%s)", symbol, exc)
        interest = None
    if isinstance(interest, dict):
        metrics["open_interest"] = interest.get("openInterestAmount")
        metrics["open_interest_value"] = interest.get("openInterestValue")
    return metrics


def _candle_direction(candle: Any) -> str:
    if candle.close > candle.open:
        return "bullish"
    if candle.close < candle.open:
        return "bearish"
    return "doji"


def _direction_brackets(
    ticker: str, brackets: dict | None = None,
) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    """(long, short) ATR brackets, reusing a caller's reads when supplied."""
    if brackets is not None:
        return brackets["long"], brackets["short"]
    return _bracket_prices(ticker, direction="long"), _bracket_prices(ticker, direction="short")


def _confirmation_state(context: dict, signal: str) -> tuple[bool, str]:
    # Kept strict on replay evidence (2026-10-29, tests/test_futures_confirmation_replay.py):
    # the archived funnel records 86 confirmation rejects, all direction_mismatch,
    # and of the 58 rejects in the newer export only 1 carries the closed 1h/4h
    # pair — so a counterfactual is scored on 77 snapshot rows total
    # (server/futures_replay.py + tests/fixtures/futures_cycles_replay.json).
    # What that replay shows: "4h dominance + bounded 1h pullback" would
    # confirm 3 additional candidate candles (all QNT-USD shorts), but every
    # one of those cycles was refused UPSTREAM at the intent gate (abstain /
    # manage-only / no structured terms, phase=proposal before this check
    # runs) — loosening this gate creates ZERO new entries in the sample. Two
    # of the 3 confirmations are even negative-retrace range-breaks (1h close
    # below the 4h low) that the harness's bound passes and labels a pullback.
    # The 1:1 agreement between recorded reasons and this rule is
    # mirror-consistency, not out-of-sample validation: snapshot rows' payload
    # reasons were derived at persist time by this very function
    # (_record_futures_cycle_rejection). No forward-return data exists for any
    # flip, so nothing here is evidence to loosen a futures entry gate; the
    # conservative side is refusing entry. This rule stays until reject rows
    # routinely carry their candle pair (current code persists the snapshot;
    # the archive predates it) and the sample can be judged on outcomes.
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        return True, "not_required_for_non_tradeable_signal"
    expected = "bearish" if _direction(signal) == "short" else "bullish"
    directions = (context.get("one_hour_direction"), context.get("four_hour_direction"))
    if any(direction == "doji" for direction in directions):
        return False, "doji_candle"
    if directions != (expected, expected):
        return False, "direction_mismatch"
    return True, "aligned_direction"


# Persistence and redaction live in server.futures_cycle_telemetry.
def _candidate_guard_decisions(derivatives: Any, baseline: int) -> list[dict]:
    """Only the RiskGuard decisions appended FOR this candidate.

    ``DerivativesExecutor.risk_guard_decisions`` accumulates across the whole
    cycle (derivatives.py builds one executor per cycle), so copying the list
    verbatim would let one candidate's row claim the guard verdicts of its
    predecessors — including an ``allowed=True`` for a candidate the guard
    never even evaluated.
    """
    decisions = getattr(derivatives, "risk_guard_decisions", None) or []
    return list(decisions[baseline:])


def _record_futures_cycle_rejection(
    ticker: str,
    *,
    unified_symbol: str | None = None,
    market_rejection_reason: str | None = None,
    confirmation_reason: str | None = None,
    snapshot: dict | None = None,
    signal: str | None = None,
    signal_reason: str | None = None,
    phase: str = "market",
    status: str = "rejected",
    agent_note: str | None = None,
    cycle_id: str | None = None,
    telemetry: dict | None = None,
    phase_timestamps: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    one_hour, four_hour = closed_candle_pair(now)
    symbol = unified_symbol or ticker
    cycle_id = cycle_id or f"futures:{symbol}:{four_hour[0].isoformat()}:{one_hour[0].isoformat()}:1"
    payload = {
        "requested_ticker": ticker,
        "unified_symbol": unified_symbol,
        "market_rejection_reason": market_rejection_reason,
        "confirmation_result": None,
        "confirmation_reason": confirmation_reason,
        "confirmation": {"result": None, "reason": confirmation_reason},
        "signal": signal,
        "signal_reason": signal_reason,
    }
    if telemetry:
        payload.update(telemetry)
    if phase_timestamps:
        payload["phase_timestamps"] = dict(phase_timestamps)
    if snapshot and signal:
        confirmed, derived_reason = _confirmation_state(snapshot, signal)
        payload["confirmation_result"] = confirmed
        payload["confirmation_reason"] = confirmation_reason or derived_reason
        payload["confirmation"] = {"result": confirmed, "reason": payload["confirmation_reason"]}
    if agent_note:
        payload["agent_decision_note"] = redact_futures_payload(agent_note[:6000])

    if snapshot:
        payload["snapshot"] = futures_snapshot_payload(snapshot)
    persist_futures_cycle_rejection(
        cycle_id,
        symbol=symbol,
        four_hour_open=(
            datetime.fromtimestamp(snapshot["four_hour_open"] / 1000, UTC).isoformat()
            if snapshot and isinstance(snapshot.get("four_hour_open"), (int, float))
            else four_hour[0].isoformat()
        ),
        one_hour_open=(
            datetime.fromtimestamp(snapshot["one_hour_open"] / 1000, UTC).isoformat()
            if snapshot and isinstance(snapshot.get("one_hour_open"), (int, float))
            else one_hour[0].isoformat()
        ),
        phase=phase,
        payload=payload,
        status=status,
    )


def _snapshot_payload(context: dict) -> dict:
    return futures_snapshot_payload(context)



def _unified_mark_price(bridge: Any, symbol: str) -> float | None:
    """Last price for the already-resolved unified futures symbol.

    Read straight off the venue client: the bridge's fetch_price() routes
    through the SPOT symbol resolver, which rejects off-whitelist bases. The
    futures path already resolved its contract from venue metadata, so it
    must not re-resolve (and silently drop) the mark price.
    """
    row = bridge.client().fetch_ticker(symbol)
    if not isinstance(row, dict):
        return None
    for key in ("last", "close"):
        value = row.get(key)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _accepts_execution_context(propagate: Callable) -> bool:
    """Whether this graph's propagate() accepts the futures decision context.

    Signature inspection instead of sniffing the TypeError message: an
    unrelated TypeError that merely mentions the kwarg name must not
    silently drop the account/market context the agents decide from.
    """
    try:
        return "execution_context" in inspect.signature(propagate).parameters
    except (TypeError, ValueError):
        return False


def _proposal_terms(
    proposal: Any, *, mark: float, direction: str, config: dict,
    reference_mark: float | None = None,
) -> tuple[dict | None, str | None]:
    """Validate the agents' futures terms; refuse instead of substituting.

    The decision agents own these numbers. The cycle only checks that the
    proposal exists, is internally consistent with the execution price, and
    stays inside the system ceilings -- a proposal outside them is refused,
    never silently clamped, because a clamped size would no longer be the
    decision anyone made.

    ``reference_mark`` is the mark the agents were shown. It is a second
    accepted anchor for ``entry_price``: analysis takes minutes on a moving
    pair, so a live mark can drift past the 1e-3 tolerance from the number the
    agent was told to be consistent with. The bracket and the plan still use
    the live ``mark``.
    """
    if not isinstance(proposal, dict) or not proposal:
        return None, "agent_proposal_missing: no futures entry without agent execution terms"
    margin_mode = str(proposal.get("margin_mode") or "").strip().lower()
    rationale = str(proposal.get("rationale") or "").strip()
    if not rationale:
        return None, "agent_proposal_rejected: rationale is required"
    if margin_mode not in ("isolated", "cross"):
        return None, f"agent_proposal_rejected: margin_mode {margin_mode!r} is not isolated/cross"
    try:
        leverage_raw = proposal["leverage"]
        leverage = float(leverage_raw)
        risk_fraction = float(proposal["risk_fraction"])
        margin_fraction = float(proposal["margin_fraction"])
    except (KeyError, TypeError, ValueError):
        return None, "agent_proposal_rejected: leverage, risk_fraction and margin_fraction are required"
    if not isinstance(leverage_raw, int) or isinstance(leverage_raw, bool):
        return None, "agent_proposal_rejected: leverage must be an integer"
    if not all(math.isfinite(value) for value in (leverage, risk_fraction, margin_fraction)):
        return None, "agent_proposal_rejected: numeric terms must be finite"
    if leverage < 1 or leverage > MAX_AUTO_LEVERAGE:
        return None, (
            f"agent_proposal_rejected: leverage {leverage:g}x is outside "
            f"1-{MAX_AUTO_LEVERAGE:g}x"
        )
    risk_cap = float(config.get("risk_derivatives_max_risk_fraction", 0.02))
    margin_cap = float(config.get("risk_derivatives_max_margin_fraction", 0.5))
    if not 0 < risk_fraction <= risk_cap:
        return None, (
            f"agent_proposal_rejected: risk_fraction {risk_fraction:g} is outside "
            f"0-{risk_cap:g}"
        )
    if not 0 < margin_fraction <= margin_cap:
        return None, (
            f"agent_proposal_rejected: margin_fraction {margin_fraction:g} is outside "
            f"0-{margin_cap:g}"
        )
    entry = proposal.get("entry_price")
    if entry is not None:
        try:
            entry_value = float(entry)
        except (TypeError, ValueError):
            return None, "agent_proposal_rejected: entry_price is not a number"
        if not math.isfinite(entry_value) or entry_value <= 0:
            return None, "agent_proposal_rejected: entry_price must be positive and finite"

        def on_mark(anchor: float | None) -> bool:
            if anchor is None or not math.isfinite(anchor) or anchor <= 0:
                return False
            return abs(entry_value - anchor) <= max(anchor * 0.001, 1e-12)

        if not on_mark(mark) and not on_mark(reference_mark):
            return None, "agent_proposal_rejected: non-market entry_price is unsupported by autoloop"
    stop = proposal.get("stop_loss")
    take = proposal.get("take_profit")
    if (stop is not None) != (take is not None):
        return None, "agent_proposal_rejected: stop_loss and take_profit must be proposed together"
    if stop is not None:
        try:
            stop_value, take_value = float(stop), float(take)
        except (TypeError, ValueError):
            return None, "agent_proposal_rejected: stop_loss/take_profit are not numbers"
        valid = (
            stop_value < mark < take_value if direction == "long"
            else stop_value > mark > take_value
        )
        if not valid:
            return None, (
                f"agent_proposal_rejected: {direction} bracket {stop_value}/{take_value} "
                f"does not protect an entry at {mark}"
            )
    else:
        stop_value = take_value = None
    return {
        "leverage": leverage,
        "risk_fraction": risk_fraction,
        "margin_fraction": margin_fraction,
        "margin_mode": margin_mode,
        "stop_loss": stop_value,
        "take_profit": take_value,
    }, None


def _process_futures_candidate(
    derivatives: Any, ticker: str, signal: str, config: dict, result: DailyResult,
    *, context: dict | None = None, proposal: Any = None, portfolio: Any = None,
    brackets: dict | None = None, intent: str | None = None,
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
    execution_lock: threading.Lock | None = None,
    telemetry: CycleTelemetry | None = None,
    agent_note: str | None = None,
    cycle_id: str | None = None,
    decision_data: dict | None = None,
    cycle_payload: dict | None = None,
    phase_timestamps: dict | None = None,
    guard_baseline: int | None = None,
) -> None:
    """Run futures candidates through a prevalidated closed candle pair.

    The entry terms come from the Portfolio Manager's FuturesExecutionProposal;
    without one the candidate is rejected and durably recorded. The engine's
    ATR bracket is the fallback only for the protective levels, never for the
    margin mode, leverage, or budgets.
    """
    telemetry = telemetry or CycleTelemetry()
    now = datetime.now(UTC)

    def persist_progress(cycle: str, /, **kwargs: Any) -> None:
        if _cycle_writes_allowed(deadline, cancelled):
            persist_futures_cycle_progress(cycle, **kwargs)

    def record_rejection(*args: Any, **kwargs: Any) -> None:
        if _cycle_writes_allowed(deadline, cancelled):
            _record_futures_cycle_rejection(*args, **kwargs)

    # Guard decisions appended before this function belongs to earlier
    # candidates (or none); planning and execution for THIS candidate append
    # from here on, so the row's risk_guard fields slice from this mark.
    if guard_baseline is None:
        guard_baseline = len(getattr(derivatives, "risk_guard_decisions", None) or [])
    context = context or _closed_futures_context(derivatives, ticker)
    if context is None:
        result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
        record_rejection(
            ticker, confirmation_reason="closed 1h/4h candle pair unavailable",
            phase="confirmation", signal=signal, cycle_id=cycle_id,
            telemetry=decision_data, agent_note=agent_note,
        )
        return
    cycle_payload = dict(cycle_payload or {})
    decision_data = dict(decision_data or {})
    phase_timestamps = dict(phase_timestamps or {})
    if not cycle_payload:
        cycle_id, four_hour_open, one_hour_open = futures_cycle_identity(context)
        cycle_payload = {
            "requested_ticker": ticker,
            "unified_symbol": context["unified_symbol"],
            "snapshot": futures_snapshot_payload(context),
            "execution_status": "in_progress",
        }
        if not _cycle_writes_allowed(deadline, cancelled):
            result.reason = "cycle closed before the candidate could claim its signal"
            return
        if not claim_futures_cycle(
            cycle_id,
            symbol=context["unified_symbol"],
            four_hour_open=four_hour_open,
            one_hour_open=one_hour_open,
            payload=cycle_payload,
        ):
            result.reason = "duplicate futures signal skipped"
            return
    candle_1h = context["one_hour"]
    candle_4h = context["four_hour"]
    if intent is None:
        intent = "structured_terms_unavailable"
    if intent != "enter":
        result.reason = {
            "abstain": "agent_abstained",
            "manage_existing": "position_management_only",
            "structured_terms_unavailable": "structured_terms_unavailable",
        }.get(str(intent), f"invalid_futures_intent: {intent}")
        record_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            signal=signal,
            signal_reason=result.reason,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": False, "reason": result.reason},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    direction = _direction(signal)
    confirmation_ok, confirmation_reason = _confirmation_state(context, signal)
    if not confirmation_ok:
        result.reason = "confirmation_missing"
        record_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            confirmation_reason=confirmation_reason,
            phase="confirmation",
            snapshot=context,
            signal=signal,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": False, "reason": confirmation_reason},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    symbol = context["unified_symbol"]
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        result.reason = f"signal_not_tradeable: {signal}"
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": True, "reason": "not_required_for_non_tradeable_signal"},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    with telemetry.span("futures_mark_price", ticker):
        mark = _unified_mark_price(derivatives._bridge, symbol)
    if mark is None:
        result.reason = "derivatives entry requires a price"
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": {"valid": False, "reason": result.reason},
                "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    terms, proposal_error = _proposal_terms(
        proposal, mark=mark, reference_mark=context.get("mark_price"),
        direction=direction, config=config,
    )
    if terms is None:
        result.reason = proposal_error
        record_rejection(
            ticker,
            unified_symbol=symbol,
            signal=signal,
            signal_reason=proposal_error,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "agent_proposal": redact_futures_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": {"valid": False, "reason": proposal_error},
                "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    proposal_validation = {"valid": True, "reason": None}
    stop, take = terms["stop_loss"], terms["take_profit"]
    if stop is None or take is None:
        with telemetry.span("protective_bracket", ticker):
            long_bracket, short_bracket = _direction_brackets(ticker, brackets)
            stop, take = long_bracket if direction == "long" else short_bracket
    if stop is None or take is None:
        result.reason = "derivatives entry requires price and protective bracket"
        proposal_validation = {"valid": False, "reason": result.reason}
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data, "agent_proposal": redact_futures_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": proposal_validation, "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    if not (stop < mark < take if direction == "long" else stop > mark > take):
        result.reason = "invalid protective bracket"
        proposal_validation = {"valid": False, "reason": result.reason}
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data, "agent_proposal": redact_futures_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": proposal_validation, "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    expires_at = now + (
        datetime.fromtimestamp(candle_4h.close_time / 1000, UTC)
        - datetime.fromtimestamp(candle_4h.open_time / 1000, UTC)
    )
    cycle_payload.update({
        **decision_data,
        "requested_ticker": ticker,
        "unified_symbol": symbol,
        "signal": signal,
        "signal_reason": "tradeable_signal",
        "confirmation_result": confirmation_ok,
        "confirmation_reason": confirmation_reason,
        "snapshot": _snapshot_payload(context),
        "agent_proposal": redact_futures_proposal(proposal),
        "proposal_validation": proposal_validation,
        "mark_price": mark,
        # HEAD carried the PM's verbatim decision onto the terminal success row;
        # staging it here keeps it on the proposal, planning, execution AND
        # failure writes that all build from cycle_payload.
        "agent_decision_note": (
            redact_futures_payload(agent_note[:6000]) if agent_note else None
        ),
        "plan": {
            "stop_loss": stop,
            "take_profit": take,
            "leverage": terms["leverage"],
            "risk_fraction": terms["risk_fraction"],
            "margin_fraction": terms["margin_fraction"],
            "margin_mode": terms["margin_mode"],
        },
    })
    phase_timestamps["proposal_validated_at"] = utc_now_iso()
    persist_progress(
        cycle_id, phase="proposal", payload={**cycle_payload, "phase_timestamps": phase_timestamps},
        timestamp=phase_timestamps["proposal_validated_at"],
    )
    FuturesSignalEnvelope.create(
        symbol=symbol,
        candle_open=datetime.fromtimestamp(candle_1h.open_time / 1000, UTC),
        candle_close=datetime.fromtimestamp(candle_1h.close_time / 1000, UTC),
        direction=direction, entry=mark, stop=stop, targets=(take,),
        created_at=now, expires_at=expires_at,
        confirmation_candle_open=datetime.fromtimestamp(candle_4h.open_time / 1000, UTC),
        strategy="autoloop", strategy_version="1",
    )
    try:
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} futures planning")
        phase_timestamps["planning_started_at"] = utc_now_iso()
        persist_progress(
            cycle_id, phase="planning",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps},
            timestamp=phase_timestamps["planning_started_at"],
        )
        with telemetry.span("portfolio_sync", ticker):
            book = portfolio if portfolio is not None else derivatives._bridge.sync_portfolio()
        with telemetry.span("planning", ticker):
            plan = derivatives.plan_order(
                ticker, signal, portfolio=book, price=mark,
                leverage=terms["leverage"], stop_loss=stop, take_profit=take,
                risk_fraction=terms["risk_fraction"],
                margin_fraction=terms["margin_fraction"],
                margin_mode=terms["margin_mode"],
                futures_execution_intent=intent,
            )
        result.plan = plan
        phase_timestamps["planning_finished_at"] = utc_now_iso()
        cycle_payload["plan"] = {
            **cycle_payload.get("plan", {}),
            "side": getattr(plan, "side", None),
            "quantity": getattr(plan, "quantity", None),
            "estimated_price": getattr(plan, "estimated_price", None),
            "cost": getattr(plan, "cost", None),
            "leverage": getattr(plan, "leverage", terms["leverage"]),
            "margin_mode": getattr(plan, "margin_mode", terms["margin_mode"]),
            "stop_loss": getattr(plan, "stop_loss", stop),
            "take_profit": getattr(plan, "take_profit", take),
            "reason": getattr(plan, "reason", None),
        }
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        cycle_payload["risk_guard"] = risk_decisions[-1] if risk_decisions else {
            "allowed": None, "halted": None, "reason": "not_called",
        }
        persist_progress(
            cycle_id, phase="planning",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps},
            timestamp=phase_timestamps["planning_finished_at"],
        )
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} execution")
        phase_timestamps["execution_started_at"] = utc_now_iso()
        persist_progress(
            cycle_id, phase="execution",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps,
                     "execution_status": "in_progress"},
            timestamp=phase_timestamps["execution_started_at"],
        )
        with telemetry.span("execution_policy", ticker):
            _handle_plan(
                derivatives,
                plan,
                book,
                result,
                execute_guard=lambda execute: _execute_with_deadline(
                    execute, deadline, cancelled, execution_lock
                ),
            )
        # Terminal outcome vocabulary (audit truth): only a venue-confirmed
        # order is "executed"; a refused/pending/no-order plan keeps its own
        # execution_status so the funnel never counts it as an entry.
        # risk_rejected keys off the guard's actual decision, never a reason
        # substring; awaiting_approval keys off the executor's own literal
        # "awaiting approval" reasons (daily.py), a vocabulary no guard
        # refusal shares, so an entry can never be counted from it either.
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        last_decision = risk_decisions[-1] if risk_decisions else None
        if result.executed:
            execution_status = "executed"
        elif result.reason and "approval" in result.reason:
            execution_status = "awaiting_approval"
        elif (
            plan.side is None
            and last_decision is not None
            and last_decision.get("allowed") is False
        ):
            execution_status = "risk_rejected"
        else:
            execution_status = "not_executed"
        protection_state = "configured" if plan.stop_loss is not None and plan.take_profit is not None else "missing"
        phase_timestamps["execution_finished_at"] = utc_now_iso()
        cycle_payload.update({
            "execution_status": execution_status,
            "executed": result.executed,
            "order_id": result.order_id,
            "plan_reason": plan.reason,
            "protection_state": protection_state,
            "risk_guard_decisions": risk_decisions,
            "risk_guard": last_decision or {"allowed": None, "halted": None, "reason": "not_called"},
            "phase_timestamps": phase_timestamps,
        })
        if result.reason:
            cycle_payload["execution_reason"] = result.reason
        terminal_phase = "entry" if execution_status == "executed" else "execution"
        terminal_status = "completed" if execution_status == "executed" else "rejected"
        job_store.update_futures_cycle(
            cycle_id, status=terminal_status, phase=terminal_phase,
            payload=redact_futures_payload(cycle_payload),
            terminal=True,
        )
    except Exception as exc:
        # This terminal write must carry the complete audit trail: the row is
        # locked once it lands terminal (job_store._FUTURES_TERMINAL_STATUSES),
        # so the outer handler in _process_candidate cannot add risk_guard
        # afterwards.
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        # A guard refusal (plan-stage halt or execute-stage refusal) is a
        # deliberate "no", not a malfunction: it keeps its own terminal shape
        # (rejected / risk_rejected) so the funnel never mixes refusals into
        # execution failures.
        if isinstance(exc, PermissionError) and str(exc).startswith("risk guard"):
            persist_progress(
                cycle_id,
                status="rejected",
                phase="execution" if phase_timestamps.get("execution_started_at") else "planning",
                payload={
                    **cycle_payload,
                    "signal": signal,
                    "signal_reason": "tradeable_signal",
                    "plan_reason": getattr(result.plan, "reason", None),
                    "execution_status": "risk_rejected",
                    "executed": False,
                    "order_id": result.order_id,
                    "risk_guard_decisions": risk_decisions,
                    "risk_guard": risk_decisions[-1] if risk_decisions else {
                        "allowed": None, "halted": None, "reason": "not_called",
                    },
                    "execution_reason": str(exc),
                    "phase_timestamps": phase_timestamps,
                },
                terminal=True,
            )
            raise
        persist_progress(
            cycle_id,
            status="failed",
            phase="execution",
            payload={
                **cycle_payload,
                "signal": signal,
                "signal_reason": "tradeable_signal",
                "plan_reason": getattr(result.plan, "reason", None),
                "execution_status": "failed",
                "executed": bool(result.executed),
                "order_id": result.order_id,
                "risk_guard_decisions": risk_decisions,
                "risk_guard": risk_decisions[-1] if risk_decisions else {
                    "allowed": None, "halted": None, "reason": "not_called",
                },
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "phase_timestamps": {
                    **phase_timestamps,
                    "execution_failed_at": utc_now_iso(),
                },
            },
            terminal=True,
        )
        raise


def _default_graph_builder(config: dict, analysts: tuple[str, ...]) -> Any:
    # Lazy like server/daily._analyze: the graph import chain pulls LangChain.
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(config=config, selected_analysts=analysts)


def _build_bridge(config: dict) -> Any:
    """The venue for the cycle's plan path; tests monkeypatch this seam like
    approvals._build_bridge (engine seam bridge.py:126-129)."""
    from tradingagents.execution.bridge import ExchangeBridge

    return ExchangeBridge(config=config)


def _build_derivatives_executor(config: dict) -> Any:
    """The futures venue for the cycle's derivatives path (lazy import — the
    executor composes the same bridge, so RiskGuard/audit/redaction keep
    working unchanged). Tests monkeypatch this seam to inject a fake."""
    from tradingagents.execution.derivatives import DerivativesExecutor

    return DerivativesExecutor(config=config)


def _direction(signal: str) -> str:
    """Map an authorized entry rating to its futures bracket direction.

    Sell/Underweight become short only after the separate PM intent gate has
    explicitly authorized a fresh ``enter`` decision; the rating alone is not
    a futures authorization.
    """
    return "short" if signal in (_SELL, _UNDERWEIGHT) else "long"


def _bracket_prices(ticker: str, direction: str = "long") -> tuple[float | None, float | None]:
    """Fetch the existing ATR bracket as values for risk sizing."""
    from tradingagents.dataflows.screening import atr_targets_signed

    return atr_targets_signed(ticker, _today_iso(), direction)


def _attach_bracket(plan: Any, ticker: str, direction: str = "long") -> Any:
    """Attach the ATR-derived bracket to an already-sized plan."""
    stop, take = _bracket_prices(ticker, direction)
    if stop is None or take is None:
        return plan
    return plan.model_copy(update={"stop_loss": stop, "take_profit": take})


def _result_summary(result: DailyResult) -> dict:
    plan = result.plan
    return {
        "ticker": result.ticker,
        "signal": result.signal,
        "executed": result.executed,
        "order_id": result.order_id,
        "reason": result.reason,
        "stop_loss": getattr(plan, "stop_loss", None) if plan is not None else None,
        "take_profit": getattr(plan, "take_profit", None) if plan is not None else None,
    }


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


# --- endpoints ----------------------------------------------------------------

# Zero-filled bucket order for the /autoloop/cycles funnel summary.
# entered requires the executed confirmation; awaiting approval, plan risk
# refusal, no-order and execute failure each keep their own bucket and are
# never counted as entered.
_CYCLE_SUMMARY_BUCKETS = (
    "market_rejected", "confirmation_rejected", "proposal_rejected", "entered",
    "awaiting_approval", "risk_rejected", "not_executed", "execution_failed",
    "in_flight", "interrupted", "other",
)


def _payload() -> dict:
    state = _load_state()
    return {
        "generated_at": utc_now_iso(),
        "enabled": bool(state.get("enabled", False)),
        "interval_minutes": _interval_from(state),
        "running_cycle": _cycle_lock.locked(),
        "gates": gates(),
        "last_cycle": _last_cycle_snapshot(),
        "cycles": job_store.list_futures_cycles(limit=20),
    }


def _cycle_payload(cycle: dict) -> dict:
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    return {
        "cycle_id": cycle["cycle_id"],
        "symbol": cycle["symbol"],
        "four_hour_open": cycle["four_hour_open"],
        "one_hour_open": cycle["one_hour_open"],
        "strategy_version": cycle["strategy_version"],
        "status": cycle["status"],
        "phase": cycle["phase"],
        "payload": payload,
        "created_at": cycle["created_at"],
        "updated_at": cycle["updated_at"],
        "finished_at": cycle["finished_at"],
    }


@router.get("/autoloop/cycles")
def get_autoloop_cycles(limit: int = 20, symbol: str | None = None):
    if limit < 1 or limit > 100:
        raise ApiError(400, "validation_error", '"limit" must be between 1 and 100')
    if symbol is not None and not symbol.strip():
        raise ApiError(400, "validation_error", '"symbol" must be a non-empty string when provided')
    symbol_filter = (symbol.strip() or None) if symbol else None
    summary = job_store.futures_cycle_summary()
    return {
        "generated_at": utc_now_iso(),
        "items": [
            _cycle_payload(item)
            for item in job_store.list_futures_cycles(limit=limit, symbol=symbol_filter)
        ],
        "summary": {
            **dict.fromkeys(_CYCLE_SUMMARY_BUCKETS, 0),
            **summary,
            "total": sum(summary.values()),
        },
        "symbols": job_store.futures_cycle_symbol_rollup(),
        "enabled": bool(_load_state().get("enabled", False)),
        "interval_minutes": _interval_from(_load_state()),
        "running_cycle": _cycle_lock.locked(),
        "last_cycle": _last_cycle_snapshot(),
    }


@router.get("/autoloop")
def get_autoloop():
    return _payload()


@router.post("/autoloop")
async def set_autoloop(request: Request):
    body = await json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "validation_error", '"enabled" must be a boolean')
    interval = body.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES
    ):
        raise ApiError(
            400, "validation_error",
            f'"interval_minutes" must be an integer between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES}',
        )
    state = gates()
    if enabled and not state["all_open"]:
        raise ApiError(
            409, "gate_closed",
            "enabling the autoloop requires the auto-confirm gates open "
            "(exec_auto_confirm=True in config AND TRADINGAGENTS_EXEC_AUTO_CONFIRM=true "
            "in the environment); the engine's own policy stays fail-closed",
            {"gates": state},
        )
    _save_state({"enabled": enabled, "interval_minutes": interval, "updated_at": utc_now_iso()})
    audit_log.record("autoloop_updated", enabled=enabled, interval_minutes=interval)
    if enabled:
        ensure_started()
    _wakeup.set()  # a changed interval or enabled flag takes effect on the next wake
    return _payload()


@router.post("/autoloop/run-now", status_code=202)
async def run_autoloop_now(request: Request):
    """Run one cycle now on a worker thread. ``skipped_busy`` is the
    best-effort hint read before the spawn; the cycle itself re-checks and
    writes the ``autoloop_cycle_skipped`` trail if a run/job is active."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    if _cycle_lock.locked():
        status = "already_running"
    else:
        busy = runs_mod.active_run_id() is not None or daily_mod.active_job_id() is not None
        status = "skipped_busy" if busy else "started"
        _spawn_cycle()
    return {"generated_at": utc_now_iso(), "status": status}


@router.post("/halt")
async def halt_now(request: Request):
    """The emergency STOP button: one halt line in the shared execution audit
    log and the RiskGuard of every process refuses the rest of today. Never
    gated — a kill switch must always work."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    # The timestamp is what makes the halt day-scoped (risk.py:111-119): a
    # halt line without one would be ignored by _halted_today.
    append_event(get_config()["exec_log_path"], {
        "event": "halt",
        "timestamp": datetime.now(UTC).isoformat(),
        "reason": "manual halt from the control panel",
    })
    audit_log.record("autoloop_halt")
    return {"generated_at": utc_now_iso(), "halted": True}


@router.post("/halt/reset")
async def reset_halt(request: Request):
    """Lift today's halt: a halt_reset line today ends the sticky halt
    (risk.py:163-173) until a new breach or a new halt line."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    append_event(get_config()["exec_log_path"], {
        "event": "halt_reset",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    audit_log.record("autoloop_halt_reset")
    return {"generated_at": utc_now_iso(), "halted": False}
