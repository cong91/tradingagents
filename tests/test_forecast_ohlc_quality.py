"""Close-only forecasts and honest OHLC quality for the active path."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.kronos_context import append_to_context, policy_for
from tests.test_forecast_mini_2k import _candles
from tests.test_forecast_sampler import _write_stub
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.ohlc import normalize_ohlc_path, ohlc_quality
from tradingagents.forecast.sampler import KronosSampler, SampledSymbol
from tradingagents.forecast.service import KronosForecastService, _paths_problem
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

SYMBOL = "BTC/USDT:USDT"


def test_2048_bar_sampler_preserves_raw_and_corrected_ohlc_for_active(tmp_path: Path):
    candles = _candles(2048)
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    settings = ForecastSettings(profile="mini_2k", lookback_bars=2048, horizon_hours=24)
    sampler = KronosSampler(
        python=Path(sys.executable), source_dir=tmp_path / "src", model_dir=tmp_path / "models",
        runner=_write_stub(tmp_path, "bad_ohlc"), settings=settings.sampler_settings(),
    )
    try:
        result = sampler.sample([(SYMBOL, "4h", 24, candles, 4242)])[0]
    finally:
        sampler.close()

    assert result.status == "ok", result.reason
    assert len(result.paths) == 16
    assert [path["seed"] for path in result.paths] == list(range(4242, 4258))
    assert result.paths[0]["raw_ohlc"]["opens"] == [102.0] + [100.0] * 5
    assert result.paths[0]["raw_ohlc"]["closes"] == [100.0, 102.0, 100.0, 100.0, 100.0, 100.0]
    assert result.paths[1]["raw_ohlc"]["closes"] == [98.0] + [100.0] * 5
    assert result.paths[0]["highs"] == [102.0, 102.0] + [101.0] * 4
    assert result.paths[1]["lows"] == [98.0] + [99.0] * 5
    assert result.ohlc_quality["status"] == "corrected"
    assert result.ohlc_quality["corrected_bar_count"] == 3
    assert result.ohlc_quality["corrected_path_count"] == 2

    class ReplaySampler:
        def sample(self, requests):
            return [result]

    service = KronosForecastService(
        sampler=ReplaySampler(), settings=settings, artifact_hash="a" * 64, created_at=as_of,
    )
    series = CandleSeries(symbol=SYMBOL, candles=candles, as_of=as_of)
    block = service.forecast(series, seed=4242)
    assert block.status == "ok", block.reason
    quality = block.as_json()["ohlc_quality"]
    assert quality["status"] == "corrected"
    assert quality["corrected_bar_count"] == 3
    assert quality["corrected_path_count"] == 2
    assert block.as_json()["signal_basis"] == "close_only"
    assert block.as_json()["ohlc_paths"] == list(result.paths)
    assert block.direction_counts == {"up": 0, "down": 0, "flat": 16}
    context = append_to_context("existing context", block)
    assert "raw_ohlc" not in context and "ohlc_paths" not in context
    assert "signal_basis=close_only" in context
    assert "OHLC quality=corrected" in context
    assert "OHLC warning:" in context
    assert policy_for(block) == "use"
    assert service.forecast(series, seed=4242) is block


def test_wide_close_band_label_and_quality_survive_cache_and_context():
    candles = _candles(96)
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    series = CandleSeries(symbol=SYMBOL, candles=candles, as_of=as_of)
    paths = []
    for index in range(16):
        end = 120.0 if index < 8 else 80.0
        paths.append({
            "opens": [100.0] * 6,
            "highs": [101.0] * 5 + [max(101.0, end)],
            "lows": [99.0] * 5 + [min(99.0, end)],
            "closes": [100.0] * 5 + [end],
            "seed": 3 + index,
        })
    paths[0]["opens"][0] = 102.0

    class WideSampler:
        calls = 0

        def sample(self, requests):
            self.calls += 1
            return [SampledSymbol(symbol=SYMBOL, status="ok", paths=tuple(paths))]

    sampler = WideSampler()
    service = KronosForecastService(
        sampler=sampler, settings=ForecastSettings(lookback_bars=96), created_at=as_of,
    )
    first = service.forecast(series, seed=3)
    cached = service.forecast(series, seed=3)
    assert sampler.calls == 1 and cached is first
    assert cached.direction == "unclear"
    assert cached.as_json()["direction"] == "unclear"
    assert cached.as_json()["ohlc_quality"]["status"] == "corrected"
    assert cached.as_json()["ohlc_quality"]["corrected_bar_count"] == 1
    context = append_to_context("", cached)
    assert "direction_label=unclear" in context
    assert "OHLC quality=corrected" in context
    assert "OHLC warning:" in context


def test_unassessed_block_does_not_claim_ohlc_is_consistent():
    block = ForecastBlock(symbol=SYMBOL, status="ok", direction="unclear")
    assert block.as_json()["ohlc_quality"]["status"] == "unknown"
    context = append_to_context("", block)
    assert "direction_label=unclear" in context
    assert "OHLC quality=unknown" in context
    assert "do not use it for stop/target" in context


@pytest.mark.parametrize("field", ["opens", "highs", "lows", "closes"])
@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), 0, -1, 10 ** 400])
def test_service_rejects_malformed_and_overflowing_prices(field, bad):
    path = {
        "opens": [100.0], "highs": [101.0], "lows": [99.0], "closes": [100.0],
    }
    path[field] = [bad]
    problem = _paths_problem((path,), requested_paths=1, horizon_bars=1)
    assert problem is not None and field in problem


def test_active_ohlc_normalization_repairs_inverted_wick_bounds():
    path = {"opens": [100.0], "highs": [98.0], "lows": [99.0], "closes": [100.0]}
    assert _paths_problem((path,), requested_paths=1, horizon_bars=1) is None
    normalized = normalize_ohlc_path(path, index=0, horizon_bars=1)
    assert normalized is not None
    assert normalized["highs"] == [100.0]
    assert normalized["lows"] == [99.0]
    quality = ohlc_quality((normalized,))
    assert quality["status"] == "corrected"
    assert quality["corrected_bar_count"] == 1


def test_service_rejects_missing_ohlc():
    path = {"highs": [101.0], "lows": [99.0], "closes": [100.0]}
    problem = _paths_problem((path,), requested_paths=1, horizon_bars=1)
    assert problem is not None and "opens" in problem
