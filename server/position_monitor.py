"""Background poller for durable futures profit-protection state."""

from __future__ import annotations

import logging
import os
import threading

from server import job_store
from tradingagents.dataflows.config import get_config
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution.futures_protection import manage_active_positions

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 30
_stop_event = threading.Event()
_monitor_thread: threading.Thread | None = None


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    return bool(raw and raw.strip().lower() in _BOOL_TRUE)


def _build_executor(config: dict):
    from tradingagents.execution.derivatives import DerivativesExecutor

    return DerivativesExecutor(config=config)


def poll_active_positions() -> list[dict]:
    states = job_store.active_position_states()
    if not states:
        return []
    config = get_config()
    allow_mutation = (
        config.get("exec_derivatives") is True
        and _env_flag("TRADINGAGENTS_EXEC_DERIVATIVES")
        and config.get("exec_live") is True
        and _env_flag("TRADINGAGENTS_EXEC_LIVE")
        and config.get("risk_derivatives_profit_protection_enabled") is True
    )
    if not allow_mutation:
        return [
            {"position_key": state["position_key"], "status": "mutation_gated"}
            for state in states
        ]
    executor = _build_executor(config)
    try:
        return manage_active_positions(
            executor._bridge, allow_mutation=True, config=config,
        )
    except Exception as exc:
        logger.exception("futures position protection poll failed")
        return [
            {"position_key": state["position_key"], "status": f"error:{type(exc).__name__}:{exc}"}
            for state in states
        ]


def _monitor_loop() -> None:
    while not _stop_event.is_set():
        try:
            poll_active_positions()
        except Exception:
            logger.exception("futures position monitor poll failed")
        if _stop_event.wait(MONITOR_INTERVAL_SECONDS):
            break


def start() -> None:
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop, name="server-position-monitor", daemon=True,
    )
    _monitor_thread.start()


def stop() -> None:
    global _monitor_thread
    _stop_event.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=MONITOR_INTERVAL_SECONDS + 5)
        if not _monitor_thread.is_alive():
            _monitor_thread = None
