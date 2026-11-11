"""Closed-candle market data adapter for crypto research paths."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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


class ClosedCandlePaginationError(ValueError):
    """The venue returned an unusable paginated closed-candle history."""


def timeframe_ms(timeframe: str) -> int:
    units = {"1h": 3_600_000, "4h": 14_400_000}
    try:
        return units[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported crypto timeframe: {timeframe}") from exc


def _coerce_limit(value: Any, timeframe: str) -> int | None:
    if callable(value):
        try:
            value = value(timeframe)
        except TypeError:
            value = value()
    if isinstance(value, Mapping):
        value = value.get(
            timeframe, value.get("default", value.get("max", value.get("limit")))
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or int(value) != value or value <= 0:
        return None
    return int(value)


def _venue_ohlcv_limit(exchange: Any, timeframe: str) -> int | None:
    """Read an optional per-venue OHLCV page cap from common adapter shapes."""
    for name in (
        "max_ohlcv_limit",
        "ohlcv_max_limit",
        "max_fetch_ohlcv",
        "fetch_ohlcv_limit",
    ):
        value = getattr(exchange, name, None)
        limit = _coerce_limit(value, timeframe)
        if limit is not None:
            return limit

    for container_name in ("limits", "options"):
        container = getattr(exchange, container_name, None)
        if not isinstance(container, Mapping):
            continue
        for key in ("fetchOHLCV", "fetch_ohlcv", "ohlcv"):
            if key not in container:
                continue
            limit = _coerce_limit(container[key], timeframe)
            if limit is not None:
                return limit
    return None


def _fetch_ohlcv_page(
    exchange: Any,
    symbol: str,
    timeframe: str,
    *,
    since: int,
    limit: int,
) -> list[Any]:
    try:
        return exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=limit,
        )
    except TypeError:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def _row_content(row: Any) -> tuple[Any, ...]:
    return tuple(row[:6])


def _parse_closed_row(
    row: Any,
    *,
    symbol: str,
    timeframe: str,
    duration: int,
    cutoff: int,
) -> tuple[int, Candle] | None:
    if len(row) < 6:
        return None
    open_time = int(row[0])
    close_time = open_time + duration
    if close_time > cutoff:
        return None
    return open_time, Candle(
        symbol, timeframe, open_time, close_time, *map(float, row[1:6]), True,
    )


def _single_closed_candles(
    exchange: Any,
    symbol: str,
    timeframe: str,
    *,
    as_of: datetime,
    cutoff: int,
    duration: int,
    limit: int,
) -> list[Candle]:
    query_since = max(0, cutoff - duration * limit)
    try:
        rows = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=query_since, limit=limit,
        )
    except TypeError:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    candles = []
    for row in rows:
        parsed = _parse_closed_row(
            row, symbol=symbol, timeframe=timeframe, duration=duration, cutoff=cutoff,
        )
        if parsed is not None:
            candles.append(parsed[1])
    return candles


def _paginated_closed_candles(
    exchange: Any,
    symbol: str,
    timeframe: str,
    *,
    cutoff: int,
    duration: int,
    limit: int,
    venue_limit: int,
) -> list[Candle]:
    closed_cutoff = cutoff - cutoff % duration
    query_since = max(0, closed_cutoff - duration * limit)
    max_pages = max(2, math.ceil(limit / venue_limit) + 2)
    by_open_time: dict[int, tuple[tuple[Any, ...], Candle]] = {}
    since = query_since
    previous_page_max: int | None = None

    for _page_number in range(max_pages):
        rows = _fetch_ohlcv_page(
            exchange, symbol, timeframe, since=since, limit=venue_limit,
        )
        if not rows:
            break

        page_open_times: set[int] = set()
        page_max: int | None = None
        new_timestamps = 0
        duplicate_outside_boundary = False
        for row in rows:
            if len(row) < 6:
                continue
            open_time = int(row[0])
            if open_time in page_open_times:
                raise ClosedCandlePaginationError(
                    f"duplicate candle open_time {open_time} within page"
                )
            page_open_times.add(open_time)
            page_max = open_time if page_max is None else max(page_max, open_time)
            content = _row_content(row)
            existing = by_open_time.get(open_time)
            if existing is not None:
                previous_content, _candle = existing
                if previous_content != content:
                    raise ClosedCandlePaginationError(
                        f"conflicting duplicate candle open_time {open_time}"
                    )
                if previous_page_max != open_time:
                    duplicate_outside_boundary = True
                continue
            parsed = _parse_closed_row(
                row, symbol=symbol, timeframe=timeframe, duration=duration, cutoff=cutoff,
            )
            if parsed is None:
                continue
            by_open_time[open_time] = (content, parsed[1])
            new_timestamps += 1

        if page_max is None or page_max < since:
            raise ClosedCandlePaginationError(
                "no progress while paginating closed candles"
            )
        next_since = page_max + duration
        if next_since <= since or new_timestamps == 0:
            raise ClosedCandlePaginationError(
                "no progress while paginating closed candles"
            )
        if duplicate_outside_boundary:
            raise ClosedCandlePaginationError(
                "duplicate candle open_time outside page boundary"
            )
        previous_page_max = page_max
        since = next_since

        if len(by_open_time) >= limit:
            break
        if page_max >= cutoff and len(by_open_time) < limit:
            break

    candles = [by_open_time[open_time][1] for open_time in sorted(by_open_time)]
    for previous, current in zip(candles, candles[1:], strict=False):
        expected = previous.open_time + duration
        if current.open_time != expected:
            raise ClosedCandlePaginationError(
                f"candle gap: expected open_time {expected}, got {current.open_time}"
            )
    if len(candles) < limit:
        raise ClosedCandlePaginationError(
            f"insufficient closed candle history: received {len(candles)}, requested {limit}"
        )
    candles = candles[-limit:]
    return candles


def fetch_closed_candles(exchange: Any, symbol: str, timeframe: str,
                         *, as_of: datetime, limit: int = 100) -> list[Candle]:
    """Fetch only candles fully closed at ``as_of``; never use the forming bar."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if limit < 1:
        raise ValueError("limit must be positive")
    cutoff = int(as_of.astimezone(UTC).timestamp() * 1000)
    duration = timeframe_ms(timeframe)
    venue_limit = _venue_ohlcv_limit(exchange, timeframe)
    if venue_limit is None or limit <= venue_limit:
        return _single_closed_candles(
            exchange, symbol, timeframe, as_of=as_of, cutoff=cutoff,
            duration=duration, limit=limit,
        )
    return _paginated_closed_candles(
        exchange, symbol, timeframe, cutoff=cutoff, duration=duration,
        limit=limit, venue_limit=venue_limit,
    )


__all__ = [
    "Candle",
    "ClosedCandlePaginationError",
    "fetch_closed_candles",
    "timeframe_ms",
]
