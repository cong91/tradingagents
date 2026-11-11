"""Cheap deterministic baseline filters, on the same contract as the model.

Each filter reads the same closed-candle request a model provider reads and
answers with a single verdict: ``up``, ``down`` or ``flat``. They exist so the
model's observations can be compared against something cheap on identical
inputs and identical costs — not to trade anything.

A filter has no sample distribution: ``distribution_kind`` is
``deterministic_rule`` and the calibration label says so, so a stored row can
never be read as "N of N model paths agreed". Every filter refuses a series it
cannot honestly read (too short, mixed timeframe, gap) instead of guessing.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence

from tradingagents.dataflows.market_data import Candle
from tradingagents.shadow.provider import (
    CALIBRATION_RULE,
    DISTRIBUTION_RULE,
    STATUS_ERROR,
    STATUS_OK,
    ForecastAttempt,
    ForecastRequest,
    direction_of,
)

# How many closed candles each filter needs before it will answer at all.
MIN_CANDLES = 3


def _closes(candles: Sequence[Candle]) -> list[float]:
    return [float(candle.close) for candle in candles]


def _validate(request: ForecastRequest, needed: int) -> str | None:
    """The reason a request is unreadable, or None when it is usable."""
    if len(request.candles) < needed:
        return f"needs at least {needed} closed candles, got {len(request.candles)}"
    timeframes = {candle.timeframe for candle in request.candles}
    if timeframes != {request.timeframe}:
        return f"candles are not all {request.timeframe}: {sorted(timeframes)}"
    if not all(candle.is_closed for candle in request.candles):
        return "a forming candle was supplied; only closed candles may be used"
    return None


def htf_direction_volume(request: ForecastRequest) -> ForecastAttempt:
    """Direction of the last two closes, confirmed by above-average volume.

    Follows the immediate move only when the newest candle's volume is at or
    above the series mean; otherwise it abstains with ``flat``. The filter is
    deliberately trivial: it is the cheap arm of the comparison, not a strategy.
    """
    reason = _validate(request, MIN_CANDLES)
    if reason is not None:
        return ForecastAttempt(status=STATUS_ERROR, reason=reason)
    closes = _closes(request.candles)
    volumes = [float(candle.volume) for candle in request.candles]
    mean_volume = statistics.fmean(volumes)
    last_move = closes[-1] - closes[-2]
    if mean_volume > 0 and volumes[-1] < mean_volume:
        return ForecastAttempt(
            status=STATUS_OK, direction="flat",
            reason="volume below the series mean; the filter abstains",
            samples={"mean_volume": mean_volume, "last_volume": volumes[-1]},
        )
    return ForecastAttempt(
        status=STATUS_OK,
        direction=direction_of(closes[-2], closes[-1]) if last_move else "flat",
        samples={"mean_volume": mean_volume, "last_volume": volumes[-1]},
    )


def donchian_breakout(request: ForecastRequest, *, lookback: int = 3) -> ForecastAttempt:
    """Close above the prior N-candle high (``up``) or below the low (``down``).

    The channel excludes the newest candle, so the breakout is measured against
    levels that existed before it — no look-ahead inside the series.
    """
    reason = _validate(request, lookback + 1)
    if reason is not None:
        return ForecastAttempt(status=STATUS_ERROR, reason=reason)
    history = request.candles[:-1][-lookback:]
    newest = request.candles[-1]
    upper = max(float(candle.high) for candle in history)
    lower = min(float(candle.low) for candle in history)
    if newest.close > upper:
        direction = "up"
    elif newest.close < lower:
        direction = "down"
    else:
        direction = "flat"
    return ForecastAttempt(
        status=STATUS_OK, direction=direction,
        samples={"upper": upper, "lower": lower, "lookback": lookback},
    )


def momentum_zscore(request: ForecastRequest, *, threshold: float = 0.5) -> ForecastAttempt:
    """Standardized return of the newest candle against the series' own stdev.

    A cheap, self-normalizing momentum read: ``up``/``down`` only when the last
    return is at least ``threshold`` standard deviations from zero, else flat.
    A zero-dispersion series (every return identical) cannot be standardized and
    answers flat with the reason recorded.
    """
    reason = _validate(request, MIN_CANDLES)
    if reason is not None:
        return ForecastAttempt(status=STATUS_ERROR, reason=reason)
    closes = _closes(request.candles)
    returns = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    dispersion = statistics.pstdev(returns)
    if dispersion <= 0:
        return ForecastAttempt(
            status=STATUS_OK, direction="flat",
            reason="returns have zero dispersion; no z-score is defined",
            samples={"dispersion": dispersion},
        )
    z_score = returns[-1] / dispersion
    direction = "up" if z_score >= threshold else "down" if z_score <= -threshold else "flat"
    return ForecastAttempt(
        status=STATUS_OK, direction=direction,
        samples={"z_score": z_score, "dispersion": dispersion, "threshold": threshold},
    )


FILTERS: dict[str, Callable[[ForecastRequest], ForecastAttempt]] = {
    "htf_direction_volume": htf_direction_volume,
    "donchian_breakout": donchian_breakout,
    "momentum_zscore": momentum_zscore,
}


class FilterProvider:
    """A baseline filter wearing the same provider interface as the model.

    The revision string encodes the filter's identity so a stored row says which
    cheap rule produced it (``filter:<name>``); ``source_revision`` names the
    repo module that owns the rule, and the artifact hash stays ``None`` because
    a deterministic rule loads no weights.
    """

    distribution_kind = DISTRIBUTION_RULE
    calibration_label = CALIBRATION_RULE
    model_revision = "1"
    source_revision = "tradingagents.shadow.filters"
    artifact_hash: str | None = None

    def __init__(self, name: str, *, deadline_seconds: float = 5.0) -> None:
        try:
            self._rule = FILTERS[name]
        except KeyError as exc:
            raise ValueError(
                f"unknown shadow filter {name!r}; known: {', '.join(sorted(FILTERS))}"
            ) from exc
        self.name = name
        self.provider_id = f"filter:{name}"
        self.deadline_seconds = deadline_seconds

    def forecast(self, request: ForecastRequest) -> ForecastAttempt:
        return self._rule(request)


def filter_names() -> Sequence[str]:
    return tuple(sorted(FILTERS))


__all__ = [
    "FILTERS",
    "MIN_CANDLES",
    "FilterProvider",
    "donchian_breakout",
    "filter_names",
    "htf_direction_volume",
    "momentum_zscore",
]
