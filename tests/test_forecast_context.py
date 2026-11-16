"""Forecast CONTEXT: server/kronos_context — ownership, budget, policy.

The failures reproduced in docs/research/kronos-active-review-20261111.vi.md
that live at this seam:

- a locally built service closes exactly once on success, error or timeout;
  a cycle-owned service stays warm across candidates and closes only after
  its last exclusive lease exits;
- the original absolute cycle deadline covers history, factory construction
  and lease waiting, without becoming the warm sampler's lifetime limit;
  cancellation is rechecked after lease acquisition;
- ``kronos_policy='defer'`` is applied before graph construction: an invalid or
  missing forecast stops the Futures candidate, and retrospective draws stay
  out of active decisions;
- ``history_for`` keeps the cutoff discipline: only closed bars at ``as_of``.

Unit tests: the service is a fake, the venue is a stub, no model, no network.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from server import kronos_context, kronos_lifecycle
from tests.test_forecast_sampler import _write_stub
from tradingagents.dataflows.market_data import Candle
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.contract import (
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
)
from tradingagents.forecast.sampler import DEFAULT_CHUNK_DEADLINE_SECONDS, KronosSampler
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000

_CONFIG = {
    **DEFAULT_CONFIG,
    "kronos_active_profile": "small_base",
    "risk_derivatives_max_hold_hours": 24,
}


class _OwnedService:
    """A fake service that records whether its owner closed it."""

    def __init__(self, block: ForecastBlock | None = None, *, raises: Exception | None = None) -> None:
        self.block = block
        self.raises = raises
        self.closed = False
        self.close_calls = 0
        self.forecast_calls = 0
        self.requests: list[tuple[int | None, float | None]] = []
        self.settings = ForecastSettings(lookback_bars=96)

    def forecast(
        self, series: CandleSeries, *, seed: int,
        lookback_bars: int | None = None, deadline: float | None = None,
    ) -> ForecastBlock:
        self.forecast_calls += 1
        self.requests.append((lookback_bars, deadline))
        if self.raises is not None:
            raise self.raises
        return self.block

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _candles(
    count: int = 40, step: int = _FOUR_HOUR_MS, *, last: int | None = None,
) -> tuple[Candle, ...]:
    if last is None:
        last = 1_800_000_000_000
    last -= last % step
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
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    block = kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_OK
    assert len(_BuiltService.instances) == 1
    assert _BuiltService.instances[0].close_calls == 1, (
        "a service built for the call is closed exactly once by the call"
    )


def test_a_scoped_service_is_closed_on_error(monkeypatch):
    """A crashed model lifecycle still closes: finally, not try-then-forget."""
    _BuiltService.instances.clear()
    monkeypatch.setattr(_BuiltService, "raises_on_forecast", RuntimeError("model exploded"))
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    with pytest.raises(RuntimeError):
        kronos_context.forecast_for_cycle(
            service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="c1",
        )
    assert len(_BuiltService.instances) == 1
    assert _BuiltService.instances[0].close_calls == 1


def test_a_scoped_service_is_closed_once_when_inference_times_out(monkeypatch):
    _BuiltService.instances.clear()
    monkeypatch.setattr(_BuiltService, "raises_on_forecast", TimeoutError("model timed out"))
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    with pytest.raises(TimeoutError, match="model timed out"):
        kronos_context.forecast_for_cycle(
            service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="timeout",
        )
    assert len(_BuiltService.instances) == 1
    assert _BuiltService.instances[0].close_calls == 1


def test_a_supplied_service_is_not_closed_by_the_seam():
    service = _OwnedService(_ok_block())
    kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert not service.closed, "a caller-owned service keeps its own close obligation"


def test_a_supplied_service_retains_its_configured_lookback():
    service = _OwnedService(_ok_block())
    service.settings = ForecastSettings(lookback_bars=96)
    block = kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(_candles(count=201)), symbol="BTC/USDT:USDT",
        config={**_CONFIG, "kronos_active_profile": "mini_2k"},
        as_of=_as_of(), cycle_id="caller-owned-lookback",
    )
    assert block is not None and block.status == STATUS_OK
    assert service.requests == [(None, None)]


def test_a_cancelled_lease_wait_returns_without_waiting_for_the_deadline(monkeypatch):
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG)
    cancelled = threading.Event()
    started = threading.Event()
    outcomes = []

    def forecast():
        started.set()
        outcomes.append(kronos_context.forecast_for_cycle(
            service=None, owner=owner, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="cancel-wait",
            deadline=time.monotonic() + 30.0, cancelled=cancelled,
        ))

    try:
        with owner.lease(None):
            worker = threading.Thread(target=forecast)
            worker.start()
            assert started.wait(2)
            time.sleep(0.05)
            cancelled.set()
            worker.join(timeout=2)
            assert not worker.is_alive()
            assert outcomes and "cancelled" in (outcomes[0].reason or "")
    finally:
        owner.close(timeout=2)


def test_a_cycle_owner_reuses_the_real_sampler_with_variable_history(tmp_path, monkeypatch):
    from tradingagents.forecast import artifact as artifact_module

    samplers = []
    payloads = []
    children = []
    deadlines = []
    factory_kwargs = []
    stub = _write_stub(tmp_path, "ok")

    def build_sampler(**kwargs):
        sampler = KronosSampler(**kwargs, runner=stub)
        communicate = sampler._communicate

        def record_payload(process, payload, *, deadline=None):
            children.append(process)
            payloads.append(payload)
            deadlines.append(deadline)
            return communicate(process, payload, deadline=deadline)

        monkeypatch.setattr(sampler, "_communicate", record_payload)
        samplers.append(sampler)
        return sampler

    factory = KronosForecastService.from_config

    def record_factory(config, **kwargs):
        factory_kwargs.append(kwargs)
        return factory(config, **kwargs)

    monkeypatch.setattr(artifact_module, "isolated_python", lambda: Path(sys.executable))
    monkeypatch.setattr(artifact_module, "isolated_source_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(artifact_module, "isolated_model_dir", lambda: tmp_path / "models")
    monkeypatch.setattr(artifact_module, "verify_artifacts", lambda *_args, **_kwargs: (None, {}))
    monkeypatch.setattr("tradingagents.forecast.service.KronosSampler", build_sampler)
    monkeypatch.setattr(KronosForecastService, "from_config", record_factory)
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG)
    original_deadlines = [time.monotonic() + 30, time.monotonic() + 60]
    blocks = []
    try:
        for count, deadline in zip([201, 96], original_deadlines, strict=True):
            blocks.append(kronos_context.forecast_for_cycle(
                service=None, owner=owner, exchange=_StubVenue(_candles(count=count)),
                symbol="BTC/USDT:USDT", config=_CONFIG, as_of=_as_of(),
                cycle_id="warm-cycle", deadline=deadline,
            ))
        assert all(block is not None and block.status == STATUS_OK for block in blocks)
        assert [block.effective_lookback_bars for block in blocks] == [201, 96]
        assert len(samplers) == 1
        assert factory_kwargs == [{}], "no candidate budget may become a lifetime chunk limit"
        assert samplers[0].settings.chunk_deadline_seconds == DEFAULT_CHUNK_DEADLINE_SECONDS
        assert [len(payload["requests"][0]["candles"]) for payload in payloads] == [201, 96]
        assert [payload["request_id"] for payload in payloads] == [1, 2]
        assert deadlines == original_deadlines
        assert len(children) == 2 and children[0] is children[1]
        assert children[0].poll() is None, "the actual child stays warm between candidates"
    finally:
        owner.close()
    assert children[0].poll() == 0








def test_forecast_after_lease_wait_keeps_the_original_deadline(monkeypatch):
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    clock = [100.0]
    monkeypatch.setattr(kronos_context.time, "monotonic", lambda: clock[0])
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG)
    waiting = threading.Event()
    outcomes = []
    wait = owner._condition.wait

    def record_wait(timeout=None):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(owner._condition, "wait", record_wait)

    def forecast():
        outcomes.append(kronos_context.forecast_for_cycle(
            service=None, owner=owner, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="waited", deadline=150.0,
        ))

    worker = threading.Thread(target=forecast)
    try:
        with owner.lease(None) as service:
            worker.start()
            assert waiting.wait(2)
            assert service.forecast_calls == 0
            clock[0] = 140.0
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(outcomes) == 1 and outcomes[0].status == STATUS_OK
        assert service.requests == [(40, 150.0)]
        assert service.close_calls == 0
    finally:
        owner.close(timeout=0)
        worker.join(timeout=2)
    assert service.close_calls == 1




@pytest.mark.parametrize("owned", [False, True])
def test_service_construction_keeps_the_original_cycle_deadline(monkeypatch, owned):
    _BuiltService.instances.clear()
    clock = [100.0]
    monkeypatch.setattr(kronos_context.time, "monotonic", lambda: clock[0])

    def build(_config, **kwargs):
        assert "chunk_deadline_seconds" not in kwargs
        clock[0] = 140.0
        return _BuiltService()

    monkeypatch.setattr(kronos_context.KronosForecastService, "from_config", build)
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG) if owned else None
    try:
        block = kronos_context.forecast_for_cycle(
            service=None, owner=owner, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="slow-factory", deadline=150.0,
        )
        assert block is not None and block.status == STATUS_OK
        assert _BuiltService.instances[0].requests == [(40, 150.0)]
    finally:
        if owner is not None:
            owner.close()
    assert _BuiltService.instances[0].close_calls == 1


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
@pytest.mark.parametrize("owned", [False, True])
def test_no_forecast_starts_when_construction_spends_the_budget(monkeypatch, stop, owned):
    _BuiltService.instances.clear()
    clock = [100.0]
    cancelled = threading.Event()
    monkeypatch.setattr(kronos_context.time, "monotonic", lambda: clock[0])

    def build(_config, **_kwargs):
        if stop == "deadline":
            clock[0] = 151.0
        else:
            cancelled.set()
        return _BuiltService()

    monkeypatch.setattr(kronos_context.KronosForecastService, "from_config", build)
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG) if owned else None
    try:
        block = kronos_context.forecast_for_cycle(
            service=None, owner=owner, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="spent-factory", deadline=150.0,
            cancelled=cancelled,
        )
        assert block is not None and block.status == STATUS_UNAVAILABLE
        assert stop in block.reason
        assert _BuiltService.instances[0].forecast_calls == 0
    finally:
        if owner is not None:
            owner.close()
    assert _BuiltService.instances[0].close_calls == 1


def test_owner_lease_wait_obeys_the_original_deadline(monkeypatch):
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    owner = kronos_lifecycle.KronosForecastOwner(_CONFIG)
    outcomes = []

    def forecast():
        outcomes.append(kronos_context.forecast_for_cycle(
            service=None, owner=owner, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
            config=_CONFIG, as_of=_as_of(), cycle_id="waiting",
            deadline=time.monotonic() + 0.1,
        ))

    worker = threading.Thread(target=forecast)
    try:
        with owner.lease(None) as service:
            worker.start()
            worker.join(timeout=2)
            assert not worker.is_alive(), "lease waiting must not outlive the cycle budget"
            assert len(outcomes) == 1 and outcomes[0].status == STATUS_UNAVAILABLE
            assert "deadline" in outcomes[0].reason
            assert service.forecast_calls == 0
    finally:
        owner.close(timeout=0)
        worker.join(timeout=2)


# -- the cycle budget bounds the inference ------------------------------------


def test_no_inference_starts_after_the_cycle_deadline(monkeypatch):
    """A spent budget returns ``unavailable`` without building a service."""
    _BuiltService.instances.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
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


@pytest.mark.parametrize("deadline", [None, 150.0])
def test_the_original_cycle_deadline_is_passed_per_request(monkeypatch, deadline):
    _BuiltService.instances.clear()
    _BuiltService.from_config_kwargs.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_context.time, "monotonic", lambda: 100.0)
    kronos_context.forecast_for_cycle(
        service=None, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1", deadline=deadline,
    )
    assert "chunk_deadline_seconds" not in _BuiltService.from_config_kwargs
    assert _BuiltService.instances[0].requests == [(40, deadline)]


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
        venue, "BTC/USDT:USDT", ForecastSettings(lookback_bars=96),
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
        venue, "BTC/USDT:USDT", ForecastSettings(lookback_bars=lookback),
        as_of=_as_of(),
    )
    assert venue.limits == [lookback]
    assert len(candles) == lookback


def test_insufficient_configured_history_is_returned_for_input_validation():
    venue = _StubVenue(_candles(count=201))
    candles = kronos_context.history_for(
        venue, "BTC/USDT:USDT", ForecastSettings(lookback_bars=256),
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


def test_active_forecast_uses_any_contiguous_history_above_floor(monkeypatch):
    _BuiltService.instances.clear()
    _BuiltService.from_config_kwargs.clear()
    monkeypatch.setattr(kronos_context, "KronosForecastService", _BuiltService)
    monkeypatch.setattr(kronos_lifecycle, "KronosForecastService", _BuiltService)

    block = kronos_context.forecast_for_cycle(
        service=None,
        exchange=_StubVenue(_candles(count=201)),
        symbol="BTC/USDT:USDT",
        config=_CONFIG,
        as_of=_as_of(),
        cycle_id="variable-history",
    )

    assert block is not None and block.status == STATUS_OK
    assert _BuiltService.from_config_kwargs["lookback_bars"] == 201


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


def test_an_active_forward_draw_is_accepted():
    block = kronos_context.forecast_for_cycle(
        service=_OwnedService(_ok_block()), exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_OK


def test_a_closed_prediction_window_skips_inference():
    last = int((datetime.now(UTC) - timedelta(hours=9)).timestamp() * 1000)
    candles = _candles(last=last)
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    service = _OwnedService(raises=AssertionError("inference must not start"))

    block = kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(candles), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=as_of, cycle_id="late-cycle",
    )

    assert block is not None and block.status == STATUS_UNAVAILABLE
    assert "already closed" in (block.reason or "")
    assert service.forecast_calls == 0


# -- the failure-policy vocabulary --------------------------------------------


def test_the_policy_vocabulary_has_no_fake_knob():
    settings = ForecastSettings()
    assert kronos_context.policy_for(None) == "defer"
    assert kronos_context.policy_for(_ok_block()) == "use"
    for status in (STATUS_UNAVAILABLE, STATUS_TIMEOUT, STATUS_ERROR):
        failed = ForecastBlock(symbol="BTC/USDT:USDT", status=status, reason="r")
        assert kronos_context.policy_for(failed) == "defer"
    assert settings.settings_problem() is None


def test_active_forecast_is_always_attempted_for_an_eligible_futures_candidate():
    service = _OwnedService(_ok_block())
    block = kronos_context.forecast_for_cycle(
        service=service, exchange=_StubVenue(), symbol="BTC/USDT:USDT",
        config=_CONFIG, as_of=_as_of(), cycle_id="c1",
    )
    assert block is not None and block.status == STATUS_OK
    assert not service.closed, "a caller-owned service keeps its own close obligation"


def test_forecast_settings_derive_full_context_and_horizon_from_max_hold():
    settings = kronos_context.settings_for({
        **DEFAULT_CONFIG,
        "kronos_active_profile": "mini_2k",
        "risk_derivatives_max_hold_hours": 50.5,
    })

    assert settings.profile == "mini_2k"
    assert settings.timeframe == "4h"
    assert settings.lookback_bars == 2048
    assert settings.horizon_hours == 48
    assert settings.paths == 16
    assert settings.temperature == 0.5
    assert not hasattr(settings, "enabled")


@pytest.mark.parametrize(
    ("max_hold_hours", "expected_horizon"),
    [
        (0, 24), (2, 4), (72, 72), (500, 288),
        (None, 24), ("invalid", 24), (float("nan"), 24), (float("inf"), 24),
    ],
)
def test_forecast_horizon_is_derived_and_bounded(max_hold_hours, expected_horizon):
    settings = kronos_context.settings_for({
        **DEFAULT_CONFIG,
        "risk_derivatives_max_hold_hours": max_hold_hours,
    })

    assert settings.horizon_hours == expected_horizon
