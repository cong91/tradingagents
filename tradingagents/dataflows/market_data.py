"""Closed-candle market data adapter for crypto research paths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


def timeframe_ms(timeframe: str) -> int:
    units = {"1h": 3_600_000, "4h": 14_400_000}
    try:
        return units[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported crypto timeframe: {timeframe}") from exc


def fetch_closed_candles(exchange: Any, symbol: str, timeframe: str,
                         *, as_of: datetime, limit: int = 100) -> list[Candle]:
    """Fetch only candles fully closed at ``as_of``; never use the forming bar."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = int(as_of.astimezone(timezone.utc).timestamp() * 1000)
    duration = timeframe_ms(timeframe)
    query_since = max(0, cutoff - duration * limit)
    try:
        rows = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=query_since, limit=limit,
        )
    except TypeError:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    candles = []
    for row in rows:
        if len(row) < 6:
            continue
        open_time = int(row[0])
        close_time = open_time + duration
        if close_time > cutoff:
            continue
        candles.append(Candle(symbol, timeframe, open_time, close_time, *map(float, row[1:6]), True))
    return candles
