"""Point-in-time candle inputs for the simulation.

The engine is offline by construction: it never imports an exchange client
and never opens a network path. ``data.py`` only reshapes candles a caller
already holds — recorded cycle snapshots, exported fixtures, or any injected
sequence — and enforces the point-in-time contract: a run dated in the past
reads only candles closed at or before its as-of, never the forming bar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from tradingagents.dataflows.market_data import Candle, timeframe_ms

CandleSource = Callable[[str, str], list[Candle]]


def candle_from_payload(
    raw: Any, timeframe: str, symbol: str
) -> Candle | None:
    """Build one closed Candle from a recorded snapshot dict; None if unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        return Candle(
            symbol=str(raw.get("symbol") or symbol),
            timeframe=str(raw.get("timeframe") or timeframe),
            open_time=int(raw["open_time"]),
            close_time=int(raw["close_time"]),
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=float(raw.get("volume") or 0.0),
            is_closed=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


def candles_from_snapshot(snapshot: Any, symbol: str) -> list[Candle]:
    """The closed candles a recorded cycle snapshot carries, newest first."""
    if not isinstance(snapshot, dict):
        return []
    candles: list[Candle] = []
    for timeframe, key in (("4h", "four_hour"), ("1h", "one_hour")):
        candle = candle_from_payload(snapshot.get(key), timeframe, symbol)
        if candle is not None:
            candles.append(candle)
    return candles


def as_of_closed(candles: Iterable[Candle], as_of: datetime) -> list[Candle]:
    """Only candles fully closed at ``as_of``; naive datetimes are refused."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff_ms = int(as_of.astimezone(UTC).timestamp() * 1000)
    return [candle for candle in candles if candle.is_closed and candle.close_time <= cutoff_ms]


def sequential_candles(candles: Iterable[Candle], timeframe: str) -> list[Candle]:
    """One contiguous, ordered, closed series; gaps and mixes are refused.

    The engine needs a strictly increasing closed sequence in a single
    timeframe; anything else (overlaps, unordered rows, the forming bar,
    foreign timeframes) would silently distort the price path, so it raises.
    """
    duration = timeframe_ms(timeframe)
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    checked: list[Candle] = []
    for candle in ordered:
        if not candle.is_closed:
            raise ValueError("simulation only consumes closed candles")
        if candle.timeframe != timeframe:
            raise ValueError(f"mixed timeframes in series: {candle.timeframe!r}")
        if candle.close_time - candle.open_time != duration:
            raise ValueError("candle duration does not match the timeframe")
        if checked and candle.open_time <= checked[-1].open_time:
            raise ValueError("duplicate or overlapping candle")
        checked.append(candle)
    return checked
