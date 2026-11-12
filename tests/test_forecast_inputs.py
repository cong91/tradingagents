"""Forecast INPUTS: the discipline a series and a result must pass.

These are the regression tests for the failures reproduced in
docs/research/kronos-active-review-20261111.vi.md: a stale series and a
mixed-symbol series once reached a prompt as a valid forecast, and a one-path
answer to a 16-path request was accepted as ``status=ok``. Everything here is
unit-level: no model, no network, no database — a fake sampler stands in for
the isolated child.

Pinned here:

- ``as_of`` must be timezone-aware (a naive cutoff means the host's locale);
- every candle must name the requested symbol — mixed-symbol input refused;
- a series whose newest bar is older than the newest CLOSED bar at the
  decision moment is STALE and refused even though no bar crosses the cutoff;
- prices are finite and positive, volume finite and non-negative, bar order
  sane (high >= max(open, close), low <= min(open, close));
- the window is gap-free on the closed-bar grid, one timeframe, no forming bar;
- settings bounds are honored (timeframe, horizon bars, lookback window, batch
  ceiling, band spread, failure policy) BEFORE any inference is paid;
- a result is accepted only when it is exactly what was requested: the
  requested number of INDEPENDENT paths, each covering the full horizon, all
  finite positive prices — a one-path answer to a 16-path request is an error,
  never ``ok``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import CandleSeries
from tradingagents.forecast.contract import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from tradingagents.forecast.inputs import series_problem
from tradingagents.forecast.sampler import (
    MAX_BATCH_ROWS,
    MIN_PATHS,
    SampledSymbol,
    SamplerSettings,
    validate_settings,
)
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import (
    ForecastSettings,
    settings_from_config,
)

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000
_ONE_HOUR_MS = 3_600_000


def _candle(
    open_time: int, *, symbol: str = "BTC/USDT:USDT", timeframe: str = "4h",
    close: float = 100.0, step: int = _FOUR_HOUR_MS, **overrides,
) -> Candle:
    base = {
        "symbol": symbol, "timeframe": timeframe, "open_time": open_time,
        "close_time": open_time + step, "open": close - 0.5, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 10.0, "is_closed": True,
    }
    base.update(overrides)
    return Candle(**base)


def _series(
    count: int = 96, *, symbol: str = "BTC/USDT:USDT", timeframe: str = "4h",
    step: int = _FOUR_HOUR_MS, end_open: int | None = None, builder=None,
) -> CandleSeries:
    """A gap-free closed series whose last bar closes exactly at ``as_of``."""
    last = end_open if end_open is not None else 1_800_000_000_000 - (1_800_000_000_000 % step)
    opens = [last - step * (count - 1 - index) for index in range(count)]
    candles = tuple(
        (builder(open_time, index) if builder else _candle(open_time, symbol=symbol, timeframe=timeframe, step=step))
        for index, open_time in enumerate(opens)
    )
    as_of = datetime.fromtimestamp((opens[-1] + step) / 1000, UTC)
    return CandleSeries(symbol=symbol, candles=candles, as_of=as_of)


def _settings(**overrides) -> ForecastSettings:
    base = {"paths": MIN_PATHS, "lookback_bars": 96}
    base.update(overrides)
    return ForecastSettings(**base)


class _FakeSampler:
    """A stand-in for the isolated child; records the calls it received.

    Paths carry the full horizon the request asked for (``horizon_hours`` as
    4h bars), which is what the acceptance rules check.
    """

    def __init__(self, *, status: str = STATUS_OK, paths: int = 16, lag: float = 5.0,
                 close_offset: float = 2.0, symbol: str | None = None) -> None:
        self.status = status
        self.paths = paths
        self.lag = lag
        self.close_offset = close_offset
        self.symbol = symbol
        self.calls: list[list[tuple]] = []

    def sample(self, requests: list[tuple]) -> list[SampledSymbol]:
        self.calls.append(list(requests))
        if self.status != STATUS_OK:
            return [
                SampledSymbol(symbol=row[0], status=self.status, reason="model not installed")
                for row in requests
            ]
        out = []
        for symbol, _timeframe, horizon_hours, candles, _seed in requests:
            bars = max(1, int(horizon_hours) // 4)
            reference = candles[-1].close
            drawn = []
            for index in range(self.paths):
                end = reference + (self.close_offset if index % 2 == 0 else -self.close_offset)
                closes = [
                    reference + (end - reference) * (step / max(1, bars - 1))
                    for step in range(bars)
                ]
                drawn.append({
                    "opens": list(closes),
                    "closes": closes,
                    "highs": [value + 1.0 for value in closes],
                    "lows": [value - 1.0 for value in closes],
                    "seed": 1000 + index,
                })
            out.append(SampledSymbol(
                symbol=self.symbol or symbol, status=STATUS_OK, paths=tuple(drawn),
                duration_ms=self.lag, params={"paths": self.paths},
            ))
        return out

    def close(self) -> None:
        pass


def _service(sampler=None, **overrides) -> KronosForecastService:
    return KronosForecastService(
        sampler=sampler if sampler is not None else _FakeSampler(),
        settings=_settings(**overrides),
        artifact_hash="a" * 64,
        created_at=datetime(2026, 11, 10, tzinfo=UTC),
    )


# -- the decision moment -----------------------------------------------------


def test_a_naive_cutoff_is_refused():
    series = _series()
    naive = CandleSeries(symbol=series.symbol, candles=series.candles,
                         as_of=series.as_of.replace(tzinfo=None))
    reason = series_problem(naive, "4h", lookback_bars=96)
    assert reason is not None and "timezone-aware" in reason


# -- symbol / venue identity -------------------------------------------------


def test_a_mixed_symbol_series_is_refused():
    series = _series()
    foreign = _candle(
        series.candles[-1].open_time, symbol="ETH/USDT:USDT",
        close=series.candles[-1].close,
    )
    broken = CandleSeries(
        symbol=series.symbol, candles=(*series.candles[:-1], foreign), as_of=series.as_of,
    )
    reason = series_problem(broken, "4h", lookback_bars=96)
    assert reason is not None and "not all for symbol" in reason, (
        "a foreign symbol's candle must never ride into a BTC forecast"
    )


# -- staleness ---------------------------------------------------------------


def test_a_stale_series_is_refused_even_without_a_late_bar():
    """The reproduced failure: a day-old series passed the old cutoff check.

    Every bar is closed and none crosses ``as_of`` — yet the newest closed 4h
    bar at the decision moment is missing, so the forecast would answer a
    different question than the one the agents are being asked.
    """
    series = _series()
    stale_open = series.candles[-1].open_time - 6 * _FOUR_HOUR_MS  # a day older
    stale = _series(end_open=stale_open)
    stale_at_decision = CandleSeries(
        symbol=stale.symbol, candles=stale.candles, as_of=series.as_of,
    )
    assert stale_at_decision.candles[-1].close_time < series.as_of.timestamp() * 1000
    reason = series_problem(stale_at_decision, "4h", lookback_bars=96)
    assert reason is not None and "stale" in reason
    assert "newest closed" in reason
    # And the service refuses it before any inference:
    sampler = _FakeSampler()
    block = _service(sampler).forecast(stale_at_decision, seed=1)
    assert block.status == STATUS_UNAVAILABLE
    assert sampler.calls == [], "a stale series must not reach the model"


def test_a_series_ending_on_the_newest_closed_bar_is_not_stale():
    series = _series()
    assert series_problem(series, "4h", lookback_bars=96) is None


def test_a_wrong_candle_duration_is_refused_before_sampling():
    series = _series()
    last = series.candles[-1]
    broken_last = _candle(last.open_time, close_time=last.close_time + _ONE_HOUR_MS)
    broken = CandleSeries(
        symbol=series.symbol,
        candles=(*series.candles[:-1], broken_last),
        as_of=series.as_of + timedelta(hours=1),
    )
    sampler = _FakeSampler()

    reason = series_problem(broken, "4h", lookback_bars=96)
    block = _service(sampler).forecast(broken, seed=1)

    assert reason is not None and "duration" in reason
    assert block.status == STATUS_UNAVAILABLE
    assert sampler.calls == []


# -- price and volume sanity -------------------------------------------------


def test_a_non_finite_or_non_positive_price_is_refused():
    series = _series(count=100)
    nan_series = CandleSeries(
        symbol=series.symbol, candles=tuple(
            _candle(candle.open_time, close=(float("nan") if index == 7 else 100.0 + index))
            for index, candle in enumerate(series.candles)
        ), as_of=series.as_of,
    )
    nan_reason = series_problem(nan_series, "4h", lookback_bars=96)
    assert nan_reason is not None and "non-finite" in nan_reason

    zero = _candle(series.candles[7].open_time, close=0.0)
    broken = CandleSeries(
        symbol=series.symbol,
        candles=(*series.candles[:7], zero, *series.candles[8:]),
        as_of=series.as_of,
    )
    zero_reason = series_problem(broken, "4h", lookback_bars=96)
    assert zero_reason is not None and "non-positive" in zero_reason


def test_inconsistent_bar_order_is_refused():
    series = _series(count=100)
    inverted = _candle(
        series.candles[7].open_time, close=100.0 + 7,
        high=95.0,  # below open/close
    )
    broken = CandleSeries(
        symbol=series.symbol,
        candles=(*series.candles[:7], inverted, *series.candles[8:]),
        as_of=series.as_of,
    )
    reason = series_problem(broken, "4h", lookback_bars=96)
    assert reason is not None and "inconsistent OHLC order" in reason


def test_a_negative_or_nan_volume_is_refused():
    series = _series(count=100)
    bad = _candle(series.candles[7].open_time, close=100.0 + 7, volume=-1.0)
    broken = CandleSeries(
        symbol=series.symbol,
        candles=(*series.candles[:7], bad, *series.candles[8:]),
        as_of=series.as_of,
    )
    reason = series_problem(broken, "4h", lookback_bars=96)
    assert reason is not None and "invalid volume" in reason


# -- the incumbent series discipline (kept) ----------------------------------


def test_a_forming_or_late_candle_is_refused():
    series = _series()
    forming = _candle(series.candles[-1].open_time, is_closed=False)
    broken = CandleSeries(
        symbol=series.symbol, candles=(*series.candles[:-1], forming), as_of=series.as_of,
    )
    reason = series_problem(broken, "4h", lookback_bars=96)
    assert reason is not None and "forming candle" in reason

    late = CandleSeries(
        symbol=series.symbol, candles=series.candles,
        as_of=series.as_of - timedelta(hours=5),
    )
    reason = series_problem(late, "4h", lookback_bars=96)
    assert reason is not None and "after the decision moment" in reason


def test_a_gap_or_a_mixed_timeframe_is_refused():
    series = _series(count=100)
    holed = list(series.candles)
    del holed[20]
    gap_reason = series_problem(
        CandleSeries(symbol=series.symbol, candles=tuple(holed), as_of=series.as_of),
        "4h", lookback_bars=96,
    )
    assert gap_reason is not None and "gap" in gap_reason

    hourly = _series(count=100, timeframe="1h", step=_ONE_HOUR_MS)
    mixed = series_problem(
        CandleSeries(
            symbol=series.symbol,
            candles=(*series.candles[:50], *hourly.candles[:50]),
            as_of=series.as_of,
        ),
        "4h", lookback_bars=96,
    )
    assert mixed is not None and "not all 4h" in mixed


def test_too_little_history_is_refused_with_the_count():
    series = _series(count=6)
    reason = series_problem(series, "4h", lookback_bars=96)
    assert reason is not None and "only 6" in reason and "at least 96" in reason


# -- settings bounds ---------------------------------------------------------


def test_settings_bounds_are_honored_before_any_inference():
    """The timeframe/horizon/lookback/band/policy bounds refuse at the settings."""
    problems = {
        "unsupported timeframe": _settings(timeframe="8h"),
        "horizon not whole bars": _settings(horizon_hours=25),
        "horizon over bound": _settings(horizon_hours=300),
        "lookback under floor": _settings(lookback_bars=8),
        "lookback over model context": _settings(lookback_bars=600),
        "batch rows over ceiling": _settings(max_batch_rows=MAX_BATCH_ROWS + 1),
        "non-finite band spread": _settings(max_band_spread_ratio=float("nan")),
        "non-positive band spread": _settings(max_band_spread_ratio=0.0),
    }
    for label, settings in problems.items():
        assert settings.settings_problem() is not None, label
        sampler = _FakeSampler()
        block = KronosForecastService(
            sampler=sampler, settings=settings, artifact_hash="a" * 64,
            created_at=datetime(2026, 11, 10, tzinfo=UTC),
        ).forecast(_series(), seed=1)
        assert block.status == STATUS_UNAVAILABLE, label
        assert sampler.calls == [], label
    assert settings_from_config({}).profile == "small_base"


def test_a_sampler_floor_is_refused_before_inference():
    assert validate_settings(SamplerSettings(paths=MIN_PATHS - 1)) is not None
    assert validate_settings(SamplerSettings(paths=MIN_PATHS)) is None
    service = _service(paths=3)
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_UNAVAILABLE
    assert block.reason is not None and "floor" in block.reason
    assert block.band == {}, "a refused forecast must not carry numbers"


# -- result acceptance -------------------------------------------------------


def test_a_one_path_answer_to_a_sixteen_path_request_is_an_error():
    """The reproduced failure: requested 16 paths, one path accepted as ok."""
    sampler = _FakeSampler(paths=1)
    service = _service(sampler, paths=16)
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_ERROR
    assert block.reason is not None and "1 path(s) for a 16-path request" in block.reason
    assert block.band == {} and block.direction is None, (
        "a malformed result must not be labelled ok with numbers attached"
    )


def test_a_short_horizon_or_bad_price_is_refused_before_status_ok():
    series = _series()

    def short_path(index):
        return {
            "closes": [74_200.0],  # horizon is 24h/4h = 2 bars; one is not enough
            "highs": [74_300.0], "lows": [74_100.0], "seed": 7000 + index,
        }

    sampler = _FakeSampler()
    original = sampler.sample

    def with_short(requests):
        results = original(requests)
        return [
            SampledSymbol(
                symbol=row.symbol, status=STATUS_OK, duration_ms=row.duration_ms,
                params=row.params,
                paths=tuple(short_path(i) for i in range(len(row.paths))),
            )
            for row in results
        ]

    sampler.sample = with_short
    block = _service(sampler).forecast(series, seed=1)
    assert block.status == STATUS_ERROR
    assert block.reason is not None and "horizon" in block.reason

    sampler2 = _FakeSampler()
    original2 = sampler2.sample

    def with_nan(requests):
        results = original2(requests)
        fixed = []
        for row in results:
            paths = []
            for i, path in enumerate(row.paths):
                closes = list(path["closes"])
                closes[-1] = float("nan") if i == 0 else closes[-1]
                paths.append({**path, "closes": closes})
            fixed.append(SampledSymbol(
                symbol=row.symbol, status=row.status, duration_ms=row.duration_ms,
                params=row.params, paths=tuple(paths),
            ))
        return fixed

    sampler2.sample = with_nan
    nan_block = _service(sampler2).forecast(series, seed=1)
    assert nan_block.status == STATUS_ERROR
    assert nan_block.reason is not None and "non-finite" in nan_block.reason
    assert nan_block.band == {} and nan_block.direction is None


def test_a_result_for_a_different_symbol_is_refused():
    sampler = _FakeSampler(symbol="ETH/USDT:USDT")
    block = _service(sampler).forecast(_series(), seed=1)
    assert block.status == STATUS_ERROR
    assert block.reason is not None and "not the requested" in block.reason


def test_an_ok_block_records_its_created_at():
    service = _service()
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_OK
    assert block.created_at is not None
    assert block.as_json()["created_at"] == block.created_at


# -- the incumbent service behaviors (kept) ----------------------------------


def test_a_wide_band_marks_the_forecast_unclear_without_dropping_it():
    wide = _FakeSampler(close_offset=20.0)
    service = _service(wide, max_band_spread_ratio=0.01)
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_OK
    assert block.direction == "unclear"
    assert block.band_spread_ratio is not None and block.band_spread_ratio > 0.01


def test_a_narrow_band_keeps_the_majority_direction():
    sampler = _FakeSampler(close_offset=0.01)
    service = _service(sampler, max_band_spread_ratio=0.15)
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_OK
    assert block.direction in ("up", "down", "flat")
    assert sum(block.direction_counts.values()) == block.paths


@pytest.mark.parametrize("status", [STATUS_UNAVAILABLE, "timeout", STATUS_ERROR])
def test_a_sampler_failure_is_a_status_with_no_numbers(status):
    service = _service(_FakeSampler(status=status))
    block = service.forecast(_series(), seed=1)
    assert block.status == status
    assert block.reason
    assert block.band == {} and block.direction is None


def test_a_raising_sampler_becomes_an_error_block_not_an_exception():
    class _Boom(_FakeSampler):
        def sample(self, requests):
            raise RuntimeError("child died")

    service = _service(_Boom())
    block = service.forecast(_series(), seed=1)
    assert block.status == STATUS_ERROR
    assert block.reason is not None and "child died" in block.reason
