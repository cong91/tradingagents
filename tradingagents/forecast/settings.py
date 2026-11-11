"""Forecast configuration and operator tuning validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from tradingagents.forecast.artifact import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PROFILE,
    MAX_HORIZON_BARS,
    MAX_PATHS,
    MIN_LOOKBACK_BARS,
    MIN_PATHS,
    TIMEFRAME_HOURS,
    max_context_for,
    profile_for,
)
from tradingagents.forecast.sampler import MAX_BATCH_ROWS, SamplerSettings, validate_settings

MODE_CONTEXT_ONLY = "context_only"


MODE_FILTER = "filter"


MODES = (MODE_CONTEXT_ONLY, MODE_FILTER)


FAILURE_DEFER = "defer"


FAILURE_PASS_THROUGH = "pass_through"


FAILURE_POLICIES = (FAILURE_DEFER, FAILURE_PASS_THROUGH)


DEFAULT_TIMEFRAME = "4h"


DEFAULT_HORIZON_HOURS = 24


@dataclass(frozen=True)
class ForecastSettings:
    """The whole configuration of one active forecast; part of the cache key."""

    enabled: bool = False
    profile: str = DEFAULT_PROFILE
    mode: str = MODE_CONTEXT_ONLY
    timeframe: str = DEFAULT_TIMEFRAME
    horizon_hours: int = DEFAULT_HORIZON_HOURS
    paths: int = 16
    temperature: float = 0.5
    top_p: float = 0.9
    lookback_bars: int = DEFAULT_LOOKBACK_BARS
    max_batch_rows: int = 80
    failure_policy: str = "defer"
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
        bound, the lookback inside the model's context, the sampling knobs
        inside their measured bounds, and the failure policy a real one.
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
        if self.failure_policy not in FAILURE_POLICIES:
            return (
                f"failure_policy={self.failure_policy!r} is not one of "
                f"{list(FAILURE_POLICIES)}"
            )
        return validate_settings(self.sampler_settings())


def settings_from_config(config: dict[str, Any]) -> ForecastSettings:
    """Read the active Kronos settings; a bad value is surfaced, not defaulted.

    Reading a config key that is missing falls back to the shipped default; a
    key that is present but unusable (a negative ``paths``, an unknown mode) is
    kept as given so ``unavailable_reason`` can refuse it with the actual value.
    The alternative — silently substituting a valid setting — would make the
    operator's configuration disagree with what ran.
    """
    defaults = ForecastSettings()

    def value(key: str, fallback: Any) -> Any:
        raw = config.get(key, fallback)
        return fallback if raw is None else raw

    try:
        paths = int(value("kronos_active_paths", defaults.paths))
    except (TypeError, ValueError):
        paths = -1
    try:
        horizon = int(value("kronos_active_horizon_hours", defaults.horizon_hours))
    except (TypeError, ValueError):
        horizon = -1
    try:
        temperature = float(value("kronos_active_temperature", defaults.temperature))
    except (TypeError, ValueError):
        temperature = -1.0
    try:
        lookback = int(value("kronos_active_lookback_bars", defaults.lookback_bars))
    except (TypeError, ValueError):
        lookback = -1
    try:
        max_rows = int(value("kronos_active_max_batch_rows", defaults.max_batch_rows))
    except (TypeError, ValueError):
        max_rows = -1
    try:
        band_spread = float(
            value("kronos_active_max_band_spread_ratio", defaults.max_band_spread_ratio)
        )
    except (TypeError, ValueError):
        band_spread = -1.0
    return ForecastSettings(
        enabled=value("kronos_active_enabled", defaults.enabled) is True,
        profile=str(value("kronos_active_profile", defaults.profile)),
        mode=str(value("kronos_active_mode", defaults.mode)),
        timeframe=str(value("kronos_active_timeframe", defaults.timeframe)),
        horizon_hours=horizon,
        paths=paths,
        temperature=temperature,
        top_p=defaults.top_p,
        lookback_bars=lookback,
        max_batch_rows=max_rows,
        failure_policy=str(value("kronos_active_failure_policy", defaults.failure_policy)),
        max_band_spread_ratio=band_spread,
    )


_TUNING_KEYS = (
    "kronos_active_profile",
    "kronos_active_mode",
    "kronos_active_timeframe",
    "kronos_active_horizon_hours",
    "kronos_active_paths",
    "kronos_active_temperature",
    "kronos_active_lookback_bars",
    "kronos_active_max_batch_rows",
    "kronos_active_failure_policy",
    "kronos_active_max_band_spread_ratio",
)


def prospective_tuning_config(
    payload: dict[str, Any], config: dict[str, Any],
) -> dict[str, Any]:
    """Return the effective config that would result from applying this payload."""
    return {**config, **{key: payload[key] for key in _TUNING_KEYS if key in payload}}


def _lookback_for_config(config: dict[str, Any]) -> int:
    try:
        return int(config.get("kronos_active_lookback_bars", DEFAULT_LOOKBACK_BARS))
    except (TypeError, ValueError):
        return -1


def tuning_problems(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    """Field -> reason for tuning values that cannot run, checked at the API."""
    problems: dict[str, str] = {}
    for key in _TUNING_KEYS:
        if key not in payload:
            continue
        raw = payload[key]
        if key in (
            "kronos_active_profile", "kronos_active_mode",
            "kronos_active_timeframe", "kronos_active_failure_policy",
        ):
            if not isinstance(raw, str) or not raw:
                problems[key] = "expected a non-empty string"
            elif key == "kronos_active_profile":
                try:
                    profile_for(raw)
                except ValueError as exc:
                    problems[key] = str(exc)
            elif key == "kronos_active_mode" and raw not in MODES:
                problems[key] = f"unknown mode {raw!r}; supported: {list(MODES)}"
            elif key == "kronos_active_timeframe" and raw not in TIMEFRAME_HOURS:
                problems[key] = (
                    f"unsupported timeframe {raw!r}; supported: {sorted(TIMEFRAME_HOURS)}"
                )
            elif key == "kronos_active_failure_policy" and raw not in FAILURE_POLICIES:
                problems[key] = (
                    f"unknown failure policy {raw!r}; supported: {list(FAILURE_POLICIES)}"
                )
            continue
        if key in (
            "kronos_active_horizon_hours", "kronos_active_paths",
            "kronos_active_lookback_bars", "kronos_active_max_batch_rows",
        ):
            if isinstance(raw, bool) or not isinstance(raw, int):
                problems[key] = "expected an integer"
                continue
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
            problems[key] = "expected a number"
            continue
        if not math.isfinite(float(raw)):
            problems[key] = "must be a finite number"
            continue
        if key == "kronos_active_horizon_hours":
            timeframe = payload.get("kronos_active_timeframe") or config.get(
                "kronos_active_timeframe", DEFAULT_TIMEFRAME,
            )
            hours = TIMEFRAME_HOURS.get(str(timeframe))
            if hours is None:
                problems[key] = f"cannot resolve bars for timeframe {timeframe!r}"
                continue
            if raw % hours:
                problems[key] = f"{raw}h is not a whole number of {timeframe} bars"
                continue
            if raw // hours > MAX_HORIZON_BARS or raw < 1:
                problems[key] = (
                    f"horizon of {raw // max(hours, 1)} bars is outside 1..{MAX_HORIZON_BARS}"
                )
            continue
        if key == "kronos_active_paths":
            if raw < MIN_PATHS or raw > MAX_PATHS:
                problems[key] = (
                    f"paths={raw} is outside [{MIN_PATHS}, {MAX_PATHS}]; the floor is a "
                    "sampling budget, not a skill claim"
                )
            continue
        if key == "kronos_active_max_batch_rows":
            if raw < 1 or raw > MAX_BATCH_ROWS:
                problems[key] = f"max_batch_rows={raw} is outside [1, {MAX_BATCH_ROWS}]"
            continue
        if key == "kronos_active_temperature":
            if not 0.0 < float(raw) <= 2.0:
                problems[key] = f"temperature={raw} is outside (0, 2]"
            continue
        if key == "kronos_active_max_band_spread_ratio" and float(raw) <= 0:
            problems[key] = f"max_band_spread_ratio={raw} must be positive"

    merged = prospective_tuning_config(payload, config)
    effective_problem = settings_from_config(merged).settings_problem()
    if effective_problem is not None:
        if "lookback" in effective_problem:
            problems.setdefault("kronos_active_lookback_bars", effective_problem)
        elif "profile" in effective_problem or "unknown Kronos profile" in effective_problem:
            problems.setdefault("kronos_active_profile", effective_problem)
    return problems
