"""Shadow forecasting: observations that never touch a decision (plan §6).

Nothing in this package is imported by the decision path — ``tradingagents/graph``,
``tradingagents/agents`` and ``server/autoloop`` stay clear of it (a test pins the
import graph). The worker that calls it runs independently, after a cycle is
already terminal, and every failure mode is recorded as data rather than raised.

Modules:
    provider    the request/attempt contract and the honest vocabulary
    filters     cheap deterministic baselines on the same contract
    kronos      the optional pinned Kronos model, run out of process
    kronos_runner   the out-of-process child entry point (never imported here)
    recorder    one attempt -> one storable observation row
    evaluation  one observation -> its realized outcome
"""

from __future__ import annotations

from tradingagents.shadow.evaluation import evaluate_forecast
from tradingagents.shadow.filters import FILTERS, FilterProvider, filter_names
from tradingagents.shadow.provider import (
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
    ForecastAttempt,
    ForecastProvider,
    ForecastRequest,
    PathSummary,
    direction_of,
)
from tradingagents.shadow.recorder import (
    RequestError,
    build_observation,
    outcome_group_for,
    validate_candles,
)

__all__ = [
    "CALIBRATION_RULE",
    "CALIBRATION_SAMPLE_RATIO",
    "DISTRIBUTION_MODEL",
    "DISTRIBUTION_RULE",
    "FILTERS",
    "GENERATION_FORWARD",
    "GENERATION_RETROSPECTIVE",
    "OUTCOME_ABSTAINED",
    "OUTCOME_EXECUTED",
    "OUTCOME_REJECTED",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STATUS_UNAVAILABLE",
    "FilterProvider",
    "ForecastAttempt",
    "ForecastProvider",
    "ForecastRequest",
    "PathSummary",
    "RequestError",
    "build_observation",
    "direction_of",
    "evaluate_forecast",
    "filter_names",
    "outcome_group_for",
    "validate_candles",
]
