"""Forecast configuration and operator tuning validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from tradingagents.forecast.artifact import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PROFILE,
    MAX_HORIZON_BARS,
    MIN_LOOKBACK_BARS,
    TIMEFRAME_HOURS,
    max_context_for,
    profile_for,
)
from tradingagents.forecast.sampler import (
    MAX_BATCH_ROWS,
    SamplerSettings,
    validate_settings,
)

DEFAULT_TIMEFRAME = "4h"


DEFAULT_HORIZON_HOURS = 24


@dataclass(frozen=True)
class ForecastSettings:
    """The whole configuration of one active forecast; part of the cache key."""

    profile: str = DEFAULT_PROFILE
    timeframe: str = DEFAULT_TIMEFRAME
    horizon_hours: int = DEFAULT_HORIZON_HOURS
    paths: int = 16
    temperature: float = 0.5
    top_p: float = 0.9
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    max_batch_rows: int = 80
    max_band_spread_ratio: float = 0.15

    def sampler_settings(self) -> SamplerSettings:
        return SamplerSettings(
            profile=self.profile,
            paths=self.paths, temperature=self.temperature, top_p=self.top_p,
            max_batch_rows=self.max_batch_rows,
        )

    def as_params(self) -> dict[str, Any]:
        # Everything here changes what the numbers MEAN and is therefore part of
        # the cache identity: the band-spread bound can flip a direction to
        # ``unclear``, so it travels with the rest.
        return {
            "timeframe": self.timeframe,
            "profile": self.profile,
            "horizon_hours": self.horizon_hours,
            "paths": self.paths,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "lookback_bars": self.lookback_bars,
            "max_band_spread_ratio": self.max_band_spread_ratio,
        }

    def unavailable_reason(self) -> str | None:
        """Why these settings cannot run a forecast, or None when they can.

        Bounds are honored here, at the settings layer: the timeframe must be a
        supported one, the horizon a whole number of its bars inside the model
        bound, the lookback inside the model's context, and the fixed sampling
        knobs inside their measured bounds.
        """
        return self.settings_problem()

    def settings_problem(self) -> str | None:
        hours = TIMEFRAME_HOURS.get(self.timeframe)
        if hours is None:
            return (
                f"timeframe {self.timeframe!r} is not a supported Kronos timeframe "
                f"(one of {sorted(TIMEFRAME_HOURS)})"
            )
        if self.horizon_hours is None or self.horizon_hours % hours:
            return (
                f"horizon_hours={self.horizon_hours} is not a whole number of "
                f"{self.timeframe} bars"
            )
        bars = self.horizon_hours // hours
        if bars < 1 or bars > MAX_HORIZON_BARS:
            return f"horizon of {bars} bars exceeds the {MAX_HORIZON_BARS}-bar bound"
        try:
            max_context = max_context_for(self.profile)
        except ValueError as exc:
            return str(exc)
        if self.lookback_bars < MIN_LOOKBACK_BARS or self.lookback_bars > max_context:
            return (
                f"lookback_bars={self.lookback_bars} is outside the "
                f"[{MIN_LOOKBACK_BARS}, {max_context}] window for profile "
                f"{self.profile!r}"
            )
        if self.max_batch_rows < 1 or self.max_batch_rows > MAX_BATCH_ROWS:
            return (
                f"max_batch_rows={self.max_batch_rows} is outside [1, {MAX_BATCH_ROWS}]"
            )
        if not math.isfinite(self.max_band_spread_ratio) or self.max_band_spread_ratio <= 0:
            return (
                f"max_band_spread_ratio={self.max_band_spread_ratio} must be a finite "
                "positive number"
            )
        return validate_settings(self.sampler_settings())


def settings_from_config(config: dict[str, Any]) -> ForecastSettings:
    """Derive active forecast inputs from the selected pair and hold limit."""
    profile = str(config.get("kronos_active_profile") or DEFAULT_PROFILE)
    raw_hold = config.get("risk_derivatives_max_hold_hours", 72.0)
    try:
        hold = float(raw_hold)
    except (TypeError, ValueError):
        hold = DEFAULT_HORIZON_HOURS
    if not math.isfinite(hold) or hold <= 0:
        hold = DEFAULT_HORIZON_HOURS
    step = TIMEFRAME_HOURS[DEFAULT_TIMEFRAME]
    horizon = min(MAX_HORIZON_BARS, max(1, int(hold // step))) * step
    try:
        lookback = max_context_for(profile)
    except ValueError:
        lookback = DEFAULT_LOOKBACK_BARS
    return ForecastSettings(profile=profile, horizon_hours=horizon, lookback_bars=lookback)


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
