"""Autoloop: the scheduled trading cycle + the kill-switch halt endpoints.

# ai-note: split deferred because the current pass focused on deadline and scheduler safety; revisit when scheduler/state, cycle execution, and futures orchestration need independent changes.

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
from datetime import datetime, timezone
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
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution.audit import append_event
from tradingagents.execution.daily import DailyResult, _handle_plan
from tradingagents.execution.futures_account import read_futures_account
from tradingagents.execution.futures_scheduler import closed_candle_pair
from tradingagents.execution.futures_signals import FuturesSignalEnvelope
from tradingagents.portfolio import PortfolioContext

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
SCREENING_TIMEOUT_SECONDS = 180.0
# 3 candidates × ~10 min sequential LLM work through a slow relay fits inside
# one hour; the scheduler skips interval ticks while the lock is held, so a
# generous budget only delays the next cycle, it never overlaps one.
CYCLE_TIMEOUT_SECONDS = 3600.0

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


def _screen_with_timeout(
    crypto_universe: Callable,
    stock_universe: Callable,
    screen_universe: Callable,
    config: dict,
    trade_date: str,
    *,
    telemetry: CycleTelemetry | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Run external screening off-cycle so a vendor stall cannot block forever."""
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    telemetry = telemetry or CycleTelemetry()

    def worker() -> None:
        try:
            with telemetry.span("screening:crypto_universe"):
                crypto = crypto_universe(config, AUTO_UNIVERSE_PER_ASSET)
            with telemetry.span("screening:stock_universe"):
                stocks = stock_universe(config, AUTO_UNIVERSE_PER_ASSET)
            with telemetry.span("screening:metrics"):
                candidates = screen_universe(crypto, stocks, trade_date, AUTO_SCREEN_POOL)
            result_queue.put((crypto, stocks, candidates))
        except BaseException as exc:  # hand exception back to cycle thread
            result_queue.put(exc)

    threading.Thread(target=worker, name="server-autoloop-screen", daemon=True).start()
    try:
        outcome = result_queue.get(timeout=SCREENING_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        telemetry.close()
        snapshot = telemetry.snapshot()
        stage = snapshot["active_stage"] or snapshot["last_stage"] or "screening"
        raise TimeoutError(
            f"screening timed out during {stage} after {SCREENING_TIMEOUT_SECONDS:.0f}s"
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
    """Drop candidates that can never become an order, before the LLM pipeline.

    In futures mode two kinds of screened candidate are guaranteed to end with
    no order: stocks (the venue bridge only plans crypto) and symbols with no
    live USDT-settled contract. Each one still costs a full 8-10 minute LLM
    pipeline, which is how a cycle used to time out with its executable
    candidates never analysed.

    Returns ``(runnable, skipped)``. ``skipped`` entries are
    ``{"ticker", "reason"}`` for the cycle summary and audit. A market-metadata
    failure fails open: the per-candidate path re-checks and rejects on its own,
    so an unreachable venue must not empty the cycle.
    """
    if derivatives is None:
        return candidates, []  # spot mode: stocks still carry advisory value

    bridge = getattr(derivatives, "_bridge", None)
    if bridge is None:
        return candidates, []

    telemetry = telemetry or CycleTelemetry()
    try:
        with telemetry.span("preflight:futures_markets"):
            markets = bridge.client().load_markets()
    except Exception as exc:  # venue unreachable: leave the decision to the candidate path
        logger.warning("autoloop preflight: futures markets unavailable (%s)", exc)
        return candidates, []

    runnable: list[dict] = []
    skipped: list[dict] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker"))
        asset_type = str(candidate.get("asset_type") or "crypto")
        if asset_type != "crypto":
            skipped.append({
                "ticker": ticker,
                "reason": "advisory only in futures mode — the venue bridge cannot plan a stock order",
            })
            continue
        try:
            bridge.futures_symbol_for(ticker, markets=markets)
        except ValueError as exc:
            reason = f"no active futures contract: {exc}"
            skipped.append({"ticker": ticker, "reason": reason})
            _record_futures_cycle_rejection(ticker, market_rejection_reason=str(exc))
            continue
        runnable.append(candidate)
    return runnable, skipped


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
        summary["universe"] = {"crypto": len(crypto), "stock": len(stocks)}
        _cycle_timeout(deadline, "venue setup")
        with telemetry.span("venue_setup"):
            bridge = _build_bridge(config)
            derivatives = _build_derivatives_executor(config) if config.get("exec_derivatives") is True else None

        # Drop candidates that cannot end in an order before spending LLM time
        # on them (futures mode: stocks and symbols without a live contract).
        _cycle_timeout(deadline, "candidate preflight")
        candidates, skipped = _preflight_candidates(candidates, derivatives, telemetry=telemetry)
        if skipped:
            summary["skipped"] = skipped
            audit_log.record("autoloop_candidates_skipped", skipped=skipped)

        # Crypto candidates first: the venue bridge only executes crypto, so a
        # cycle that runs out of time must have already seen its executable
        # candidates rather than spending the budget on advisory-only stocks.
        # Stable sort — score order is preserved inside each group.
        candidates.sort(key=lambda entry: str(entry.get("asset_type") or "crypto") != "crypto")
        # Cap after the drops: the screen returns a wider pool so filtering
        # cannot leave the cycle with nothing to run.
        candidates = candidates[:AUTO_TOP_N]
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
                return result
            except ValueError as exc:
                result.reason = f"market_rejected: {exc}"
                _record_futures_cycle_rejection(ticker, market_rejection_reason=str(exc))
                return result
            if futures_context is None:
                result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
                _record_futures_cycle_rejection(
                    ticker,
                    confirmation_reason="closed 1h/4h candle pair unavailable",
                    phase="confirmation",
                )
                return result
            with telemetry.span("futures_account", ticker):
                try:
                    futures_account = read_futures_account(derivatives._bridge)
                except (PermissionError, ValueError) as exc:
                    result.reason = f"futures_account_unavailable: {exc}"
                    return result
                futures_portfolio = _futures_portfolio(futures_account)
            with telemetry.span("protective_bracket", ticker):
                # Both directions: the agents need the engine's reference levels
                # before they decide, and the entry path must reuse these reads
                # rather than hitting the vendor a second time.
                brackets = {
                    "long": _bracket_prices(ticker, direction="long"),
                    "short": _bracket_prices(ticker, direction="short"),
                }
            with telemetry.span("futures_decision_context", ticker):
                execution_context = _futures_decision_context(
                    futures_context, futures_account, ticker, config, brackets,
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
    return result


def _closed_futures_context(derivatives: Any, ticker: str) -> dict | None:
    now = datetime.now(timezone.utc)
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


def _futures_portfolio(snapshot: Any) -> PortfolioContext:
    """Sizing book backed by the FUTURES wallet, not the spot balance.

    ``ExchangeBridge.sync_portfolio`` reads the spot account, so sizing a
    futures entry against it would risk money the derivatives wallet does not
    hold. ``cash`` is the futures equity (wallet balance plus unrealized PnL);
    open positions are deliberately not added again, because that equity
    already contains their PnL and re-adding their notional would overstate
    the denominator every percentage cap divides by. Open exposure is instead
    reported to the decision agents in their context.
    """
    equity = snapshot.equity if snapshot.equity is not None else snapshot.available_balance
    currency = snapshot.equity_currency or snapshot.available_balance_currency
    return PortfolioContext(
        cash=float(equity) if equity is not None else None,
        currency=currency,
    )


def _direction_brackets(
    ticker: str, brackets: dict | None = None,
) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    """(long, short) ATR brackets, reusing a caller's reads when supplied."""
    if brackets is not None:
        return brackets["long"], brackets["short"]
    return _bracket_prices(ticker, direction="long"), _bracket_prices(ticker, direction="short")


def _futures_decision_context(
    market: dict, account: Any, ticker: str, config: dict,
    brackets: dict | None = None,
) -> str:
    """Account, market, and hard-limit facts the futures agents decide against.

    The agents own margin mode, leverage, and the risk/margin budgets, so they
    need the real wallet and the venue's caps; the executor enforces the same
    caps afterwards and refuses rather than substituting its own numbers.
    """
    def number(value: Any, suffix: str = "") -> str:
        return f"{float(value):,.4f}{suffix}" if value is not None else "unavailable"

    def percent(value: Any) -> str:
        return f"{float(value) * 100:.4f}%" if value is not None else "unavailable"

    currency = account.equity_currency or account.available_balance_currency or ""
    lines = [
        f"Venue profile: {account.venue_profile or 'unreported'}",
        f"Account scope: {account.account_scope}",
        f"Futures equity: {number(account.equity, ' ' + currency if currency else '')}",
        f"Available margin: {number(account.available_balance, ' ' + currency if currency else '')}",
        f"Wallet balance: {number(account.wallet_balance, ' ' + currency if currency else '')}",
        f"Unrealized PnL: {number(account.unrealized_pnl, ' ' + currency if currency else '')}",
    ]
    if account.positions:
        lines.append("Open futures positions:")
        for position in account.positions:
            lines.append(
                f"- {position.symbol} {position.side} contracts={position.contracts} "
                f"entry={position.entry_price} mark={position.mark_price} "
                f"leverage={position.leverage} margin_mode={position.margin_mode} "
                f"uPnL={position.unrealized_pnl}"
            )
    else:
        lines.append("Open futures positions: none")
    lines.extend([
        "",
        f"Instrument: {market.get('unified_symbol')} (requested as {ticker})",
        f"Closed 1h candle: open={market['one_hour'].open} high={market['one_hour'].high} "
        f"low={market['one_hour'].low} close={market['one_hour'].close} "
        f"direction={market.get('one_hour_direction')}",
        f"Closed 4h candle: open={market['four_hour'].open} high={market['four_hour'].high} "
        f"low={market['four_hour'].low} close={market['four_hour'].close} "
        f"direction={market.get('four_hour_direction')}",
        f"Funding rate: {percent(market.get('funding_rate'))} "
        f"(next funding {market.get('funding_datetime') or 'unavailable'})",
        f"Mark price: {number(market.get('mark_price'))}",
        f"Open interest: {number(market.get('open_interest'))} "
        f"contracts (notional {number(market.get('open_interest_value'))})",
    ])
    long_bracket, short_bracket = _direction_brackets(ticker, brackets)
    if long_bracket[0] is not None:
        lines.append(f"Engine ATR bracket (long): stop={long_bracket[0]} target={long_bracket[1]}")
    if short_bracket[0] is not None:
        lines.append(f"Engine ATR bracket (short): stop={short_bracket[0]} target={short_bracket[1]}")
    lines.extend([
        "",
        "Hard limits the execution layer enforces (a proposal outside them is refused, "
        "not resized):",
        f"- Maximum leverage: {MAX_AUTO_LEVERAGE:g}x",
        f"- Maximum risk_fraction: {float(config.get('risk_derivatives_max_risk_fraction', 0.02)):g}",
        f"- Maximum margin_fraction: {float(config.get('risk_derivatives_max_margin_fraction', 0.5)):g}",
        f"- Maximum derivatives notional: "
        f"{float(config.get('risk_max_derivatives_exposure_pct', 0.0)):g}% of equity",
        f"- Maximum position per asset: "
        f"{float(config.get('risk_max_position_pct_per_asset', 25.0)):g}% of equity",
        f"- Maximum total exposure: "
        f"{float(config.get('risk_max_total_exposure_pct', 80.0)):g}% of equity",
        "A FuturesExecutionProposal is required for any entry: margin mode, leverage, "
        "risk_fraction, margin_fraction, and an evidence-based rationale. Without one "
        "no order is placed.",
    ])
    return "\n".join(lines)


def _confirmation_state(context: dict, signal: str) -> tuple[bool, str]:
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        return True, "not_required_for_non_tradeable_signal"
    expected = "bearish" if _direction(signal) == "short" else "bullish"
    directions = (context.get("one_hour_direction"), context.get("four_hour_direction"))
    if any(direction == "doji" for direction in directions):
        return False, "doji_candle"
    if directions != (expected, expected):
        return False, "direction_mismatch"
    return True, "aligned_direction"


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
    agent_note: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    one_hour, four_hour = closed_candle_pair(now)
    cycle_id = f"futures:{ticker}:{four_hour[0].isoformat()}:{one_hour[0].isoformat()}:1"
    symbol = unified_symbol or ticker
    payload = {
        "requested_ticker": ticker,
        "unified_symbol": unified_symbol,
        "market_rejection_reason": market_rejection_reason,
        "confirmation_reason": confirmation_reason,
        "signal": signal,
        "signal_reason": signal_reason,
    }
    # A cycle that stops before entry still observed the closed candle pair, so
    # record the same verdict the entry path would have reached. Without it the
    # UI can only say "no data" for a cycle that did read the candles.
    if snapshot and signal:
        confirmed, derived_reason = _confirmation_state(snapshot, signal)
        payload["confirmation_result"] = confirmed
        if not confirmation_reason:
            payload["confirmation_reason"] = derived_reason
    # The Portfolio Manager's verbatim decision (rating, thesis, execution
    # intent) is the only human-legible record of WHY a cycle was refused.
    if agent_note:
        payload["agent_decision_note"] = agent_note[:6000]
    if snapshot:
        payload["snapshot"] = _snapshot_payload(snapshot)
    # Create the row when absent, then always converge it to the terminal
    # rejected state; update_futures_cycle merges, so a prior snapshot survives.
    job_store.claim_futures_cycle(
        cycle_id,
        symbol=symbol,
        four_hour_open=four_hour[0].isoformat(),
        one_hour_open=one_hour[0].isoformat(),
        strategy_version="1",
        payload=payload,
    )
    job_store.update_futures_cycle(
        cycle_id, status="rejected", phase=phase, payload=payload, terminal=True,
    )


def _snapshot_payload(context: dict) -> dict:
    def encode(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {key: encode(getattr(value, key)) for key in value.__dataclass_fields__}
        if isinstance(value, dict):
            return {str(key): encode(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [encode(item) for item in value]
        return value
    return encode(context)


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
) -> tuple[dict | None, str | None]:
    """Validate the agents' futures terms; refuse instead of substituting.

    The decision agents own these numbers. The cycle only checks that the
    proposal exists, is internally consistent with the execution price, and
    stays inside the system ceilings -- a proposal outside them is refused,
    never silently clamped, because a clamped size would no longer be the
    decision anyone made.
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
        if abs(entry_value - mark) > max(mark * 0.001, 1e-12):
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
) -> None:
    """Run futures candidates through a prevalidated closed candle pair.

    The entry terms come from the Portfolio Manager's FuturesExecutionProposal;
    without one the candidate is rejected and durably recorded. The engine's
    ATR bracket is the fallback only for the protective levels, never for the
    margin mode, leverage, or budgets.
    """
    telemetry = telemetry or CycleTelemetry()
    now = datetime.now(timezone.utc)
    context = context or _closed_futures_context(derivatives, ticker)
    if context is None:
        result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
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
        _record_futures_cycle_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            signal=signal,
            signal_reason=result.reason,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
        )
        return
    direction = _direction(signal)
    confirmation_ok, confirmation_reason = _confirmation_state(context, signal)
    if not confirmation_ok:
        result.reason = "confirmation_missing"
        _record_futures_cycle_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            confirmation_reason=confirmation_reason,
            phase="confirmation",
            snapshot=context,
            signal=signal,
            agent_note=agent_note,
        )
        return
    symbol = context["unified_symbol"]
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        # No entry to decide, so no proposal is required and no cycle is
        # claimed: burning the candle's cycle id on a Hold would block a later
        # tradeable signal for the same closed pair.
        result.reason = f"signal_not_tradeable: {signal}"
        return
    with telemetry.span("futures_mark_price", ticker):
        mark = _unified_mark_price(derivatives._bridge, symbol)
    if mark is None:
        result.reason = "derivatives entry requires a price"
        return
    terms, proposal_error = _proposal_terms(
        proposal, mark=mark, direction=direction, config=config,
    )
    if terms is None:
        result.reason = proposal_error
        _record_futures_cycle_rejection(
            ticker,
            unified_symbol=symbol,
            signal=signal,
            signal_reason=proposal_error,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
        )
        return
    stop, take = terms["stop_loss"], terms["take_profit"]
    if stop is None or take is None:
        with telemetry.span("protective_bracket", ticker):
            long_bracket, short_bracket = _direction_brackets(ticker, brackets)
            stop, take = long_bracket if direction == "long" else short_bracket
    if stop is None or take is None:
        result.reason = "derivatives entry requires price and protective bracket"
        return
    if not (stop < mark < take if direction == "long" else stop > mark > take):
        result.reason = "invalid protective bracket"
        return
    expires_at = now + (
        datetime.fromtimestamp(candle_4h.close_time / 1000, timezone.utc)
        - datetime.fromtimestamp(candle_4h.open_time / 1000, timezone.utc)
    )
    envelope = FuturesSignalEnvelope.create(
        symbol=symbol,
        candle_open=datetime.fromtimestamp(candle_1h.open_time / 1000, timezone.utc),
        candle_close=datetime.fromtimestamp(candle_1h.close_time / 1000, timezone.utc),
        direction=direction, entry=mark, stop=stop, targets=(take,),
        created_at=now, expires_at=expires_at,
        confirmation_candle_open=datetime.fromtimestamp(candle_4h.open_time / 1000, timezone.utc),
        strategy="autoloop", strategy_version="1",
    )
    if not job_store.claim_futures_cycle(
        envelope.cycle_id, symbol=symbol,
        four_hour_open=envelope.confirmation_candle_open,
        one_hour_open=envelope.candle_open,
        strategy_version=envelope.strategy_version,
        payload={
            "requested_ticker": ticker,
            "unified_symbol": symbol,
            "signal": signal,
            "signal_reason": "tradeable_signal" if signal in RATINGS_5_TIER else "signal_not_tradeable",
            "confirmation_result": True,
            "confirmation_reason": "aligned_direction" if signal in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT) else "not_required_for_non_tradeable_signal",
            "snapshot": _snapshot_payload(context),
            "agent_proposal": _snapshot_payload(proposal) if isinstance(proposal, dict) else {},
        },
    ):
        result.reason = "duplicate futures signal skipped"
        return
    try:
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} futures planning")
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
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} execution")
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
        execution_status = (
            "executed" if result.executed else "awaiting_approval" if result.reason and "approval" in result.reason
            else "not_executed"
        )
        protection_state = "configured" if plan.stop_loss is not None and plan.take_profit is not None else "missing"
        job_store.update_futures_cycle(
            envelope.cycle_id, status="completed", phase="entry",
            payload={
                "signal": signal,
                "signal_reason": "tradeable_signal" if signal in RATINGS_5_TIER else "signal_not_tradeable",
                "confirmation_result": confirmation_ok,
                "confirmation_reason": confirmation_reason,
                "market_rejection_reason": None,
                "plan_reason": plan.reason,
                "execution_status": execution_status,
                "executed": result.executed,
                "order_id": result.order_id,
                "protection_state": protection_state,
                "agent_proposal": _snapshot_payload(proposal),
                "agent_decision_note": agent_note,
                "plan": getattr(plan, "__dict__", {}),
            },
            terminal=True,
        )
    except Exception as exc:
        job_store.update_futures_cycle(
            envelope.cycle_id,
            status="failed",
            phase="entry",
            payload={
                "signal": signal,
                "signal_reason": "tradeable_signal" if signal in RATINGS_5_TIER else "signal_not_tradeable",
                "plan_reason": getattr(result.plan, "reason", None),
                "execution_status": "failed",
                "error": str(exc),
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
    return datetime.now(timezone.utc).date().isoformat()


# --- endpoints ----------------------------------------------------------------

# Zero-filled bucket order for the /autoloop/cycles funnel summary.
_CYCLE_SUMMARY_BUCKETS = (
    "market_rejected", "confirmation_rejected", "proposal_rejected", "entered",
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    audit_log.record("autoloop_halt_reset")
    return {"generated_at": utc_now_iso(), "halted": False}
