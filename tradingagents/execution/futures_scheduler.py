"""UTC boundary helpers for closed futures candles."""

from datetime import datetime

from tradingagents.auto_timeframes import last_closed_candle


# The Futures confirmation rule is deliberately fixed to this pair. The
# operator-selected Auto candle timeframe is a separate identity contract,
# and the scheduler cadence derives from it (server/autoloop_schedule.py).
def closed_candle_pair(as_of: datetime) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Return the latest closed 1h and 4h candles at ``as_of``."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("scheduler time must be timezone-aware")
    return last_closed_candle(as_of, "1h"), last_closed_candle(as_of, "4h")


__all__ = ["closed_candle_pair"]
