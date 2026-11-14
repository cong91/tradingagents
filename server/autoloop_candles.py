"""Read and validate the selected Auto candle at a single UTC cutoff."""

from __future__ import annotations

import math
from contextvars import Context
from datetime import UTC, datetime
from typing import Any

from tradingagents.auto_timeframes import last_closed_candle, timeframe_ms, validate_auto_timeframe
from tradingagents.dataflows.config import config_key_revision, get_config
from tradingagents.dataflows.market_data import Candle, fetch_closed_candles


def selected_candle(exchange: Any, symbol: str, timeframe: object, *, as_of: datetime) -> Candle:
    selected = validate_auto_timeframe(timeframe)
    candles = fetch_closed_candles(exchange, symbol, selected, as_of=as_of, limit=1)
    if len(candles) != 1:
        raise ValueError(f"closed {selected} candle unavailable")
    candle = candles[0]
    expected_open, expected_close = last_closed_candle(as_of, selected)
    if (
        candle.symbol != symbol or candle.timeframe != selected or candle.is_closed is not True
        or candle.open_time != int(expected_open.timestamp() * 1000)
        or candle.close_time != int(expected_close.timestamp() * 1000)
    ):
        raise ValueError(f"closed {selected} candle identity or cutoff is invalid")
    prices = (candle.open, candle.high, candle.low, candle.close)
    if (not all(math.isfinite(value) and value > 0 for value in prices)
            or not math.isfinite(candle.volume) or candle.volume < 0
            or candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)):
        raise ValueError(f"closed {selected} candle has invalid prices or volume")
    return candle


def candle_identity(candle: Candle) -> dict[str, Any]:
    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "open_time": candle.open_time,
        "close_time": candle.close_time,
    }


def require_current(
    candle: Candle, *, as_of: datetime | None = None,
    config_key: str | None = None, config_revision: int | None = None,
) -> None:
    if config_key is not None:
        configured = Context().run(get_config).get(config_key, "4h")
        if configured != candle.timeframe or config_key_revision(config_key) != config_revision:
            raise ValueError("Auto candle timeframe changed after the cycle started")
    now = as_of or datetime.now(UTC)
    opened = last_closed_candle(now, candle.timeframe)[0]
    if int(opened.timestamp() * 1000) != candle.open_time:
        raise ValueError(f"closed {candle.timeframe} candle expired before entry")
    if candle.close_time - candle.open_time != timeframe_ms(candle.timeframe):
        raise ValueError("candle duration is invalid")
