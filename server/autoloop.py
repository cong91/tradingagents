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

State: ``enabled``/``interval_minutes`` persist in ``paths.AUTOLOOP_PATH``
(the scheduler re-reads the file every wake, so edits need no restart);
``last_cycle`` is an in-memory summary of the most recent cycle only.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from server import audit as audit_log, daily as daily_mod, job_store, paths, runs as runs_mod
from server.autoloop_telemetry import CycleTelemetry
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution.audit import append_event
from tradingagents.execution.daily import DailyResult, _handle_plan
from tradingagents.execution.futures_scheduler import closed_candle_pair
from tradingagents.execution.futures_signals import FuturesSignalEnvelope

router = APIRouter(prefix="/api", tags=["autoloop"])

# Auto-cycle tuning: how many universe entries to pull per market and how
# many screened candidates graduate into the expensive LLM pipeline.
AUTO_UNIVERSE_PER_ASSET = 30
AUTO_TOP_N = 8
SCREENING_TIMEOUT_SECONDS = 180.0
# 8 candidates × ~8 sequential LLM calls each through a slow relay needs well
# over 15 minutes; the scheduler skips interval ticks while the lock is held,
# so a generous budget only delays the next cycle, it never overlaps one.
CYCLE_TIMEOUT_SECONDS = 3600.0

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440
DEFAULT_INTERVAL_MINUTES = 1440

# Hard system ceiling on the leverage an auto cycle may REQUEST from the
# derivatives path, regardless of the configured risk cap. The RiskGuard's
# own ``risk_max_derivatives_leverage`` stays the authoritative gate (it can
# still refuse below this); this constant only bounds what the cycle asks for.
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
                candidates = screen_universe(crypto, stocks, trade_date, AUTO_TOP_N)
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
    """The persisted {enabled, interval_minutes, updated_at}; defaults when
    missing or unreadable — a fresh install has the loop OFF, not an error."""
    path = paths.AUTOLOOP_PATH
    if not path.exists():
        return {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("autoloop state file %s unreadable; treating as disabled", path)
        return {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}
    return data if isinstance(data, dict) else {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}


def _save_state(state: dict) -> None:
    paths.ensure_data_dir()
    tmp_path = paths.AUTOLOOP_PATH.with_name(paths.AUTOLOOP_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    tmp_path.replace(paths.AUTOLOOP_PATH)


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
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, name="server-autoloop-scheduler", daemon=True
    )
    _scheduler_thread.start()


def _scheduler_loop() -> None:
    """Run an enabled loop immediately, then wait between later cycles.

    First cycle runs at startup if enabled; subsequent cycles wait the full
    interval before running. The wakeup event allows immediate re-reads when
    settings change but does not bypass the interval between actual cycles.
    """
    processed_update = object()
    while True:
        state = _load_state()
        updated_at = state.get("updated_at")
        if updated_at != processed_update:
            processed_update = updated_at
            if state.get("enabled"):
                _spawn_cycle()
        timed_out = not _wakeup.wait(timeout=_interval_from(state) * 60.0)
        _wakeup.clear()
        if timed_out:
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
        # Crypto candidates first: the venue bridge only executes crypto, so a
        # cycle that runs out of time must have already seen its executable
        # candidates rather than spending the budget on advisory-only stocks.
        # Stable sort — score order is preserved inside each group.
        candidates.sort(key=lambda entry: str(entry.get("asset_type") or "crypto") != "crypto")
        summary["candidates"] = [str(entry["ticker"]) for entry in candidates]
        _cycle_timeout(deadline, "venue setup")
        with telemetry.span("venue_setup"):
            bridge = _build_bridge(config)
            derivatives = _build_derivatives_executor(config) if config.get("exec_derivatives") is True else None
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
        with telemetry.span("graph_build", ticker):
            graph = build_graph(config, _ANALYSTS_CRYPTO if asset_type == "crypto" else _ANALYSTS_STOCK)
        with telemetry.span("graph_analysis", ticker):
            propagate = getattr(graph, "propagate_with_telemetry", graph.propagate)
            kwargs: dict = {"asset_type": asset_type}
            parameters = inspect.signature(propagate).parameters
            if "telemetry" in parameters:
                kwargs["telemetry"] = telemetry
            if futures_context is not None and _accepts_market_snapshot(propagate):
                kwargs["market_snapshot"] = futures_context
            _, signal = propagate(ticker, _today_iso(), **kwargs)
            if deadline is not None:
                _cycle_timeout(deadline, f"candidate {ticker} analysis")
        result.signal = signal
        if asset_type != "crypto":
            result.reason = (
                "advisory only — signal from the stock pipeline; the venue "
                "bridge only executes crypto, so no order plan"
            )
            return result
        if derivatives is not None:
            with telemetry.span("futures_planning", ticker):
                _process_futures_candidate(
                    derivatives, ticker, signal, config, result,
                    context=futures_context, deadline=deadline, cancelled=cancelled,
                    execution_lock=execution_lock, telemetry=telemetry,
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
    }


def _candle_direction(candle: Any) -> str:
    if candle.close > candle.open:
        return "bullish"
    if candle.close < candle.open:
        return "bearish"
    return "doji"


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


def _accepts_market_snapshot(propagate: Callable) -> bool:
    """Whether this graph's propagate() accepts the futures snapshot kwarg.

    Signature inspection instead of sniffing the TypeError message: an
    unrelated TypeError that merely mentions the kwarg name must not
    silently drop the futures context.
    """
    try:
        return "market_snapshot" in inspect.signature(propagate).parameters
    except (TypeError, ValueError):
        return False


def _process_futures_candidate(
    derivatives: Any, ticker: str, signal: str, config: dict, result: DailyResult,
    *, context: dict | None = None, deadline: float | None = None,
    cancelled: threading.Event | None = None,
    execution_lock: threading.Lock | None = None,
    telemetry: CycleTelemetry | None = None,
) -> None:
    """Run futures candidates through a prevalidated closed candle pair."""
    telemetry = telemetry or CycleTelemetry()
    now = datetime.now(timezone.utc)
    context = context or _closed_futures_context(derivatives, ticker)
    if context is None:
        result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
        return
    candle_1h = context["one_hour"]
    candle_4h = context["four_hour"]
    direction = _direction(signal)
    confirmation_ok, confirmation_reason = _confirmation_state(context, signal)
    if not confirmation_ok:
        result.reason = "confirmation_missing"
        _record_futures_cycle_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            confirmation_reason=confirmation_reason,
            phase="confirmation",
        )
        return
    symbol = context["unified_symbol"]
    with telemetry.span("futures_mark_price", ticker):
        mark = _unified_mark_price(derivatives._bridge, symbol)
    with telemetry.span("protective_bracket", ticker):
        stop, take = _bracket_prices(ticker, direction=direction)
    if mark is None or stop is None or take is None:
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
        },
    ):
        result.reason = "duplicate futures signal skipped"
        return
    try:
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} futures planning")
        with telemetry.span("portfolio_sync", ticker):
            portfolio = derivatives._bridge.sync_portfolio()
        with telemetry.span("planning", ticker):
            plan = derivatives.plan_order(
                ticker, signal, portfolio=portfolio, price=mark,
                leverage=_auto_leverage(config), stop_loss=stop, take_profit=take,
            )
        result.plan = plan
        if deadline is not None:
            _cycle_timeout(deadline, f"candidate {ticker} execution")
        with telemetry.span("execution_policy", ticker):
            _handle_plan(
                derivatives,
                plan,
                portfolio,
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


def recover_pending_brackets() -> list[dict]:
    """Retry durable bracket tasks without recreating an entry order."""
    config = get_config()
    if config.get("exec_derivatives") is not True or not _env_flag("TRADINGAGENTS_EXEC_DERIVATIVES"):
        return []
    executor = _build_derivatives_executor(config)
    outcomes = []
    for task in job_store.pending_bracket_recoveries():
        task_id = task["task_id"]
        try:
            status = executor.recover_bracket(task_id, confirm=True)
        except Exception as exc:
            status = f"error:{type(exc).__name__}:{exc}"
        outcomes.append({"task_id": task_id, "status": status})
    return outcomes


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


def _auto_leverage(config: dict) -> float:
    """Leverage the cycle requests from the derivatives path: the configured
    ``risk_max_derivatives_leverage`` clamped to MAX_AUTO_LEVERAGE. A missing
    config entry means 1x — the same default as RiskLimits."""
    raw = config.get("risk_max_derivatives_leverage")
    configured = 1.0 if raw is None else float(raw)
    return min(configured, MAX_AUTO_LEVERAGE)


def _direction(signal: str) -> str:
    """Bracket direction for a 5-tier signal: Sell/Underweight open shorts on
    the derivatives path, so their bracket mirrors the long side."""
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
def get_autoloop_cycles(limit: int = 20):
    if limit < 1 or limit > 100:
        raise ApiError(400, "validation_error", '"limit" must be between 1 and 100')
    return {
        "generated_at": utc_now_iso(),
        "items": [_cycle_payload(item) for item in job_store.list_futures_cycles(limit=limit)],
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
