"""Active mini_2k forecast request regression at the sampler boundary."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_forecast_sampler import _write_stub
from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import CandleSeries
from tradingagents.forecast.sampler import KronosSampler
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000
_SYMBOL = "BTC/USDT:USDT"


def _candles(count: int) -> tuple[Candle, ...]:
    last = 1_800_000_000_000 - (1_800_000_000_000 % _FOUR_HOUR_MS)
    return tuple(
        Candle(
            symbol=_SYMBOL, timeframe="4h",
            open_time=last - _FOUR_HOUR_MS * (count - 1 - index),
            close_time=last - _FOUR_HOUR_MS * (count - 2 - index),
            open=99.5, high=101.0, low=99.0, close=100.0,
            volume=10.0, is_closed=True,
        )
        for index in range(count)
    )


def test_mini_2k_payload_keeps_2048_candles_and_six_bar_horizon(tmp_path, monkeypatch):
    candles = _candles(count=2050)
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    settings = ForecastSettings(
        profile="mini_2k", lookback_bars=2048,
        timeframe="4h", horizon_hours=24,
    )
    sampler = KronosSampler(
        python=Path(sys.executable), source_dir=tmp_path / "src", model_dir=tmp_path / "models",
        runner=_write_stub(tmp_path, "ok"), settings=settings.sampler_settings(),
    )
    service = KronosForecastService(
        sampler=sampler, settings=settings, artifact_hash="a" * 64, created_at=as_of,
    )
    payloads: list[dict] = []
    communicate = sampler._communicate

    def record_payload(process, payload):
        payloads.append(payload)
        return communicate(process, payload)

    monkeypatch.setattr(sampler, "_communicate", record_payload)
    try:
        block = service.forecast(
            CandleSeries(symbol=_SYMBOL, candles=candles, as_of=as_of), seed=4242,
        )
    finally:
        service.close()

    assert block.status == "ok", block.reason
    assert len(payloads) == 1
    payload = payloads[0]
    expected_rows = [
        [candle.open, candle.high, candle.low, candle.close, candle.volume,
         candle.open_time]
        for candle in candles[-2048:]
    ]
    assert payload["profile"] == "mini_2k"
    assert payload["max_context"] == 2048
    assert payload["pred_len"] == 6
    assert payload["step_ms"] == _FOUR_HOUR_MS
    assert len(payload["requests"]) == 1
    assert payload["requests"][0]["symbol"] == _SYMBOL
    assert payload["requests"][0]["candles"] == expected_rows
    assert len(payload["requests"][0]["candles"]) == 2048


@pytest.mark.parametrize(
    ("profile", "lookback", "context_limit"),
    [("small_base", 513, 512), ("mini_2k", 2049, 2048)],
)
def test_lookback_over_profile_context_fails_before_inference(profile, lookback, context_limit):
    candles = _candles(count=lookback)
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    settings = ForecastSettings(profile=profile, lookback_bars=lookback)
    calls: list[list[tuple]] = []

    class _RecordingSampler:
        def sample(self, requests):
            calls.append(requests)
            raise AssertionError("an invalid profile window must not reach inference")

    service = KronosForecastService(
        sampler=_RecordingSampler(), settings=settings, artifact_hash="a" * 64,
        created_at=as_of,
    )
    block = service.forecast(CandleSeries(symbol=_SYMBOL, candles=candles, as_of=as_of), seed=1)

    assert block.status == "unavailable"
    assert block.reason is not None
    assert f"lookback_bars={lookback}" in block.reason
    assert f", {context_limit}]" in block.reason
    assert profile in block.reason
    assert calls == []
