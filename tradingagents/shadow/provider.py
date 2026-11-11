"""The shadow layer's view of the forecast contract.

Everything in this package is observation-only. No module here is imported by
``tradingagents/graph``, ``tradingagents/agents`` or ``server/autoloop``; the
worker that calls it runs on its own and its failures are invisible to the
order, risk and protection paths (a test pins the import graph).

The vocabulary that keeps an observation honest — the request, the path summary,
the attempt, the calibration labels — is NOT owned here. It lives in
``tradingagents.forecast.contract`` because the decision path reads the same
model and must not import the observation layer to do it. This module re-exports
that contract so the shadow modules keep a stable import site without a second,
diverging definition.
"""

from __future__ import annotations

from tradingagents.dataflows.market_data import Candle  # noqa: F401 - re-exported
from tradingagents.forecast.contract import (
    CALIBRATION_NOTES,
    CALIBRATION_RULE,
    CALIBRATION_SAMPLE_RATIO,
    DISTRIBUTION_MODEL,
    DISTRIBUTION_RULE,
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    OUTCOME_ABSTAINED,
    OUTCOME_EXECUTED,
    OUTCOME_REJECTED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    STATUSES,
    ForecastAttempt,
    ForecastProvider,
    ForecastRequest,
    PathSummary,
    direction_of,
    stable_seed,
)

__all__ = [
    "CALIBRATION_NOTES",
    "CALIBRATION_RULE",
    "CALIBRATION_SAMPLE_RATIO",
    "Candle",
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
