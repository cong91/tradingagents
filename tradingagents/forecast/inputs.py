"""Validate identified, closed and complete forecast candle windows."""

from __future__ import annotations

import math

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import MIN_LOOKBACK_BARS
from tradingagents.forecast.block import CandleSeries


def series_problem(series: CandleSeries, timeframe: str, *, lookback_bars: int) -> str | None:
    """Why this series cannot be forecast, or None when it can.

    A refusal is a real outcome: a gap, a forming bar, a mixed timeframe or
    symbol, a stale newest bar, a malformed price, too little history — all mean
    the input is not the series the question is about, and interpolating or
    "just running it" would put data the decision never saw (or wrong data)
    into a prompt as a forecast. The checks, in order:

    - ``as_of`` must be timezone-aware (a naive cutoff would silently mean the
      host's local time) and the candles must all name the requested symbol;
    - every bar has the requested timeframe's duration and closed at or before
      ``as_of``; the NEWEST closed bar of the grid is present: a series ending
      a day ago is stale even though no bar crosses the cutoff, and staleness
      is refused, not forecast through;
    - prices are finite and positive, volume is finite and non-negative, and
      each bar's order is sane (high >= max(open, close) >= min >= low);
    - the window is gap-free on the closed-bar grid.
    """
    if series.as_of.tzinfo is None or series.as_of.utcoffset() is None:
        return "the decision moment (as_of) must be timezone-aware"
    candles = series.candles
    required_bars = max(MIN_LOOKBACK_BARS, lookback_bars)
    if len(candles) < required_bars:
        return (
            f"only {len(candles)} closed {timeframe} candles available; Kronos needs "
            f"at least {required_bars} for the configured lookback"
        )
    symbols = {candle.symbol for candle in candles}
    if symbols != {series.symbol}:
        return f"candles are not all for symbol {series.symbol}: {sorted(symbols)}"
    timeframes = {candle.timeframe for candle in candles}
    if timeframes != {timeframe}:
        return f"candles are not all {timeframe}: {sorted(timeframes)}"
    if not all(candle.is_closed is True for candle in candles):
        return "a forming candle was supplied; only closed candles may be used"
    step = _timeframe_ms(timeframe)
    for index, candle in enumerate(candles):
        if (isinstance(candle.open_time, bool) or not isinstance(candle.open_time, int)
                or candle.open_time < 0 or candle.open_time % step
                or isinstance(candle.close_time, bool) or not isinstance(candle.close_time, int)):
            return f"candle {index} has an invalid or off-grid timestamp"
        if candle.close_time - candle.open_time != step:
            return (
                f"candle {index} has a duration inconsistent with the {timeframe} "
                f"timeframe (expected {step} ms)"
            )
    cutoff_ms = int(series.as_of.timestamp() * 1000)
    late = [candle for candle in candles if candle.close_time > cutoff_ms]
    if late:
        return (
            f"a candle closing at {late[0].close_time} is after the decision moment "
            f"{cutoff_ms}; the series would contain a bar that had not closed"
        )
    newest_closed = cutoff_ms - (cutoff_ms % step)
    if candles[-1].close_time < newest_closed:
        age = newest_closed - candles[-1].close_time
        return (
            f"the series is stale: its newest bar closes at {candles[-1].close_time}, "
            f"{age} ms before the newest closed {timeframe} bar at the decision "
            "moment; a forecast on an older bar answers a different question"
        )
    for index, candle in enumerate(candles):
        problem = _candle_problem(candle, index)
        if problem is not None:
            return problem
    opens = [candle.open_time for candle in candles]
    if opens != sorted(opens):
        return "candles are not in ascending open-time order"
    if len(set(opens)) != len(opens):
        return "candles contain a duplicate open time"
    window = candles[-lookback_bars:]
    for previous, current in zip(window, window[1:], strict=False):
        if current.open_time - previous.open_time != step:
            return (
                f"a gap in the closed series between {previous.open_time} and "
                f"{current.open_time} (expected {step} ms)"
            )
    return None


def _candle_problem(candle: Candle, index: int) -> str | None:
    """One candle's price/volume sanity, or None."""
    prices = {
        "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close,
    }
    for name, value in prices.items():
        if not math.isfinite(value) or value <= 0:
            return (
                f"candle {index} carries a non-finite or non-positive {name} price "
                f"{value!r}; a malformed price must not enter a prompt as data"
            )
    if candle.high < candle.low or candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        return (
            f"candle {index} has inconsistent OHLC order (open={candle.open}, "
            f"high={candle.high}, low={candle.low}, close={candle.close})"
        )
    if not math.isfinite(candle.volume) or candle.volume < 0:
        return f"candle {index} carries an invalid volume {candle.volume!r}"
    return None


def _timeframe_ms(timeframe: str) -> int:
    from tradingagents.dataflows.market_data import timeframe_ms

    return timeframe_ms(timeframe)
