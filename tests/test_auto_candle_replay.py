from dataclasses import replace
from datetime import UTC, datetime

import pytest

from server import autoloop_candles, futures_cycle_telemetry, futures_replay, kronos_context
from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit


def test_recorded_selected_candle_is_rebuilt_and_cutoff_checked():
    step = 900_000
    cutoff = 1_800_000_000_000 // step * step
    selected = Candle("BTC/USDT:USDT", "15m", cutoff - step, cutoff, 100, 101, 99, 100, 1, True)
    one = replace(selected, timeframe="1h", open_time=cutoff - 3_600_000, close_time=cutoff)
    four = replace(selected, timeframe="4h", open_time=cutoff - 14_400_000, close_time=cutoff)
    snapshot = futures_cycle_telemetry.snapshot_payload({
        "one_hour": one, "four_hour": four, "selected_candle": selected,
        "candle_identity": autoloop_candles.candle_identity(selected),
        "decision_as_of": datetime.fromtimestamp(cutoff / 1000, UTC).isoformat(),
    })
    assert futures_replay._rebuild_context(snapshot, selected.symbol)["selected_candle"] == selected
    snapshot["decision_as_of"] = datetime.fromtimestamp((cutoff - step) / 1000, UTC).isoformat()
    assert futures_replay._rebuild_context(snapshot, selected.symbol) is None


def test_forecast_refuses_history_that_changed_since_cycle_snapshot(monkeypatch):
    step = 60_000
    cutoff = 1_800_000_000_000 // step * step
    expected = Candle("BTC/USDT:USDT", "1m", cutoff - step, cutoff, 100, 101, 99, 100, 1, True)
    monkeypatch.setattr(kronos_context, "history_for", lambda *_a, **_k: (replace(expected, close=100.5),))

    class Service:
        def forecast(self, *_args, **_kwargs):
            pytest.fail("revised candle must not reach inference")

    monkeypatch.setattr(kronos_context, "settings_for", lambda _: ForecastSettings(timeframe="1m"))
    result = kronos_context.forecast_for_cycle(
        service=Service(), exchange=object(), symbol=expected.symbol, config={},
        as_of=datetime.fromtimestamp(cutoff / 1000, UTC), cycle_id="cycle", expected_candle=expected,
    )
    assert isinstance(result, ForecastBlock)
    assert result.status == "unavailable"
    assert "differs" in result.reason
