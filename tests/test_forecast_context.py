"""Forecast CONTEXT: server/kronos_context — ownership, budget, policy.

The failures reproduced in docs/research/kronos-active-review-20261111.vi.md
that live at this seam:

- ``forecast_for_cycle`` built an unclosed service per candidate (the autoloop
  passed ``service=None``): the model child outlived its owner. Ownership is
  now SCOPED — the service built for the call is closed in ``finally`` on
  success, on error and on timeout — and there is no warm reuse to claim;
- the inference was bounded only by the sampler's own deadline: now the
  remaining CYCLE budget bounds it (no inference starts after the deadline,
  and the sampler's chunk deadline never exceeds what is left), with the
  cancellation event checked before the work;
- ``kronos_policy='defer'`` was recorded but never acted on: the policy
  vocabulary is pinned here (``use``/``defer``/``pass``), and a retrospective
  draw is converted to an explicit failure so research output stays out of
  active decisions;
- ``history_for`` keeps the cutoff discipline: only closed bars at ``as_of``.

Unit tests: the service is a fake, the venue is a stub, no model, no network.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from server import kronos_context
from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.contract import (
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000

_CONFIG = {
    "kronos_active_enabled": True, "kronos_active_paths": 16,
    "kronos_active_timeframe": "4h", "kronos_active_horizon_hours": 24,
}


class _OwnedService:
    """A fake service that records whether its owner closed it."""

    def __init__(self, block: ForecastBlock | None = None, *, raises: Exception | None = None) -> None:
        self.block = block
        self.raises = raises
        self.closed = False
        self.settings = ForecastSettings(enabled=True)

    def forecast(self, series: CandleSeries, *, seed: int) -> ForecastBlock:
        if self.raises is not None:
            raise self.raises
        return self.block

    def close(self) -> None:
        self.closed = True


def _candles(count: int = 40, step: int = _FOUR_HOUR_MS) -> tuple[Candle, ...]:
    last = 1_800_000_000_000 - (1_800_000_000_000 % step)
    opens = [last - step * (count - 1 - index) for index in range(count)]
    return tuple(
        Candle(
            symbol="BTC/USDT:USDT", timeframe="4h", open_time=open_time,
            close_time=open_time + step, open=99.5, high=101.0, low=99.0,
            close=100.0, volume=10.0, is_closed=True,
        )
        for open_time in opens
    )


class _StubVenue:
    def __init__(self, candles: tuple[Candle, ...] | None = None) -> None:
        self.candles = candles or _candles()
        self.limits: list[int | None] = []

    def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None, **_kwargs):
        self.limits.append(limit)
        return [
            [candle.open_time, candle.open, candle.high, candle.low, candle.close, candle.volume]
            for candle in self.candles
        ]


def _ok_block(**overrides) -> ForecastBlock:
    base = {
        "symbol": "BTC/USDT:USDT", "status": STATUS_OK, "direction": "up",
        "generation_kind": GENERATION_FORWARD, "paths": 16,
    }
    base.update(overrides)
    return ForecastBlock(**base)


def _as_of() -> datetime:
    candles = _candles()
    return datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)


# -- scoped lifecycle ownership -----------------------------------------------


class _BuiltService(_OwnedService):
    """A fake from_config-constructible service that records its instances."""

    instances: list[_BuiltService] = []
    from_config_kwargs: dict = {}
    raises_on_forecast: Exception | None = None

    def __init__(self, **_kwargs) -> None:
        super().__init__(_ok_block(), raises=_BuiltService.raises_on_forecast)
        _BuiltService.instances.append(self)

    @classmethod
    def from_config(cls, config, **kwargs) -> _BuiltService:
        _BuiltService.from_config_kwargs = dict(kwargs)
        return cls(**kwargs)


def test_a_scoped_service_is_built_and_closed_on_success(monkeypatch):
    """The reproduced failure: service=None built an unclosed service."""
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    block = kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_OK
    assert len(_BuiltService.instances) == 1
    assert _BuiltService.instances[0].closed, (
        "a service built for the call is closed by the call; nothing outlives its owner"
    )


def test_a_scoped_service_is_closed_on_error(monkeypatch):
    """A crashed model lifecycle still closes: finally, not try-then-forget."""
    _BuiltService.instances.clear()
    _BuiltService.raises_on_forecast = RuntimeError("model exploded")
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    with pytest.raises(RuntimeError):
        kronos_context.forecast_for_cycle(
            service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="c1",
        )
    assert _BuiltService.instances and _BuiltService.instances[0].closed
    _BuiltService.raises_on_forecast = None


def test_a_supplied_service_is_not_closed_by_the_seam():
    service = _OwnedService(_ok_block())
    kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert not service.closed, "a caller-owned service keeps its own close obligation"


# -- the cycle budget bounds the inference ------------------------------------


def test_no_inference_starts_after_the_cycle_deadline(monkeypatch):
    """A spent budget returns ``unavailable`` without building a service."""
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    spent = threading.Event()
    block = kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
        deadline=-1.0, cancelled=spent,
    )
    assert block is not None and block.status == STATUS_UNAVAILABLE
    assert "deadline" in (block.reason or "")
    assert _BuiltService.instances == [], (
        "no model service is built when the cycle budget is spent"
    )


def test_the_remaining_cycle_budget_bounds_the_service_deadline(monkeypatch):
    """The seam turns the remaining time into the sampler's chunk deadline."""
    _BuiltService.instances.clear()
    _BuiltService.from_config_kwargs.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
        deadline=None,
    )
    assert _BuiltService.from_config_kwargs.get("chunk_deadline_seconds") is None, (
        "no deadline: the sampler keeps its own bound"
    )
    remaining_budget = threading.Event()
    deadline = time.monotonic() + 50.0
    _BuiltService.instances.clear()
    _BuiltService.from_config_kwargs.clear()
    kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
        deadline=deadline, cancelled=remaining_budget,
    )
    budget = _BuiltService.from_config_kwargs.get("chunk_deadline_seconds")
    assert budget is not None and 45.0 < budget <= 50.0, (
        "the sampler's deadline never exceeds the cycle's remaining time"
    )


def test_a_cancelled_cycle_reports_the_stop_before_the_work():
    cancelled = threading.Event()
    cancelled.set()
    block = kronos_context.forecast_for_cycle(
        service=_OwnedService(_ok_block()), exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1", cancelled=cancelled,
    )
    assert block is not None and block.status == STATUS_UNAVAILABLE
    assert "cancelled" in (block.reason or "")


def test_the_history_read_cuts_at_the_decision_moment():
    venue = _StubVenue()
    candles = kronos_context.history_for(
        venue, "BTC/USDT:USDT", ForecastSettings(enabled=True),
        as_of=_as_of(),
    )
    assert candles, "the stub venue supplies closed candles"
    assert venue.limits == [96]
    assert all(candle.is_closed for candle in candles)
    assert all(candle.close_time <= int(_as_of().timestamp() * 1000) for candle in candles)


@pytest.mark.parametrize("lookback", [96, 201, 256, 512])
def test_the_history_read_honors_a_configured_large_lookback(lookback):
    venue = _StubVenue(_candles(count=lookback))
    candles = kronos_context.history_for(
        venue, "BTC/USDT:USDT", ForecastSettings(enabled=True, lookback_bars=lookback),
        as_of=_as_of(),
    )
    assert venue.limits == [lookback]
    assert len(candles) == lookback


def test_insufficient_configured_history_is_returned_for_input_validation():
    venue = _StubVenue(_candles(count=201))
    candles = kronos_context.history_for(
        venue, "BTC/USDT:USDT", ForecastSettings(enabled=True, lookback_bars=256),
        as_of=_as_of(),
    )
    assert venue.limits == [256]
    assert len(candles) == 201
    series = CandleSeries(
        symbol="BTC/USDT:USDT", candles=candles, as_of=_as_of(),
    )
    from tradingagents.forecast.inputs import series_problem

    reason = series_problem(series, "4h", lookback_bars=256)
    assert reason is not None and "only 201" in reason and "256" in reason


# -- research output stays out of active decisions -----------------------------


def test_a_retrospective_draw_is_never_fed_to_an_active_decision():
    block = kronos_context.forecast_for_cycle(
        service=_OwnedService(_ok_block(generation_kind=GENERATION_RETROSPECTIVE)),
        exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_UNAVAILABLE
    assert block.reason is not None and "retrospective" in block.reason
    assert block.band == {}, "a research draw must not enter the prompt with numbers"


def test_a_forward_draw_passes_through_unchanged():
    block = kronos_context.forecast_for_cycle(
        service=_OwnedService(_ok_block()), exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_OK


# -- the failure-policy vocabulary --------------------------------------------


def test_the_policy_vocabulary_has_no_fake_knob():
    settings = ForecastSettings(enabled=True, failure_policy="defer")
    assert kronos_context.policy_for(None, settings) == "use"
    assert kronos_context.policy_for(_ok_block(), settings) == "use"
    failed = ForecastBlock(symbol="BTC/USDT:USDT", status=STATUS_UNAVAILABLE, reason="r")
    assert kronos_context.policy_for(failed, settings) == "defer"
    passthrough = ForecastSettings(enabled=True, failure_policy="pass_through")
    assert kronos_context.policy_for(failed, passthrough) == "pass"
    # context_only is retired as a failure policy: it had no behavior distinct
    # from defer, and an unknown value must not silently mean one of the two.
    unknown = ForecastSettings(enabled=True, failure_policy="context_only")
    assert kronos_context.policy_for(failed, unknown) == "defer"
    assert unknown.settings_problem() is not None


def test_off_is_off_and_the_context_is_untouched():
    off_config = {**_CONFIG, "kronos_active_enabled": False}
    service = _OwnedService(_ok_block())
    block = kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=off_config, as_of=_as_of(), cycle_id="c1",
    )
    assert block is None
    assert not service.closed, "off never touched the supplied service"
    incumbent = "Futures equity: 1,000 USDT"
    assert kronos_context.append_to_context(incumbent, None) == incumbent
    assert kronos_context.append_to_context("", None) == ""
