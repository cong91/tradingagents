"""Convert a forecast duration to complete candles without truncating it."""

from __future__ import annotations

import math

from tradingagents.auto_timeframes import timeframe_ms


def prediction_bars(timeframe: str, horizon_hours: float | None) -> int:
    step = timeframe_ms(timeframe)
    if isinstance(horizon_hours, bool) or not isinstance(horizon_hours, (int, float)):
        raise ValueError("horizon_hours must be a finite positive duration")
    try:
        bars = float(horizon_hours) * (3_600_000 / step)
    except OverflowError as exc:
        raise ValueError("horizon_hours is too large") from exc
    if not math.isfinite(bars) or bars <= 0:
        raise ValueError("horizon_hours must be a finite positive duration")
    rounded = round(bars)
    if not math.isclose(bars, rounded, rel_tol=0, abs_tol=1e-9):
        raise ValueError(f"horizon_hours={horizon_hours} is not a whole number of {timeframe} bars")
    return rounded
