"""Background reconciliation for durable derivatives bracket tasks."""

from __future__ import annotations

import logging
import os
import threading

from server import job_store
from tradingagents.dataflows.config import get_config
from tradingagents.default_config import _BOOL_TRUE

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 30
_stop_event = threading.Event()
_monitor_thread: threading.Thread | None = None


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
    for task in tasks:
        try:
            if task["status"] == "pending" and can_recover:
                status = executor.recover_bracket(task["task_id"], confirm=True)
            else:
                status = executor.monitor_bracket(task["task_id"])
        except Exception as exc:
            status = f"error:{type(exc).__name__}:{exc}"
            logger.warning("bracket reconciliation failed for %s: %s", task["task_id"], exc)
        outcomes.append({"task_id": task["task_id"], "status": status})
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
