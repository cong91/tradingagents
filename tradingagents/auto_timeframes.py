"""Candle timeframe contract shared by Auto, market data and forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

AUTO_TIMEFRAMES = ("1m", "15m", "1h", "4h")
TIMEFRAME_MS = {
    "1m": 60_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}
DEFAULT_AUTO_TIMEFRAME = "4h"


def validate_auto_timeframe(value: object) -> str:
    """Return one supported Auto candle timeframe or refuse the value."""
    if not isinstance(value, str) or value not in AUTO_TIMEFRAMES:
        raise ValueError(
            f"unsupported Auto timeframe {value!r}; choose one of {AUTO_TIMEFRAMES}"
        )
    return value


def timeframe_ms(timeframe: str) -> int:
    """Duration of a supported UTC-aligned candle in milliseconds."""
    try:
        return TIMEFRAME_MS[timeframe]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported crypto timeframe: {timeframe}") from exc


def floor_time(value: datetime, timeframe: str) -> datetime:
    """UTC opening boundary of the candle containing an aware timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candle boundary time must be timezone-aware")
    duration = timedelta(milliseconds=timeframe_ms(timeframe))
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((value.astimezone(UTC) - epoch) // duration) * duration


def last_closed_candle(value: datetime, timeframe: str) -> tuple[datetime, datetime]:
    """Return (open, close) for the latest candle closed at ``value``."""
    boundary = floor_time(value, timeframe)
    duration = timedelta(milliseconds=timeframe_ms(timeframe))
    return boundary - duration, boundary


__all__ = [
    "AUTO_TIMEFRAMES",
    "DEFAULT_AUTO_TIMEFRAME",
    "TIMEFRAME_MS",
    "floor_time",
    "last_closed_candle",
    "timeframe_ms",
    "validate_auto_timeframe",
]
