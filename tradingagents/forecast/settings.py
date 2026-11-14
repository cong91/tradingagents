"""Forecast configuration and operator tuning validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from tradingagents.auto_timeframes import DEFAULT_AUTO_TIMEFRAME, timeframe_ms
from tradingagents.forecast.artifact import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PROFILE,
    MAX_HORIZON_BARS,
    MIN_LOOKBACK_BARS,
    max_context_for,
    profile_for,
)
from tradingagents.forecast.horizon import prediction_bars
from tradingagents.forecast.sampler import (
    MAX_BATCH_ROWS,
    SamplerSettings,
    validate_settings,
)

DEFAULT_TIMEFRAME = DEFAULT_AUTO_TIMEFRAME
DEFAULT_HORIZON_HOURS = 24


@dataclass(frozen=True)
class ForecastSettings:
    """The whole configuration of one active forecast; part of the cache key."""

    profile: str = DEFAULT_PROFILE
    timeframe: str = DEFAULT_TIMEFRAME
    horizon_hours: float | None = DEFAULT_HORIZON_HOURS
    paths: int = 16
    temperature: float = 0.5
    top_p: float = 0.9
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    max_batch_rows: int = 80
    max_band_spread_ratio: float = 0.15

    @property
    def horizon_bars(self) -> int:
        return prediction_bars(self.timeframe, self.horizon_hours)

    def sampler_settings(self) -> SamplerSettings:
        return SamplerSettings(
            profile=self.profile,
            paths=self.paths, temperature=self.temperature, top_p=self.top_p,
            max_batch_rows=self.max_batch_rows,
        )

    def as_params(self) -> dict[str, Any]:
        # Mirrors settings_problem(): an unsupported timeframe degrades the
        # horizon instead of raising from a getter a caller forgot to guard.
        try:
            horizon_bars: int | None = self.horizon_bars
            horizon_hours = self.horizon_hours
        except ValueError:
            horizon_bars, horizon_hours = None, None
        return {
            "timeframe": self.timeframe,
            "profile": self.profile,
            "horizon_hours": horizon_hours,
            "horizon_bars": horizon_bars,
            "paths": self.paths,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "lookback_bars": self.lookback_bars,
            "max_band_spread_ratio": self.max_band_spread_ratio,
        }

    def unavailable_reason(self) -> str | None:
        return self.settings_problem()

    def settings_problem(self) -> str | None:
        try:
            bars = self.horizon_bars
        except ValueError as exc:
            return str(exc)
        if not 1 <= bars <= MAX_HORIZON_BARS:
            return f"horizon of {bars} bars exceeds the {MAX_HORIZON_BARS}-bar bound"
        try:
            max_context = max_context_for(self.profile)
        except ValueError as exc:
            return str(exc)
        if (isinstance(self.lookback_bars, bool) or not isinstance(self.lookback_bars, int)
                or self.lookback_bars < MIN_LOOKBACK_BARS or self.lookback_bars > max_context):
            return (
                f"lookback_bars={self.lookback_bars} is outside the "
                f"[{MIN_LOOKBACK_BARS}, {max_context}] window for profile "
                f"{self.profile!r}"
            )
        if self.max_batch_rows < 1 or self.max_batch_rows > MAX_BATCH_ROWS:
            return f"max_batch_rows={self.max_batch_rows} is outside [1, {MAX_BATCH_ROWS}]"
        if not math.isfinite(self.max_band_spread_ratio) or self.max_band_spread_ratio <= 0:
            return "max_band_spread_ratio must be a finite positive number"
        return validate_settings(self.sampler_settings())


def settings_from_config(config: dict[str, Any]) -> ForecastSettings:
    """Derive lookback and prediction bars from the selected Auto candle series."""
    profile = str(config.get("kronos_active_profile") or DEFAULT_PROFILE)
    timeframe = config.get("auto_futures_timeframe", DEFAULT_TIMEFRAME)
    try:
        step_minutes = timeframe_ms(timeframe) // 60_000
    except ValueError:
        horizon = None
    else:
        raw_hold = config.get("risk_derivatives_max_hold_hours", 72.0)
        try:
            hold = float(raw_hold)
        except (TypeError, ValueError):
            hold = float(DEFAULT_HORIZON_HOURS)
        if not math.isfinite(hold) or hold <= 0:
            hold = float(DEFAULT_HORIZON_HOURS)
        bars = max(1, int(min(hold * 60 / step_minutes, MAX_HORIZON_BARS)))
        horizon = bars * step_minutes / 60
    try:
        lookback = max_context_for(profile)
    except ValueError:
        lookback = DEFAULT_LOOKBACK_BARS
    return ForecastSettings(
        profile=profile, timeframe=timeframe,
        horizon_hours=horizon, lookback_bars=lookback,
    )


_TUNING_KEYS = ("kronos_active_profile",)


def prospective_tuning_config(
    payload: dict[str, Any], config: dict[str, Any],
) -> dict[str, Any]:
    """Return the effective config that would result from applying this payload."""
    return {**config, **{key: payload[key] for key in _TUNING_KEYS if key in payload}}


def tuning_problems(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    """Validate the selected model-tokenizer pair before it is persisted."""
    if "kronos_active_profile" not in payload:
        return {}
    raw = payload["kronos_active_profile"]
    if not isinstance(raw, str) or not raw:
        return {"kronos_active_profile": "expected a non-empty string"}
    try:
        profile_for(raw)
    except ValueError as exc:
        return {"kronos_active_profile": str(exc)}
    return {}


__all__ = [
    "DEFAULT_HORIZON_HOURS",
    "DEFAULT_TIMEFRAME",
    "ForecastSettings",
    "prospective_tuning_config",
    "settings_from_config",
    "tuning_problems",
]
