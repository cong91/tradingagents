"""The active path runs the real pinned model and produces a decision block.

The service uses the pinned weights through ``tradingagents.forecast``, the
package the decision path imports, and renders the block the Trader/PM prompts
will carry.

An absent isolated environment is a FAILURE, never a skip: a green Kronos stage
printed over a model that never ran is exactly the fake pass the runner exists
to prevent. The fixture is deterministic closed candles (mechanism, not alpha);
the model runs for real through its isolated interpreter.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import (
    MIN_PATHS,
    isolated_model_dir,
    isolated_python,
    isolated_source_dir,
    verify_artifacts,
)
from tradingagents.forecast.block import CandleSeries
from tradingagents.forecast.rendering import render_forecast_block
from tradingagents.forecast.sampler import KronosSampler, SamplerSettings
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import ForecastSettings

pytestmark = [pytest.mark.smoke, pytest.mark.integration]

STEP_MS = 14_400_000  # 4h
SYMBOL = "BTC/USDT:USDT"

# Anchored to the CURRENT 4h boundary at import time, so a block drawn from this
# fixture sits in the same forward window a live decision does: the last bar is
# the most recently CLOSED 4h bar and the horizon has not realized. A fixed 2025
# anchor made every block retrospective by construction — true, but it tested
# the fixture, not the runtime property.
_NOW_MS = int(datetime.now(UTC).timestamp() * 1000)
_LAST_OPEN_MS = _NOW_MS - (_NOW_MS % STEP_MS) - STEP_MS
START_MS = _LAST_OPEN_MS - 511 * STEP_MS


def _fixture_candles(count: int = 512) -> list[Candle]:
    """A deterministic closed 4h series (no randomness, no recorded data)."""
    candles = []
    price = 42_000.0
    for index in range(count):
        wave = math.sin(index / 5.0) * 90.0 + math.sin(index / 17.0) * 220.0
        open_price = price
        close_price = 42_000.0 + wave + index * 3.0
        candles.append(Candle(
            symbol=SYMBOL, timeframe="4h",
            open_time=START_MS + STEP_MS * index,
            close_time=START_MS + STEP_MS * (index + 1),
            open=round(open_price, 2),
            high=round(max(open_price, close_price) + 55.0, 2),
            low=round(min(open_price, close_price) - 55.0, 2),
            close=round(close_price, 2),
            volume=float(100 + (index * 3) % 41),
            is_closed=True,
        ))
        price = close_price
    return candles


def _environment_problem() -> str | None:
    python = isolated_python()
    if not python.exists():
        return (
            "the isolated Kronos interpreter is absent: "
            f"{python} — create it outside the repo and rerun; this test does "
            "NOT pass without a real model run"
        )
    missing = [str(path) for path in (isolated_source_dir(), isolated_model_dir()) if not path.exists()]
    if missing:
        return (
            "the pinned Kronos source/weights are absent: "
            + ", ".join(missing)
            + " — this test does NOT pass without a real model run"
        )
    return verify_artifacts(isolated_source_dir(), isolated_model_dir())[0]


def _settings(**overrides) -> ForecastSettings:
    return ForecastSettings(profile="small_base", paths=MIN_PATHS, **overrides)


def test_the_active_service_runs_the_real_model_into_a_decision_block():
    """Pinned weights -> service -> block -> rendered context, for real."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    candles = tuple(_fixture_candles())
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    service = KronosForecastService.from_config({
        "kronos_active_profile": "small_base",
        "risk_derivatives_max_hold_hours": 24,
    })
    try:
        block = service.forecast(
            CandleSeries(symbol=SYMBOL, candles=candles, as_of=as_of), seed=4242,
        )
    finally:
        service.close()

    assert block.status == "ok", block.reason
    assert block.paths == MIN_PATHS
    assert block.per_path_samples == 1
    assert sum(block.direction_counts.values()) == MIN_PATHS
    assert block.direction in ("up", "down", "flat", "unclear")
    assert block.band["low"] <= block.band["p50"] <= block.band["high"]
    assert block.temperature == 0.5
    assert block.model_revision.startswith("NeoQuasar/Kronos-small@")
    assert block.generation_kind == "forward"
    assert block.latency_ms is not None and block.latency_ms > 0
    rendered = render_forecast_block(block)
    assert "NOT a probability" in rendered
    assert f"paths={MIN_PATHS}" in rendered and "up=" in rendered
    assert "data_open_time=" in rendered and "horizon=24h" in rendered


def test_the_active_cycle_path_reads_history_through_the_cutoff():
    """``forecast_for_cycle`` feeds the service the same closed series it read.

    A stub venue returns OHLCV rows; the active path converts them through
    ``fetch_closed_candles`` (which drops forming bars) and hands the result to
    the REAL model. This is the wiring the cycle actually runs.
    """
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    from server import kronos_context

    rows = []
    for candle in _fixture_candles():
        rows.append([
            candle.open_time, candle.open, candle.high, candle.low,
            candle.close, candle.volume,
        ])

    class _StubVenue:
        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None, **_kwargs):
            assert timeframe == "4h"
            return rows

    config = {
        "kronos_active_profile": "small_base",
        "risk_derivatives_max_hold_hours": 24,
    }
    as_of = datetime.fromtimestamp(rows[-1][0] / 1000, UTC) + timedelta(hours=4)
    try:
        block = kronos_context.forecast_for_cycle(
            service=None, exchange=_StubVenue(), symbol=SYMBOL, config=config,
            as_of=as_of, cycle_id="smoke-cycle-20261110",
        )
    finally:
        pass

    assert block is not None and block.status == "ok", (
        None if block is None else block.reason
    )
    assert block.paths == MIN_PATHS
    assert block.latency_ms is not None and block.latency_ms > 0
    context = kronos_context.append_to_context("Futures equity: 1,000 USDT", block)
    assert context.index("Futures equity") < context.index("Kronos forecast")
    assert kronos_context.policy_for(block) == "use"


def test_no_model_library_leaks_into_the_repo_venv():
    import importlib.util

    for name in ("torch", "einops"):
        assert importlib.util.find_spec(name) is None, (
            f"{name} must live in the isolated environment, not the repo venv"
        )


def test_the_deadline_is_honored_by_the_active_path():
    """A config-driven tiny deadline surfaces as a timeout block, not a hang."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    candles = tuple(_fixture_candles())
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    service = KronosForecastService(
        sampler=KronosSampler(
            python=isolated_python(), source_dir=isolated_source_dir(),
            model_dir=isolated_model_dir(),
            settings=SamplerSettings(
                paths=MIN_PATHS, temperature=0.5, chunk_deadline_seconds=0.05,
            ),
        ),
        settings=_settings(),
    )
    try:
        block = service.forecast(
            CandleSeries(symbol=SYMBOL, candles=candles, as_of=as_of), seed=1,
        )
    finally:
        service.close()
    assert block.status == "timeout", (block.status, block.reason)
    assert "deadline" in (block.reason or "")
    assert block.band == {} and block.direction is None
