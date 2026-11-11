"""Execution deadline, cancellation and operator authorization checks."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from server import (
    auto_modes,
)
from server.auto_modes import AutoMode
from tradingagents.dataflows.config import get_config
from tradingagents.execution.auto_authorization import (
    AuthorizationSnapshot,
)

CYCLE_TIMEOUT_SECONDS = 3600.0


class AutoModeDisabled(Exception):
    """Raised when an operator closes a mode before a new entry boundary."""


def cycle_timeout(deadline: float, stage: str) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(f"cycle timed out during {stage} after {CYCLE_TIMEOUT_SECONDS:.0f}s")


def execution_allowed(deadline: float | None, cancelled: threading.Event | None) -> bool:
    return (
        (deadline is None or time.monotonic() < deadline)
        and (cancelled is None or not cancelled.is_set())
    )


def cycle_writes_allowed(
    deadline: float | None, cancelled: threading.Event | None,
) -> bool:
    """Whether a candidate may still persist rows for its cycle."""
    return execution_allowed(deadline, cancelled)


def raise_if_execution_closed(
    deadline: float | None, cancelled: threading.Event | None, stage: str,
) -> None:
    if not execution_allowed(deadline, cancelled):
        raise TimeoutError(f"cycle execution closed during {stage}")


def execute_with_deadline(
    execute: Callable[[], Any],
    deadline: float | None,
    cancelled: threading.Event | None,
    execution_lock: threading.Lock | None,
    mode: AutoMode | None = None,
    authorization: AuthorizationSnapshot | None = None,
) -> Any:
    if execution_lock is None:
        raise_if_execution_closed(deadline, cancelled, "order submission")
        raise_if_mode_disabled(mode, "execution", authorization)
        return execute()
    with execution_lock:
        raise_if_execution_closed(deadline, cancelled, "order submission")
        raise_if_mode_disabled(mode, "execution", authorization)
        return execute()


def raise_if_mode_disabled(
    mode: AutoMode | None,
    stage: str,
    authorization: AuthorizationSnapshot | None = None,
) -> None:
    if mode is not None and not mode_is_enabled(mode, authorization):
        raise AutoModeDisabled(f"{mode.value} automation disabled before {stage}")


def mode_is_enabled(mode: AutoMode, authorization: AuthorizationSnapshot | None = None) -> bool:
    if authorization is not None:
        return authorization.allows(mode, live=True)
    return mode in auto_modes.enabled_modes(get_config())
