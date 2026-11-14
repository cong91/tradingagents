"""Forecast input and output records shared by decision consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import MAX_PATHS
from tradingagents.forecast.contract import CALIBRATION_SAMPLE_RATIO, DISTRIBUTION_MODEL, STATUS_OK


@dataclass(frozen=True)
class ForecastBlock:
    """One symbol's forecast, in the shape the decision context renders.

    ``status`` is the honest outcome: ``ok`` carries numbers, everything else
    carries a reason and NO numbers (a block with a band and a failure status
    would be read as a forecast).
    """

    symbol: str
    status: str
    reason: str | None = None
    direction: str | None = None
    direction_counts: dict[str, int] = field(default_factory=dict)
    direction_ratio: dict[str, float] = field(default_factory=dict)
    band: dict[str, float | None] = field(default_factory=dict)
    band_spread_ratio: float | None = None
    reference_close: float | None = None
    data_open_time: int | None = None
    as_of: str | None = None
    horizon_hours: float | None = None
    horizon_bars: int | None = None
    timeframe: str | None = None
    paths: int | None = None
    per_path_samples: int = 1
    generation_kind: str | None = None
    temperature: float | None = None
    profile: str | None = None
    model_revision: str | None = None
    artifact_hash: str | None = None
    latency_ms: float | None = None
    created_at: str | None = None
    calibration_label: str = CALIBRATION_SAMPLE_RATIO
    distribution_kind: str = DISTRIBUTION_MODEL
    ohlc_quality: dict[str, Any] = field(default_factory=lambda: {
        "status": "unknown",
        "reason": "Forecast OHLC quality was not assessed; do not use it for stop/target analysis.",
    })
    ohlc_provenance: dict[str, Any] = field(default_factory=dict)
    ohlc_paths: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def uncertain(self) -> bool:
        """A drawn forecast whose own band says it is too wide to size against."""
        return self.ok and self.direction == "unclear"

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "direction": self.direction,
            "calibration_label": self.calibration_label,
            "distribution_kind": self.distribution_kind,
        }
        if not self.ok:
            return payload
        payload.update({
            "direction_counts": self.direction_counts,
            "direction_ratio": self.direction_ratio,
            "band": self.band,
            "band_spread_ratio": self.band_spread_ratio,
            "signal_basis": "close_only",
            "ohlc_quality": self.ohlc_quality,
            "ohlc_provenance": self.ohlc_provenance,
            "ohlc_paths": list(self.ohlc_paths[:MAX_PATHS]),
            "reference_close": self.reference_close,
            "data_open_time": self.data_open_time,
            "as_of": self.as_of,
            "horizon_hours": self.horizon_hours,
            "horizon_bars": self.horizon_bars,
            "timeframe": self.timeframe,
            "paths": self.paths,
            "per_path_samples": self.per_path_samples,
            "generation_kind": self.generation_kind,
            "temperature": self.temperature,
            "profile": self.profile,
            "model_revision": self.model_revision,
            "artifact_hash": self.artifact_hash,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at,
        })
        return payload


@dataclass(frozen=True)
class CandleSeries:
    """One symbol's closed candles, cut at the decision moment."""

    symbol: str
    candles: tuple[Candle, ...]
    as_of: datetime
