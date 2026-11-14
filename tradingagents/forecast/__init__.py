"""Active Kronos forecast core for the Futures decision path.

This package owns the forecast contract, pinned-artifact verification, bounded
isolated sampling, and the service that turns closed candles into a
decision-grade block. The core decides nothing: it supplies model context
before the Trader and Portfolio Manager decide.
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
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import (
    DEFAULT_HORIZON_HOURS,
    DEFAULT_TIMEFRAME,
    ForecastSettings,
    settings_from_config,
)

__all__ = [
    "CALIBRATION_NOTES",
    "CALIBRATION_SAMPLE_RATIO",
    "CandleSeries",
    "DEFAULT_HORIZON_HOURS",
    "DEFAULT_TIMEFRAME",
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
