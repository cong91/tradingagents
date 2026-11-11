"""Own scheduler lifecycle and sequential market-cycle orchestration."""

from __future__ import annotations

import inspect
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from server import (
    audit as audit_log,
    auto_modes,
    autoloop_analysis,
    autoloop_candidates,
    autoloop_execution,
    autoloop_futures_context,
    control_store,
    daily as daily_mod,
    runs as runs_mod,
)
from server.auto_modes import AutoMode
from server.autoloop_cycles import ModeDispatch, dispatch_modes
from server.autoloop_telemetry import CycleTelemetry
from server.contract import utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.auto_authorization import (
    AuthorizationSnapshot,
    capture_process_config,
)
from tradingagents.execution.daily import DailyResult

MIN_INTERVAL_MINUTES = 15


MAX_INTERVAL_MINUTES = 1440


DEFAULT_INTERVAL_MINUTES = 1440


logger = logging.getLogger(__name__)


_cycle_lock = threading.Lock()


_execution_lock = threading.Lock()


_state_lock = threading.Lock()


_scheduler_start_lock = threading.Lock()


_cycle_thread: threading.Thread | None = None  # last spawned cycle worker


_scheduler_thread: threading.Thread | None = None


_scheduler_stop = threading.Event()


_wakeup = threading.Event()  # set on every state write so the loop re-reads at once


_last_cycle: dict | None = None


_last_telemetry: CycleTelemetry | None = None


_last_cycle_lock = threading.Lock()


def gates() -> dict:
    """Current market-specific automation modes."""
    enabled = auto_modes.enabled_modes(get_config())
    modes = {
        "auto_spot": AutoMode.SPOT in enabled,
        "auto_futures": AutoMode.FUTURES in enabled,
    }
    return {**modes, "any_auto_enabled": any(modes.values())}


def _load_persisted_state() -> dict | None:
    try:
        data = control_store.load_autoloop_state()
    except Exception:  # an unreadable store must not arm the loop
        logger.warning("autoloop state unreadable; treating as disabled", exc_info=True)
        return None
    return data if isinstance(data, dict) else {}


def _load_state() -> dict:
    """Return persisted scheduler settings with mode-derived enablement.

    Auto Spot/Auto Futures are the source of truth for whether the scheduler
    may run. The persisted bit is normalized on read so stale state cannot arm
    the loop after both modes are closed.
    """
    data = _load_persisted_state()
    if data is None:
        return {"enabled": False, "interval_minutes": DEFAULT_INTERVAL_MINUTES}
    normalized = dict(data)
    normalized["enabled"] = bool(auto_modes.enabled_modes(get_config()))
    normalized.setdefault("interval_minutes", DEFAULT_INTERVAL_MINUTES)
    return normalized


def _save_state(state: dict) -> None:
    control_store.save_autoloop_state(state)


def sync_mode_state() -> dict:
    """Persist mode-derived scheduler state and wake the scheduler.

    Operator mode writes call this after applying their config. A mode change
    always wakes the scheduler so a closed mode is observed promptly; the
    durable ``enabled``/``updated_at`` pair changes only when the derived
    scheduler state changes.
    """
    with _state_lock:
        persisted = control_store.load_autoloop_state()
        persisted = persisted if isinstance(persisted, dict) else {}
        derived_enabled = bool(auto_modes.enabled_modes(get_config()))
        if persisted.get("enabled") is not derived_enabled:
            persisted = {
                **persisted,
                "enabled": derived_enabled,
                "interval_minutes": _interval_from(persisted),
                "updated_at": utc_now_iso(),
            }
            _save_state(persisted)
    if derived_enabled:
        ensure_started()
    _wakeup.set()
    return {**persisted, "enabled": derived_enabled}


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


def ensure_started() -> None:
    """Start the scheduler daemon once; idempotent — every enable calls it."""
    global _scheduler_thread
    with _scheduler_start_lock:
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
    mode: AutoMode | None = None,
    authorization: AuthorizationSnapshot | None = None,
) -> DailyResult:
    """Run one candidate off-cycle so an LLM stall cannot hold the lock."""
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(autoloop_analysis._process_candidate(
                bridge, candidate, config, build_graph, derivatives,
                deadline=deadline, cancelled=cancelled,
                execution_lock=execution_lock, telemetry=telemetry, mode=mode,
                authorization=authorization,
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
            f"stage {stage}{node_context} after {autoloop_execution.CYCLE_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _run_cycle(graph_builder: Callable | None = None) -> list[DailyResult]:
    """Dispatch the enabled markets sequentially under one cycle deadline."""
    started_at = utc_now_iso()
    deadline = time.monotonic() + autoloop_execution.CYCLE_TIMEOUT_SECONDS
    build_graph = graph_builder if graph_builder is not None else _default_graph_builder

    busy_run = runs_mod.active_run_id()
    busy_job = None if busy_run is not None else daily_mod.active_job_id()
    if busy_run is not None or busy_job is not None:
        occupied = f"run {busy_run}" if busy_run is not None else f"daily job {busy_job}"
        audit_log.record("autoloop_cycle_skipped", reason=f"another worker is active: {occupied}")
        _set_last_cycle({
            "started_at": started_at, "finished_at": utc_now_iso(),
            "status": "skipped_busy", "reason": occupied,
            "candidates": [], "results": [], "modes": {}, "error": None,
        })
        return []

    audit_log.record("autoloop_cycle_started")
    config, authorization = capture_process_config()
    enabled = auto_modes.enabled_modes(config)
    summary: dict = {
        "started_at": started_at, "finished_at": None, "status": "running",
        "candidates": [], "results": [], "modes": {}, "error": None,
    }
    for mode in AutoMode:
        summary["modes"][_mode_key(mode)] = {
            "mode": mode.value,
            "status": "pending" if mode in enabled else "disabled",
            "candidates": [], "results": [],
            "started_at": None, "finished_at": None,
            "telemetry": None,
        }
    results: list[DailyResult] = []

    def run_mode(mode: AutoMode, shared_deadline: float) -> ModeDispatch:
        mode_config = dict(config)
        mode_config["auto_spot_enabled"] = mode is AutoMode.SPOT
        mode_config["auto_futures_enabled"] = mode is AutoMode.FUTURES
        return _run_mode_cycle(
            mode, mode_config, build_graph, shared_deadline, threading.Event(),
            summary=summary, results=results, authorization=authorization,
        )

    try:
        dispatch_modes(
            enabled,
            deadline=deadline,
            is_enabled=lambda mode: autoloop_execution.mode_is_enabled(mode, authorization),
            run_mode=run_mode,
        )
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        audit_log.record("autoloop_cycle_failed", error=summary["error"])

    for mode in AutoMode:
        mode_summary = summary["modes"][_mode_key(mode)]
        if mode_summary["status"] == "pending":
            if autoloop_execution.mode_is_enabled(mode, authorization) and time.monotonic() >= deadline:
                mode_summary["status"] = "timeout"
                mode_summary["error"] = "cycle deadline expired before screening"
                summary["error"] = summary["error"] or mode_summary["error"]
            else:
                mode_summary["status"] = "disabled"
            mode_summary["finished_at"] = utc_now_iso()
    mode_statuses = {item["status"] for item in summary["modes"].values()}
    if summary["status"] == "running":
        if "timeout" in mode_statuses or time.monotonic() >= deadline:
            summary["status"] = "timeout"
        elif "failed" in mode_statuses:
            summary["status"] = "failed"
        else:
            summary["status"] = "completed"
    summary["finished_at"] = utc_now_iso()
    _set_last_cycle(summary)
    if summary["status"] == "completed":
        audit_log.record(
            "autoloop_cycle_completed",
            candidates=summary["candidates"], results=summary["results"],
            modes=summary["modes"],
        )
    elif summary["status"] == "timeout":
        audit_log.record("autoloop_cycle_timeout", error=summary.get("error"))
    elif summary["status"] == "failed" and summary.get("error") is None:
        audit_log.record("autoloop_cycle_failed", modes=summary["modes"])
    return results


def _run_mode_cycle(
    mode: AutoMode,
    config: dict,
    build_graph: Callable,
    deadline: float,
    cancelled: threading.Event,
    *,
    summary: dict,
    results: list[DailyResult],
    authorization: AuthorizationSnapshot,
) -> ModeDispatch:
    """Screen and run one market without extending the enclosing deadline."""
    mode_name = _mode_key(mode)
    mode_summary = summary["modes"][mode_name]
    mode_summary["status"] = "running"
    mode_summary["started_at"] = utc_now_iso()

    def publish_telemetry(snapshot: dict) -> None:
        mode_summary["telemetry"] = snapshot
        summary["telemetry"] = snapshot
        summary["active_mode"] = mode_name
        _set_last_cycle(dict(summary, results=list(summary["results"])), telemetry)

    telemetry = CycleTelemetry(on_change=publish_telemetry)
    publish_telemetry(telemetry.snapshot())
    mode_results: list[DailyResult] = []
    try:
        from tradingagents.dataflows.screening import (
            MAX_PRICE_MISMATCH,
            crypto_universe,
            price_mismatch,
            screen_universe,
            stock_universe,
        )

        futures_mode = mode is AutoMode.FUTURES
        derivatives = None
        autoloop_execution.raise_if_mode_disabled(mode, "venue setup", authorization)
        if futures_mode:
            autoloop_execution.cycle_timeout(deadline, "venue setup")
            with telemetry.span("venue_setup"):
                derivatives = _call_authorized_builder(
                    _build_derivatives_executor, config, authorization,
                )

        crypto_fn: Callable = crypto_universe
        screen_fn: Callable = screen_universe
        mismatched: dict[str, dict] = {}
        if derivatives is not None:
            contracts: dict[str, dict] = {}
            crypto_fn = autoloop_candidates._futures_crypto_universe(derivatives, contracts, mode_summary)
            screen_fn = autoloop_candidates._pinning_screen(
                screen_universe, derivatives, contracts, mismatched,
                max_mismatch=MAX_PRICE_MISMATCH, price_mismatch=price_mismatch,
            )

        with telemetry.span("screening"):
            autoloop_execution.raise_if_mode_disabled(mode, "screening", authorization)
            autoloop_execution.cycle_timeout(deadline, "screening")
            remaining = deadline - time.monotonic()
            crypto, stocks, candidates = autoloop_candidates._screen_with_timeout(
                crypto_fn, stock_universe, screen_fn, config, autoloop_futures_context._today_iso(),
                telemetry=telemetry, futures_mode=futures_mode,
                timeout_seconds=remaining, mode=mode, authorization=authorization,
            )
            autoloop_execution.cycle_timeout(deadline, "screening")
        autoloop_execution.raise_if_mode_disabled(mode, "venue setup", authorization)
        autoloop_execution.cycle_timeout(deadline, "venue setup")
        with telemetry.span("venue_setup"):
            bridge = _call_authorized_builder(_build_bridge, config, authorization)

        autoloop_execution.raise_if_mode_disabled(mode, "candidate preflight", authorization)
        autoloop_execution.cycle_timeout(deadline, "candidate preflight")
        candidates, skipped, candidate_selection = autoloop_candidates._select_cycle_candidates(
            candidates, derivatives, limit=autoloop_candidates.AUTO_TOP_N, telemetry=telemetry,
        )
        if futures_mode:
            with telemetry.span("screening:refill"):
                crypto, stocks, candidates, skipped, candidate_selection = (
                    autoloop_candidates._refill_futures_candidates(
                        crypto_fn, stock_universe, screen_fn, config, autoloop_futures_context._today_iso(),
                        derivatives, limit=autoloop_candidates.AUTO_TOP_N, selected=candidates,
                        skipped=skipped, selection=candidate_selection, crypto=crypto,
                        inspected=autoloop_candidates.AUTO_FUTURES_SCREEN_POOL, deadline=deadline,
                        telemetry=telemetry, mode=mode, authorization=authorization,
                    )
                )

        universe = {"crypto": len(crypto), "stock": len(stocks)}
        if candidate_selection is not None and candidate_selection.get("discovery_inspected"):
            universe["crypto"] = int(candidate_selection["discovery_inspected"])
        mode_summary["universe"] = universe
        aggregate_universe = summary.setdefault("universe", {"crypto": 0, "stock": 0})
        for asset, count in universe.items():
            aggregate_universe[asset] += count
        if mismatched:
            skipped = list(mismatched.values()) + skipped
        if skipped:
            mode_summary["skipped"] = skipped
            summary.setdefault("skipped", []).extend(skipped)
            audit_log.record("autoloop_candidates_skipped", mode=mode.value, skipped=skipped)
        if candidate_selection is not None:
            mode_summary["candidate_selection"] = candidate_selection
            summary["candidate_selection"] = candidate_selection

        ticker_list = [str(entry["ticker"]) for entry in candidates]
        mode_summary["candidates"] = ticker_list
        summary["candidates"].extend(ticker_list)
        for candidate in candidates:
            if not autoloop_execution.mode_is_enabled(mode, authorization):
                mode_summary["status"] = "disabled"
                break
            autoloop_execution.cycle_timeout(deadline, f"candidate {candidate.get('ticker')}")
            try:
                result = _run_candidate_with_timeout(
                    bridge, candidate, config, build_graph, derivatives, deadline, cancelled,
                    _execution_lock, telemetry, mode=mode, authorization=authorization,
                )
            except autoloop_execution.AutoModeDisabled:
                mode_summary["status"] = "disabled"
                break
            mode_results.append(result)
            results.append(result)
            result_summary = _result_summary(result)
            mode_summary["results"].append(result_summary)
            summary["results"].append(result_summary)
        if mode_summary["status"] == "running":
            mode_summary["status"] = "completed"
    except autoloop_execution.AutoModeDisabled as exc:
        mode_summary["status"] = "disabled"
        mode_summary["error"] = str(exc)
    except TimeoutError as exc:
        cancelled.set()
        telemetry.close()
        mode_summary["status"] = "timeout"
        mode_summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["error"] = mode_summary["error"]
        audit_log.record(
            "autoloop_cycle_timeout", mode=mode.value,
            error=mode_summary["error"], telemetry=telemetry.snapshot(),
        )
    except Exception as exc:
        mode_summary["status"] = "failed"
        mode_summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["error"] = mode_summary["error"]
        audit_log.record("autoloop_cycle_failed", mode=mode.value, error=mode_summary["error"])
    finally:
        telemetry.close()
        mode_summary["telemetry"] = telemetry.snapshot()
        mode_summary["finished_at"] = utc_now_iso()
        publish_telemetry(mode_summary["telemetry"])
    return ModeDispatch(mode, mode_results, dict(mode_summary))


def _mode_key(mode: AutoMode) -> str:
    return "auto_spot" if mode is AutoMode.SPOT else "auto_futures"


def _default_graph_builder(config: dict, analysts: tuple[str, ...]) -> Any:
    # Lazy like server/daily._analyze: the graph import chain pulls LangChain.
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(config=config, selected_analysts=analysts)


def _call_authorized_builder(
    builder: Callable, config: dict, authorization: AuthorizationSnapshot,
) -> Any:
    """Pass the cycle token to builders that accept it, including test doubles."""
    try:
        parameters = inspect.signature(builder).parameters
    except (TypeError, ValueError):
        parameters = {}
    parameter = parameters.get("authorization")
    if parameter is not None and parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
        return builder(config, authorization)
    if parameter is not None or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        return builder(config, authorization=authorization)
    return builder(config)


def _build_bridge(
    config: dict, *, authorization: AuthorizationSnapshot | None = None,
) -> Any:
    """Build the spot venue with the cycle's entry authorization."""
    from tradingagents.execution.bridge import ExchangeBridge

    return ExchangeBridge(config=config, authorization=authorization)


def _build_derivatives_executor(
    config: dict, *, authorization: AuthorizationSnapshot | None = None,
) -> Any:
    """Build the futures executor with the cycle's entry authorization."""
    from tradingagents.execution.derivatives import DerivativesExecutor

    return DerivativesExecutor(config=config, authorization=authorization)


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
