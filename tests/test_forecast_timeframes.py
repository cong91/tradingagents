from datetime import UTC, datetime

import pytest

from tradingagents.auto_timeframes import timeframe_ms
from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import CandleSeries
from tradingagents.forecast.inputs import series_problem
from tradingagents.forecast.kronos_runner import _timestamp_sequences
from tradingagents.forecast.settings import ForecastSettings, settings_from_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "4h"])
def test_selected_timeframe_controls_horizon_and_predictor_timestamps(timeframe):
    settings = settings_from_config({"auto_futures_timeframe": timeframe, "risk_derivatives_max_hold_hours": 1})
    step = timeframe_ms(timeframe)
    assert settings.settings_problem() is None
    assert settings.horizon_bars == max(1, 3_600_000 // step)
    assert settings.lookback_bars == 512
    base = 1_800_000_000_000 // step * step
    rows = [[100, 101, 99, 100, 1, base + index * step] for index in range(32)]
    x, y = _timestamp_sequences(rows, step, settings.horizon_bars, 512)
    assert x == [row[5] for row in rows]
    assert y == [x[-1] + step * index for index in range(1, settings.horizon_bars + 1)]


def test_invalid_selection_is_not_reinterpreted_as_four_hours():
    settings = settings_from_config({"auto_futures_timeframe": "5m"})
    assert settings.timeframe == "5m"
    assert settings.horizon_hours is None
    assert "unsupported" in settings.settings_problem()


@pytest.mark.parametrize("problem", ["gap", "off_grid", "short", "context", "bad_time"])
def test_runner_refuses_unusable_timestamp_sequence(problem):
    step = 60_000
    base = 1_800_000_000_000 // step * step
    rows = [[100, 101, 99, 100, 1, base + index * step] for index in range(32)]
    context = 512
    if problem == "gap":
        rows[-1][5] += step
    elif problem == "off_grid":
        rows[-1][5] += 1
    elif problem == "short":
        rows.pop()
    elif problem == "context":
        context = 31
    else:
        rows[-1][5] = "1800000000000"
    with pytest.raises(ValueError):
        _timestamp_sequences(rows, step, 1, context)


def test_series_rejects_aligned_duration_with_off_grid_opens():
    step = 900_000
    base = 1_800_000_000_000 // step * step + 1
    candles = tuple(Candle("BTC", "15m", base + i * step, base + (i + 1) * step,
                           100, 101, 99, 100, 1, True) for i in range(32))
    series = CandleSeries("BTC", candles, datetime.fromtimestamp((base + 32 * step) / 1000, UTC))
    assert "off-grid" in series_problem(series, "15m", lookback_bars=32)


def test_fractional_hour_horizon_is_exactly_one_minute():
    settings = ForecastSettings(timeframe="1m", horizon_hours=1 / 60, lookback_bars=32)
    assert settings.settings_problem() is None
    assert settings.horizon_bars == 1
