import json
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.execution.futures_signals import FuturesSignalEnvelope


def test_signal_stable_serialization_expiry_and_confirmation_boundary():
    utc = UTC
    created = datetime(2026, 10, 7, 8, 0, 5, tzinfo=utc)
    envelope = FuturesSignalEnvelope.create(
        symbol="BTC/USDT:USDT", timeframe="1h", candle_open=datetime(2026, 10, 7, 7, tzinfo=utc),
        candle_close=datetime(2026, 10, 7, 8, tzinfo=utc), direction="long",
        entry=100, stop=95, targets=(110,), created_at=created,
        expires_at=created + timedelta(hours=1),
        confirmation_candle_open=datetime(2026, 10, 7, 4, tzinfo=utc),
        strategy="momentum", strategy_version="v1",
    )
    assert FuturesSignalEnvelope.from_json(envelope.to_json()) == envelope
    assert FuturesSignalEnvelope.create(
        symbol="BTC/USDT:USDT", timeframe="1h", candle_open=datetime(2026, 10, 7, 7, tzinfo=utc),
        candle_close=datetime(2026, 10, 7, 8, tzinfo=utc), direction="long",
        entry=100, stop=95, targets=(110,), created_at=created,
        expires_at=created + timedelta(hours=1),
        confirmation_candle_open=datetime(2026, 10, 7, 4, tzinfo=utc),
        strategy="momentum", strategy_version="v1",
    ).signal_id == envelope.signal_id
    with pytest.raises(ValueError, match="expired"):
        envelope.validate_at(created + timedelta(hours=1))
    with pytest.raises(ValueError, match="closed"):
        FuturesSignalEnvelope.create(
            symbol="BTC/USDT:USDT", timeframe="1h", candle_open=datetime(2026, 10, 7, 8, tzinfo=utc),
            candle_close=datetime(2026, 10, 7, 12, tzinfo=utc), direction="long",
            entry=100, stop=95, targets=(110,), created_at=created,
            expires_at=created + timedelta(hours=1),
            confirmation_candle_open=datetime(2026, 10, 7, 8, tzinfo=utc),
            strategy="momentum", strategy_version="v1",
        )


def test_signal_deserialization_rejects_tampered_candle_close():
    now = datetime(2026, 10, 7, 8, tzinfo=UTC)
    envelope = FuturesSignalEnvelope.create(
        symbol="BTC/USDT:USDT", timeframe="15m", candle_open=now - timedelta(minutes=15),
        candle_close=now, direction="long", entry=100, stop=95, targets=(110,),
        created_at=now, expires_at=now + timedelta(minutes=15),
        confirmation_candle_open=now - timedelta(hours=4), strategy="momentum", strategy_version="v1",
    )
    payload = json.loads(envelope.to_json())
    payload["candle_close"] = (now + timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="invalid futures signal"):
        FuturesSignalEnvelope.from_json(json.dumps(payload))


def test_short_signal_requires_correct_bracket_geometry():
    now = datetime(2026, 10, 7, 8, tzinfo=UTC)
    with pytest.raises(ValueError, match="short"):
        FuturesSignalEnvelope.create(
            symbol="BTC/USDT:USDT", timeframe="1h", candle_open=now - timedelta(hours=1),
            candle_close=now, direction="short", entry=100, stop=95,
            targets=(90,), created_at=now, expires_at=now + timedelta(minutes=10),
            confirmation_candle_open=now - timedelta(hours=4),
            strategy="momentum", strategy_version="v1",
        )
