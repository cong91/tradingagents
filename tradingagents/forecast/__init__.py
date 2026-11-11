"""Neutral forecast core: the budgeted Kronos model both consumers share.

This package owns the contract, the pinned-artifact verification, the bounded
isolated sampler and the service that turns closed candles into a decision-grade
block. It is deliberately NOT part of ``tradingagents.shadow``: the shadow layer
is observation-only and a test pins that the graph, the agents and the autoloop
never import it, so a model the decision path reads has to live somewhere the
decision path may import.

Two consumers, one core:

- ``server/shadow_worker.py`` records observations from it (off by default,
  never touching an order);
- ``server/autoloop.py`` puts the compact block into the futures execution
  context BEFORE the Trader and the Portfolio Manager decide.

The core itself decides nothing: it answers "what did the pinned model sample
from candles that had already closed", and both callers decide what that means.
"""

from __future__ import annotations

from tradingagents.forecast.artifact import (
    MAX_HORIZON_BARS,
    MAX_PATHS,
    MIN_LOOKBACK_BARS,
    MIN_PATHS,
    MODEL_REPO,
    MODEL_REVISION,
    TOKENIZER_REPO,
    TOKENIZER_REVISION,
    isolated_model_dir,
    isolated_python,
    isolated_source_dir,
    verify_artifacts,
)
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.cache import ForecastCache
from tradingagents.forecast.contract import (
    CALIBRATION_NOTES,
    CALIBRATION_SAMPLE_RATIO,
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    ForecastAttempt,
    ForecastProvider,
    ForecastRequest,
    PathSummary,
    direction_of,
    stable_seed,
)
from tradingagents.forecast.inputs import series_problem
from tradingagents.forecast.rendering import render_context_section, render_forecast_block
from tradingagents.forecast.sampler import (
    KronosSampler,
    SampledSymbol,
    SamplerError,
    SamplerSettings,
    plan_batches,
)
from tradingagents.forecast.service import FILTER_MODE_REFUSAL, KronosForecastService
from tradingagents.forecast.settings import (
    DEFAULT_HORIZON_HOURS,
    DEFAULT_TIMEFRAME,
    MODE_CONTEXT_ONLY,
    MODE_FILTER,
    ForecastSettings,
    settings_from_config,
)

__all__ = [
    "CALIBRATION_NOTES",
    "CALIBRATION_SAMPLE_RATIO",
    "CandleSeries",
    "DEFAULT_HORIZON_HOURS",
    "DEFAULT_TIMEFRAME",
    "FILTER_MODE_REFUSAL",
    "ForecastAttempt",
    "ForecastBlock",
    "ForecastCache",
    "ForecastProvider",
    "ForecastRequest",
    "ForecastSettings",
    "GENERATION_FORWARD",
    "GENERATION_RETROSPECTIVE",
    "KronosForecastService",
    "KronosSampler",
    "MAX_HORIZON_BARS",
    "MAX_PATHS",
    "MIN_LOOKBACK_BARS",
    "MIN_PATHS",
    "MODE_CONTEXT_ONLY",
    "MODE_FILTER",
    "MODEL_REPO",
    "MODEL_REVISION",
    "PathSummary",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STATUS_UNAVAILABLE",
    "SampledSymbol",
    "SamplerError",
    "SamplerSettings",
    "TOKENIZER_REPO",
    "TOKENIZER_REVISION",
    "direction_of",
    "isolated_model_dir",
    "isolated_python",
    "isolated_source_dir",
    "plan_batches",
    "render_context_section",
    "render_forecast_block",
    "series_problem",
    "settings_from_config",
    "stable_seed",
    "verify_artifacts",
]
