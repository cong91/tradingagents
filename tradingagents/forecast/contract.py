"""The forecast vocabulary used by the active Futures decision path.

This module defines the request and answer contract for forecasts consumed by
Trader and Portfolio Manager context. It carries no observation-only behavior.

The labels that keep a forecast honest live here:

- a request carries only CLOSED candles and states the exact timeframe and
  horizon it asks about;
- a provider declares its identity (``provider_id``), the pinned revisions and
  the artifact hash it actually loaded, so no row can claim a model revision it
  did not run;
- ``calibration_label`` states plainly that a model sample ratio is NOT a
  calibrated probability, and ``CALIBRATION_NOTES`` spells out what each label
  means for a reader who only sees the number.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from tradingagents.dataflows.market_data import Candle

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUSES = (STATUS_OK, STATUS_UNAVAILABLE, STATUS_ERROR, STATUS_TIMEOUT)

# Two kinds of distribution, never mixed up:
#   model_samples     -> N independent draws from a model; the direction ratio is
#                        a SAMPLE ratio over N paths, not a probability;
#   deterministic_rule-> a cheap filter with one deterministic verdict; it has no
#                        distribution at all (no band, no dispersion).
DISTRIBUTION_MODEL = "model_samples"
DISTRIBUTION_RULE = "deterministic_rule"

GENERATION_FORWARD = "forward"
GENERATION_RETROSPECTIVE = "retrospective"

OUTCOME_EXECUTED = "executed"
OUTCOME_REJECTED = "rejected"
OUTCOME_ABSTAINED = "abstained"

CALIBRATION_SAMPLE_RATIO = "model_sample_ratio_not_probability"
CALIBRATION_RULE = "deterministic_rule_no_distribution"

CALIBRATION_NOTES = {
    CALIBRATION_SAMPLE_RATIO: (
        "This is the share of model-generated paths that ended in each direction. "
        "It is a sample ratio over a small number of draws from one model, not a "
        "calibrated probability of the market moving that way."
    ),
    CALIBRATION_RULE: (
        "A deterministic rule produces one verdict; there is no sample distribution "
        "and therefore no ratio, band or dispersion to report."
    ),
}


@dataclass(frozen=True)
class ForecastRequest:
    """One observation request: closed candles plus the locked timeframe/horizon.

    ``candles`` must already be closed, in a single timeframe, ascending and
    gap-free; the recorder refuses anything else rather than silently
    interpolating a series. ``as_of`` is the moment the candles are read at
    (the decision moment); candles closing after it may never appear.
    """

    symbol: str
    timeframe: str
    horizon_hours: int
    candles: tuple[Candle, ...]
    as_of: str
    seed: int
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def reference_close(self) -> float:
        return self.candles[-1].close

    @property
    def data_open_time(self) -> int:
        return self.candles[-1].open_time


@dataclass(frozen=True)
class PathSummary:
    """One sampled path in the model's own units, in draw order.

    ``opens``/``closes``/``highs``/``lows`` keep the whole horizon bar by bar so a later
    evaluation can ask which protective level the path touched FIRST — an
    end-point summary alone cannot answer that, and interpolating it would
    invent the ordering. A deterministic rule leaves them empty.

    ``path_low``/``path_high`` are the extremes of the whole path (the lowest low
    and highest high) when every sampled path has consistent OHLC ordering;
    otherwise both are unavailable for every path. They were once named
    ``min_close``/``max_close``, which
    described something else entirely: the minimum/maximum CLOSE is a different
    number whenever a wick extends beyond every close, and a reader comparing the
    name against ``closes`` would have drawn the wrong conclusion.
    """

    end_close: float
    path_low: float | None
    path_high: float | None
    direction: str  # "up" | "down" | "flat"
    closes: tuple[float, ...] = ()
    highs: tuple[float, ...] = ()
    lows: tuple[float, ...] = ()
    opens: tuple[float, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "end_close": self.end_close,
            "path_low": self.path_low,
            "path_high": self.path_high,
            "direction": self.direction,
            "opens": list(self.opens),
            "closes": list(self.closes),
            "highs": list(self.highs),
            "lows": list(self.lows),
        }


@dataclass(frozen=True)
class ForecastAttempt:
    """A provider's answer, successful or not; the status is never inferred."""

    status: str
    reason: str | None = None
    paths: tuple[PathSummary, ...] = ()
    direction: str | None = None
    duration_ms: float | None = None
    samples: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown forecast status: {self.status!r}")
        if self.status == STATUS_OK and not self.reason and not self.paths and self.direction is None:
            raise ValueError("a successful attempt must carry paths or a direction")


class ForecastProvider(Protocol):
    """What any consumer needs from a provider, model or cheap filter."""

    provider_id: str
    model_revision: str
    source_revision: str | None
    artifact_hash: str | None
    distribution_kind: str
    calibration_label: str
    deadline_seconds: float

    def forecast(self, request: ForecastRequest) -> ForecastAttempt: ...


def direction_of(reference: float, value: float) -> str:
    """The realized direction of ``value`` against ``reference``."""
    if value > reference:
        return "up"
    if value < reference:
        return "down"
    return "flat"


def stable_seed(cycle_id: str, provider_id: str) -> int:
    """A process-independent seed derived from the observation's identity.

    SHA-256, never the builtin ``hash()``: Python salts string hashing per
    process, so a ``hash()``-based seed differs on every run and the same
    observation would draw different paths from the same model — which destroys
    reproducibility and any forward comparison (probed: 158156885 then 991776861
    for the same cycle). With this, two processes draw identical paths.
    """
    material = f"{cycle_id}|{provider_id}".encode()
    return int(hashlib.sha256(material).hexdigest()[:8], 16)


__all__ = [
    "CALIBRATION_NOTES",
    "CALIBRATION_RULE",
    "CALIBRATION_SAMPLE_RATIO",
    "DISTRIBUTION_MODEL",
    "DISTRIBUTION_RULE",
    "ForecastAttempt",
    "ForecastProvider",
    "ForecastRequest",
    "GENERATION_FORWARD",
    "GENERATION_RETROSPECTIVE",
    "OUTCOME_ABSTAINED",
    "OUTCOME_EXECUTED",
    "OUTCOME_REJECTED",
    "PathSummary",
    "STATUSES",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STATUS_UNAVAILABLE",
    "direction_of",
    "stable_seed",
]
