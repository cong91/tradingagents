"""Sequential dispatch for enabled autoloop market modes."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from server.auto_modes import AutoMode


@dataclass(frozen=True)
class ModeDispatch:
    """One mode's result as returned by the mode runner."""

    mode: AutoMode
    results: list
    summary: dict


def dispatch_modes(
    modes: Iterable[AutoMode],
    *,
    deadline: float,
    is_enabled: Callable[[AutoMode], bool],
    run_mode: Callable[[AutoMode, float], ModeDispatch],
) -> list[ModeDispatch]:
    """Run enabled modes in a stable order under one caller-owned deadline.

    The cycle lock is owned by ``server.autoloop``. This function only serializes
    the market-specific runners and re-checks authorization before each one so
    a mode disabled between spot and futures cannot start later work.
    """
    ordered = (AutoMode.SPOT, AutoMode.FUTURES)
    requested = set(modes)
    dispatches: list[ModeDispatch] = []
    for mode in ordered:
        if mode not in requested or time.monotonic() >= deadline:
            continue
        if not is_enabled(mode):
            continue
        dispatches.append(run_mode(mode, deadline))
    return dispatches


__all__ = ["ModeDispatch", "dispatch_modes"]
