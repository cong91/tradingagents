"""Forecast CACHE: identity, reuse rules and the look-ahead guard.

These are the regression tests for the failure reproduced in
docs/research/kronos-active-review-20261111.vi.md: the cache key carried the
candle TIMESTAMP but not the candle CONTENT, so a corrected close at the same
timestamp was served the old block; it also lacked venue scope, the decision
cutoff, and the setting that can flip a direction (the band-spread bound).

Pinned here:

- the key changes with candles' CONTENT (same timestamps, edited close),
  venue scope, cutoff, seed, model revision, artifact hash, mode and every
  classification-affecting setting (paths, temperature, horizon, band bound);
- a cached block is served only for the same identity, and a new closed bar
  misses;
- a ``retrospective`` entry is never stored and never served forward;
- a HISTORICAL decision (its first predicted bar already closed) bypasses the
  cache entirely: created_at before the outcome bar closes is NOT proof the
  forecast existed at the decision timestamp, so no cached entry may be served
  into a historical decision;
- the cache is bounded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.cache import ForecastCache, cache_key, content_digest
from tradingagents.forecast.contract import (
    GENERATION_RETROSPECTIVE,
    STATUS_OK,
)
from tradingagents.forecast.inputs import series_problem
from tradingagents.forecast.sampler import MIN_PATHS
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import MODE_CONTEXT_ONLY, ForecastSettings

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000


def _candle(open_time: int, *, close: float = 100.0, step: int = _FOUR_HOUR_MS) -> Candle:
    return Candle(
        symbol="BTC/USDT:USDT", timeframe="4h", open_time=open_time,
        close_time=open_time + step, open=close - 0.5, high=close + 1.0,
        low=close - 1.0, close=close, volume=10.0, is_closed=True,
    )


def _series(
    count: int = 96, *, end_open: int | None = None, step: int = _FOUR_HOUR_MS,
    close: float = 100.0,
) -> CandleSeries:
    last = end_open if end_open is not None else 1_800_000_000_000 - (1_800_000_000_000 % step)
    opens = [last - step * (count - 1 - index) for index in range(count)]
    candles = tuple(
        _candle(open_time, close=close + index, step=step) for index, open_time in enumerate(opens)
    )
    return CandleSeries(
        symbol="BTC/USDT:USDT", candles=candles,
        as_of=datetime.fromtimestamp((opens[-1] + step) / 1000, UTC),
    )


def _settings(**overrides) -> ForecastSettings:
    base = {"enabled": True, "mode": MODE_CONTEXT_ONLY, "paths": MIN_PATHS}
    base.update(overrides)
    return ForecastSettings(**base)


class _FakeSampler:
    def __init__(self) -> None:
        self.calls: list[list[tuple]] = []

    def sample(self, requests: list[tuple]):
        from tradingagents.forecast.sampler import SampledSymbol

        self.calls.append(list(requests))
        out = []
        for symbol, _timeframe, horizon_hours, candles, _seed in requests:
            bars = max(1, int(horizon_hours) // 4)
            reference = candles[-1].close
            drawn = tuple(
                {
                    "closes": [reference + 2.0 * (step / max(1, bars - 1)) for step in range(bars)],
                    "highs": [reference + 3.0] * bars,
                    "lows": [reference - 1.0] * bars,
                    "seed": 1000 + index,
                }
                for index in range(16)
            )
            out.append(SampledSymbol(
                symbol=symbol, status=STATUS_OK, paths=drawn,
                duration_ms=5.0, params={"paths": 16},
            ))
        return out

    def close(self) -> None:
        pass


def _service(sampler=None, *, created_at=None, venue="binance", **overrides) -> KronosForecastService:
    return KronosForecastService(
        sampler=sampler if sampler is not None else _FakeSampler(),
        settings=_settings(**overrides),
        artifact_hash="a" * 64,
        venue=venue,
        created_at=created_at or datetime(2026, 11, 10, 12, tzinfo=UTC),
    )


# -- the content digest ------------------------------------------------------


def test_the_content_digest_changes_with_edited_candle_content():
    """The reproduced failure: an edited close at the same timestamp reused."""
    series = _series()
    edited = list(series.candles)
    edited[-1] = Candle(
        **{**edited[-1].__dict__, "close": 999.0, "open": 998.0},
    )
    assert content_digest(series.candles) != content_digest(tuple(edited))
    assert content_digest(series.candles) == content_digest(series.candles), (
        "the digest must be stable for identical content"
    )


# -- key identity ------------------------------------------------------------


def test_the_cache_key_changes_with_every_input_that_changes_the_meaning():
    settings = _settings()
    series = _series()
    base = {
        "symbol": "BTC/USDT:USDT", "timeframe": "4h",
        "data_open_time": series.candles[-1].open_time,
        "settings": settings, "model_revision": "kronos@1",
        "artifact_hash": "a" * 64, "seed": 7,
        "venue": "binance", "cutoff_ms": int(series.as_of.timestamp() * 1000),
        "data_digest": content_digest(series.candles),
    }
    reference = cache_key(**base)
    other_series = _series(close=200.0)
    variants = {
        "symbol": {**base, "symbol": "ETH/USDT:USDT"},
        "venue": {**base, "venue": "bybit"},
        "timeframe": {**base, "timeframe": "1h"},
        "data_open_time": {**base, "data_open_time": base["data_open_time"] + _FOUR_HOUR_MS},
        "cutoff": {**base, "cutoff_ms": base["cutoff_ms"] + 1},
        "data_digest": {**base, "data_digest": content_digest(other_series.candles)},
        "paths": {**base, "settings": _settings(paths=32)},
        "temperature": {**base, "settings": _settings(temperature=1.0)},
        "horizon": {**base, "settings": _settings(horizon_hours=12)},
        "band_bound": {**base, "settings": _settings(max_band_spread_ratio=0.05)},
        "lookback": {**base, "settings": _settings(lookback_bars=64)},
        "model_revision": {**base, "model_revision": "kronos@2"},
        "artifact_hash": {**base, "artifact_hash": "b" * 64},
        "seed": {**base, "seed": 8},
    }
    for name, variant in variants.items():
        assert cache_key(**variant) != reference, f"the key ignored {name}"


# -- reuse rules -------------------------------------------------------------


def test_a_cached_block_is_served_for_the_same_identity_only():
    sampler = _FakeSampler()
    service = _service(sampler)
    series = _series()
    first = service.forecast(series, seed=3)
    calls = len(sampler.calls)
    second = service.forecast(series, seed=3)
    assert first is second
    assert len(sampler.calls) == calls, "an identical request must not re-run the model"
    # A NEW closed bar is a new input and must miss the cache.
    advanced = _series(end_open=series.candles[-1].open_time + _FOUR_HOUR_MS)
    service.forecast(advanced, seed=3)
    assert len(sampler.calls) == calls + 1


def test_an_edited_close_at_the_same_timestamp_misses_the_cache():
    """The reproduced failure, end to end: same timestamps, corrected price."""
    sampler = _FakeSampler()
    service = _service(sampler)
    series = _series()
    first = service.forecast(series, seed=3)
    assert first.status == STATUS_OK
    edited = list(series.candles)
    edited[-1] = Candle(
        **{**edited[-1].__dict__, "open": 123_455.5, "close": 123_456.0,
           "high": 123_457.0, "low": 123_455.0},
    )
    edited_series = CandleSeries(symbol=series.symbol, candles=tuple(edited), as_of=series.as_of)
    assert series_problem(edited_series, "4h", lookback_bars=96) is None, (
        "the edited series must still be a valid series for this test to bite"
    )
    service.forecast(edited_series, seed=3)
    assert len(sampler.calls) == 2, "a corrected close is a DIFFERENT input, not the old block"


def test_a_temperature_change_is_not_served_from_cache():
    plain = _service(_FakeSampler())
    series = _series()
    first = plain.forecast(series, seed=11)
    assert first.status == STATUS_OK
    # Same candles, same seed, different temperature: a DIFFERENT cycle's
    # settings, so the stored ratio means something else and must not be reused.
    hotter = KronosForecastService(
        sampler=plain._sampler, settings=_settings(temperature=1.0),
        artifact_hash="a" * 64, venue="binance",
        created_at=datetime(2026, 11, 10, 12, tzinfo=UTC),
        cache=plain.cache,
    )
    second = hotter.forecast(series, seed=11)
    assert second.temperature == 1.0
    assert second is not first


def test_a_venue_change_is_not_served_from_cache():
    """The same contract string on two venues is not the same market."""
    sampler = _FakeSampler()
    service = _service(sampler, venue="binance")
    series = _series()
    assert service.forecast(series, seed=5).status == STATUS_OK
    elsewhere = KronosForecastService(
        sampler=sampler, settings=_settings(), artifact_hash="a" * 64, venue="bybit",
        created_at=datetime(2026, 11, 10, 12, tzinfo=UTC), cache=service.cache,
    )
    assert len(sampler.calls) == 1
    elsewhere.forecast(series, seed=5)
    assert len(sampler.calls) == 2, "venue scope is part of the identity"


# -- the look-ahead guard ----------------------------------------------------


def test_a_retrospective_entry_is_never_served_forward():
    cache = ForecastCache()
    cache.put("k", ForecastBlock(
        symbol="BTC/USDT:USDT", status=STATUS_OK, direction="up",
        generation_kind=GENERATION_RETROSPECTIVE,
    ))
    assert cache.get("k") is None, "a block drawn after its outcome realized is look-ahead"


def test_a_historical_decision_never_serves_the_cache():
    """The decision's first predicted bar already closed: cache bypassed.

    The service's clock is pinned AFTER the first outcome bar closes, so even a
    block stored by an earlier LIVE decision of the same boundary cannot be
    served: created_at before the outcome bar closes is not proof the forecast
    existed at this decision's timestamp.
    """
    sampler = _FakeSampler()
    series = _series()
    # A LIVE decision of this boundary: created one minute after the cutoff,
    # inside the first predicted bar, so the draw is forward and cached.
    live = _service(sampler, created_at=series.as_of + timedelta(minutes=1))
    assert live.forecast(series, seed=3).status == STATUS_OK
    assert len(sampler.calls) == 1

    historical_sampler = _FakeSampler()
    historical = KronosForecastService(
        sampler=historical_sampler, settings=_settings(), artifact_hash="a" * 64,
        venue="binance",
        created_at=series.as_of + timedelta(hours=5),  # the first bar has closed
        cache=live.cache,  # the SAME cache the live decision stored into
    )
    block = historical.forecast(series, seed=3)
    assert block.generation_kind == GENERATION_RETROSPECTIVE, (
        "a historical decision's draw is research output, labelled retrospective"
    )
    assert len(historical_sampler.calls) == 1, (
        "the historical decision drew fresh (cache bypassed)"
    )
    assert len(sampler.calls) == 1, "the live entry was never served into the replay"


def test_a_live_decision_still_reuses_the_cache():
    sampler = _FakeSampler()
    series = _series()
    service = _service(sampler, created_at=series.as_of + timedelta(minutes=1))
    first = service.forecast(series, seed=3)
    second = service.forecast(series, seed=3)
    assert first is second and first.generation_kind == "forward"


def test_the_cache_is_bounded():
    cache = ForecastCache(max_entries=2)
    for index in range(5):
        cache.put(f"k{index}", ForecastBlock(symbol="S", status=STATUS_OK))
    assert len(cache) == 2
