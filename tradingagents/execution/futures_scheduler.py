"""UTC boundary helpers for closed futures candles."""

from datetime import datetime, timedelta, timezone

_TIMEFRAME = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}


def floor_boundary(value: datetime, timeframe: str) -> datetime:
    """Return the UTC opening time of the containing candle."""
    if value.tzinfo is None:
        raise ValueError("boundary time must be timezone-aware")
    try:
        duration = _TIMEFRAME[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported futures timeframe: {timeframe}") from exc
    utc = value.astimezone(timezone.utc)
    seconds = int(duration.total_seconds())
    epoch = int(utc.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, timezone.utc)


def last_closed_boundary(value: datetime, timeframe: str) -> tuple[datetime, datetime]:
    """Return (open, close) for the latest candle closed at value."""
    boundary = floor_boundary(value, timeframe)
    duration = _TIMEFRAME[timeframe]
    return boundary - duration, boundary


def closed_candle_pair(as_of: datetime) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Return the latest closed 1h and 4h candles at ``as_of``."""
    if as_of.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    return last_closed_boundary(as_of, "1h"), last_closed_boundary(as_of, "4h")
