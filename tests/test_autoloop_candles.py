from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server import (
    autoloop_analysis,
    autoloop_candles,
    autoloop_futures_context,
    futures_cycle_telemetry,
)
from server.auto_modes import AutoMode
from tradingagents.auto_timeframes import last_closed_candle
from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import ForecastBlock

pytestmark = pytest.mark.unit


def _candle(timeframe="15m"):
    now = datetime.now(UTC)
    opened, closed = last_closed_candle(now, timeframe)
    return Candle("BTC/USDT:USDT", timeframe, int(opened.timestamp() * 1000),
                  int(closed.timestamp() * 1000), 100, 101, 99, 100, 1, True)


def test_spot_defers_before_graph_if_selected_candle_missing(monkeypatch):
    monkeypatch.setattr(autoloop_analysis.autoloop_execution, "raise_if_mode_disabled", lambda *_: None)
    monkeypatch.setattr(autoloop_candles, "selected_candle", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("missing closed candle")))
    bridge = SimpleNamespace(client=lambda: object(), symbol_for=lambda _: "BTC/USDT")
    result = autoloop_analysis._process_candidate(
        bridge, {"ticker": "BTC-USD"}, {"auto_spot_timeframe": "1m"},
        lambda *_: pytest.fail("graph must not run"), mode=AutoMode.SPOT,
    )
    assert "missing closed candle" in result.reason
    assert result.plan is None and result.executed is False


def test_selected_candle_expires_when_next_candle_closes():
    candle = _candle()
    boundary = datetime.fromtimestamp(candle.close_time / 1000, UTC)
    autoloop_candles.require_current(candle, as_of=boundary + timedelta(minutes=14))
    with pytest.raises(ValueError, match="expired"):
        autoloop_candles.require_current(candle, as_of=boundary + timedelta(minutes=15))


def test_changed_timeframe_invalidates_candle_even_if_restored():
    from tradingagents.dataflows.config import config_key_revision, set_config

    candle = _candle("4h")
    set_config({"auto_futures_timeframe": "4h"})
    revision = config_key_revision("auto_futures_timeframe")
    set_config({"auto_futures_timeframe": "1m"})
    set_config({"auto_futures_timeframe": "4h"})
    with pytest.raises(ValueError, match="timeframe changed"):
        autoloop_candles.require_current(
            candle, config_key="auto_futures_timeframe", config_revision=revision,
        )


def test_cycle_identity_and_snapshot_capture_selected_candle():
    candle = _candle()
    context = {
        "unified_symbol": candle.symbol, "one_hour_open": candle.open_time,
        "four_hour_open": candle.open_time, "selected_candle": candle,
        "candle_identity": autoloop_candles.candle_identity(candle),
    }
    cycle_id, _, _ = futures_cycle_telemetry.cycle_identity(context)
    assert cycle_id.endswith(f":15m:{candle.open_time}")
    snapshot = futures_cycle_telemetry.snapshot_payload(context)
    assert snapshot["selected_candle"]["is_closed"] is True
    assert snapshot["candle_identity"]["open_time"] == candle.open_time


def test_forecast_identity_mismatch_defers(monkeypatch):
    candle = _candle()
    monkeypatch.setattr(autoloop_futures_context.kronos_context, "forecast_for_cycle", lambda **_: ForecastBlock(
        symbol=candle.symbol, status="ok", timeframe="4h", data_open_time=candle.open_time,
    ))
    executor = SimpleNamespace(_bridge=SimpleNamespace(client=lambda: object()))
    block = autoloop_futures_context._kronos_block_for(
        executor, "BTC-USD", {"unified_symbol": candle.symbol, "selected_candle": candle,
                              "candle_identity": autoloop_candles.candle_identity(candle)},
        {"auto_futures_timeframe": "15m"}, cycle_id="cycle",
    )
    assert block.status == "unavailable"
    assert "identity" in block.reason
