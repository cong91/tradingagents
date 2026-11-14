"""Due-ledger scheduling derived from each Auto mode's selected candle timeframe.

The scheduler no longer keeps a manual scan interval: each enabled mode is due
when the candle of ITS OWN selected timeframe has closed past the boundary the
mode last ran on. Pure functions only — persistence stays in ``server.autoloop``
under the state lock, so this module can be tested without a store.

Semantics pinned here:

- A mode is due when no marker exists for its configured timeframe (first run
  after boot marks the latest closed candle) or the latest closed candle open
  is newer than the last dispatched open for that timeframe. Markers are kept
  per ``(mode, timeframe)``, so switching 4h→1m→4h does not replay the 4h
  candle already served; only a genuinely newer closed candle is due.
- Missed boundaries coalesce to the latest closed candle: a long cycle never
  replays a backlog, it runs once for the newest candle. A boundary consumed
  while the venue has not yet published the closed bar is not retried
  automatically (at-most-once); a manual run-now is the explicit retry.
- Only ONE mode is scheduled per cycle, the one whose unserved candle has been
  waiting longest (smallest latest closed open), with a stable alternating
  tie-break so equal boundaries do not always favor the same mode. Each mode
  keeps its own full cycle budget; the other enabled mode stays ``not_due``.
- The fixed 1h+4h confirmation pair is a separate contract
  (``tradingagents/execution/futures_scheduler.py``) and is never used as a
  scheduling boundary here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.auto_modes import MODE_KEYS, AutoMode
from tradingagents.auto_timeframes import (
    DEFAULT_AUTO_TIMEFRAME,
    last_closed_candle,
    validate_auto_timeframe,
)

TIMEFRAME_CONFIG_KEYS: dict[AutoMode, str] = {
    AutoMode.SPOT: "auto_spot_timeframe",
    AutoMode.FUTURES: "auto_futures_timeframe",
}

MODE_SCHEDULE_KEY = "mode_schedule"

# The scheduler wakes at candle boundaries; a wall-clock wait is capped so a
# stale boundary or a clock correction self-heals instead of sleeping for hours.
MAX_WAKE_SECONDS = 3600.0
# The candle closing exactly at ``now`` is already closed; the small buffer
# absorbs timer jitter so a boundary tick never lands a fraction early.
_WAKE_BUFFER_SECONDS = 0.25


def mode_key(mode: AutoMode) -> str:
    """The marker key inside the persisted ``mode_schedule`` payload."""
    return f"auto_{mode.value}"


def selected_timeframe(config: dict[str, Any], mode: AutoMode) -> str:
    """The mode's configured Auto candle timeframe, validated."""
    return validate_auto_timeframe(
        config.get(TIMEFRAME_CONFIG_KEYS[mode], DEFAULT_AUTO_TIMEFRAME)
    )


def _closed_open_ms(now: datetime, timeframe: str) -> int:
    opened, _closed = last_closed_candle(now, timeframe)
    return int(opened.timestamp() * 1000)


def due_modes(
    config: dict[str, Any], markers: dict[str, Any], now: datetime,
) -> dict[AutoMode, dict[str, Any]]:
    """Enabled modes whose selected candle advanced past their last dispatch.

    ``markers`` maps ``auto_spot``/``auto_futures`` to ``{timeframe: open_ms}``.
    Returns ``{mode: {"timeframe": ..., "candle_open": <epoch ms>}}``.
    """
    due: dict[AutoMode, dict[str, Any]] = {}
    for mode in AutoMode:
        if config.get(MODE_KEYS[mode]) is not True:
            continue
        timeframe = selected_timeframe(config, mode)
        latest = _closed_open_ms(now, timeframe)
        served = markers.get(mode_key(mode))
        served_open = served.get(timeframe) if isinstance(served, dict) else None
        if (
            not isinstance(served_open, int)
            or isinstance(served_open, bool)
            or served_open < latest
        ):
            due[mode] = {"timeframe": timeframe, "candle_open": latest}
    return due


def pick_scheduled_mode(
    due: dict[AutoMode, dict[str, Any]], last_scheduled: str | None,
) -> AutoMode | None:
    """One mode per cycle: the longest-waiting unserved candle first.

    ``candle_open`` ascending is the wait order (an older candle has waited
    longer); equal boundaries alternate against ``last_scheduled`` so a 1m and
    a 1h mode closing together do not starve either.
    """
    if not due:
        return None
    ordered = sorted(
        due.items(),
        key=lambda item: (
            item[1]["candle_open"],
            # Stable alternating tie-break within the same boundary.
            0 if mode_key(item[0]) != last_scheduled else 1,
            item[0].value,
        ),
    )
    return ordered[0][0]


def seconds_until_next_boundary(
    config: dict[str, Any], now: datetime,
) -> float:
    """Seconds until the earliest selected-candle boundary of the enabled modes.

    Returns :data:`MAX_WAKE_SECONDS` when no mode is enabled (an idle poll so a
    later config change through the wakeup event is observed promptly).
    """
    waits = []
    for mode in AutoMode:
        if config.get(MODE_KEYS[mode]) is not True:
            continue
        opened, closed = last_closed_candle(now, selected_timeframe(config, mode))
        del opened
        waits.append(max(0.0, (closed - now).total_seconds()))
    if not waits:
        return MAX_WAKE_SECONDS
    return min(waits)


def wake_wait_seconds(config: dict[str, Any], now: datetime) -> float:
    """Bounded scheduler sleep: the next boundary plus jitter buffer."""
    return min(
        MAX_WAKE_SECONDS,
        seconds_until_next_boundary(config, now) + _WAKE_BUFFER_SECONDS,
    )


__all__ = [
    "MAX_WAKE_SECONDS",
    "MODE_SCHEDULE_KEY",
    "TIMEFRAME_CONFIG_KEYS",
    "due_modes",
    "mode_key",
    "pick_scheduled_mode",
    "seconds_until_next_boundary",
    "selected_timeframe",
    "wake_wait_seconds",
]
