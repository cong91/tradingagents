"""Offline end-to-end demo: injected adapters only, no venue/network access."""

import socket
from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.execution.futures_safety import risk_limited_quantity
from tradingagents.execution.futures_scheduler import closed_candle_pair
from tradingagents.execution.futures_signals import FuturesSignalEnvelope
from tradingagents.execution.position_manager import (
    FakePositionAdapter,
    PositionManager,
    PositionSnapshot,
)

pytestmark = pytest.mark.smoke


def test_demo_flow_closed_candles_signal_sizing_and_position_manager(monkeypatch):
    def no_socket(*args, **kwargs):
        raise AssertionError("network access forbidden in demo smoke")

    monkeypatch.setattr(socket, "socket", no_socket)
    now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    one_hour, four_hour = closed_candle_pair(now)
    assert one_hour[1] <= now and four_hour[1] <= now
    envelope = FuturesSignalEnvelope.create(
        symbol="BTC/USDT:USDT", candle_open=one_hour[0], candle_close=one_hour[1],
        direction="short", entry=100, stop=105, targets=(95, 90),
        created_at=now, expires_at=now + timedelta(minutes=30),
        confirmation_candle_open=four_hour[0], strategy="demo", strategy_version="1",
    )
    assert FuturesSignalEnvelope.from_json(envelope.to_json()) == envelope
    qty = risk_limited_quantity(
        side="sell", equity=1000, entry=100, stop_loss=105, take_profit=95,
        leverage=2, risk_fraction=0.01, margin_fraction=0.2,
    )
    assert qty > 0
    adapter = FakePositionAdapter(PositionSnapshot(
        position_key=envelope.signal_id, side="short", entry=100,
        quantity=qty, stop=105, targets=(95,), mark=95,
    ))
    commands = PositionManager(adapter).tick(envelope.signal_id)
    assert commands and all(command.reduce_only and command.side == "buy" for command in commands)
    assert all(call.kind != "create_order" for call in commands)
