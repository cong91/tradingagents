"""Background reconciliation for durable derivatives bracket tasks."""

from __future__ import annotations

import logging
import os
import re
import threading

from server import job_store
from tradingagents.dataflows.config import get_config
from tradingagents.default_config import _BOOL_TRUE

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 30
_stop_event = threading.Event()
_monitor_thread: threading.Thread | None = None
_ENTRY_INTENT_PREFIX = "entry-intent:"
_SUCCESSFUL_ENTRY_STATUSES = {"completed", "already_reconciled"}
_LOCK_BUSY_STATUS = "lock_busy"


def _entry_status_reason(status: str) -> str:
    if not isinstance(status, str):
        return "unresolved"
    if status.startswith("error:"):
        parts = status.split(":", 2)
        if len(parts) > 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", parts[1]):
            return f"error:{parts[1]}"
        return "error"
    if re.fullmatch(r"[a-z][a-z0-9_]*", status):
        return status
    return "unresolved"


def _entry_error_reason(exc: Exception) -> str:
    from tradingagents.execution.futures_operation_lock import OperationLockBusyError

    if isinstance(exc, OperationLockBusyError):
        return _LOCK_BUSY_STATUS
    return f"error:{type(exc).__name__}"


def _log_entry_outcome(task: dict, status: str, *, exception: Exception | None = None) -> None:
    symbol_value = task.get("symbol")
    symbol = symbol_value if isinstance(symbol_value, str) and symbol_value.strip() else "unknown"
    if exception is not None:
        reason = _entry_error_reason(exception)
    else:
        reason = _entry_status_reason(status)
    if reason in _SUCCESSFUL_ENTRY_STATUSES:
        return
    if reason == _LOCK_BUSY_STATUS:
        logger.warning(
            "Entry reconciliation deferred for %s (%s); symbol lock is busy",
            symbol, reason,
        )
        return
    logger.error(
        "Entry reconciliation deferred for %s (%s); verify venue protection manually",
        symbol, reason,
    )


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    return bool(raw and raw.strip().lower() in _BOOL_TRUE)


def poll_active_brackets() -> list[dict]:
    tasks = job_store.active_bracket_recoveries()
    if not tasks:
        return []

    from tradingagents.execution.derivatives import DerivativesExecutor

    config = get_config()
    executor = DerivativesExecutor(config=config)
    can_recover = (
        config.get("exec_derivatives") is True
        and _env_flag("TRADINGAGENTS_EXEC_DERIVATIVES")
        and executor.effective_mode() == "live"
    )
    outcomes = []
    deferred_counts: dict[str, int] = {}
    for task in tasks:
        is_entry_intent = task["task_id"].startswith(_ENTRY_INTENT_PREFIX)
        exception = None
        try:
            if is_entry_intent:
                status = executor.monitor_bracket(task["task_id"])
            elif task["status"] == "pending" and can_recover:
                status = executor.recover_bracket(task["task_id"], confirm=True)
            else:
                status = executor.monitor_bracket(task["task_id"])
        except Exception as exc:
            status = f"error:{type(exc).__name__}:{exc}"
            if is_entry_intent:
                exception = exc
            else:
                logger.warning("bracket reconciliation failed for %s: %s", task["task_id"], exc)
        outcomes.append({"task_id": task["task_id"], "status": status})
        if is_entry_intent:
            _log_entry_outcome(task, status, exception=exception)
        elif status in {
            "account_identity_unknown", "account_identity_mismatch",
            "entry_reconciliation_required", "venue_mismatch",
        }:
            deferred_counts[status] = deferred_counts.get(status, 0) + 1
    for status, count in deferred_counts.items():
        logger.error(
            "Bracket reconciliation deferred (%s: %d); verify venue protection manually",
            status, count,
        )
    return outcomes


def _monitor_loop() -> None:
    while not _stop_event.is_set():
        try:
            poll_active_brackets()
        except Exception:
            logger.exception("bracket monitor poll failed")
        if _stop_event.wait(MONITOR_INTERVAL_SECONDS):
            break


def start() -> None:
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop, name="server-bracket-monitor", daemon=True,
    )
    _monitor_thread.start()


def stop() -> None:
    global _monitor_thread
    _stop_event.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=MONITOR_INTERVAL_SECONDS + 5)
        if not _monitor_thread.is_alive():
            _monitor_thread = None
