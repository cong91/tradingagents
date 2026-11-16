"""Credential-free public Futures venue client tests."""

import ccxt
import pytest

from server import forecast_evaluation_venue as venue

pytestmark = pytest.mark.unit


def test_target_candle_exchange_leaves_other_exchanges_unchanged():
    exchange = ccxt.binance()
    assert venue.target_candle_exchange(exchange, 3_600_000) is exchange


def test_bybit_target_candle_exchange_refuses_missing_start_or_wrong_limit():
    class Bybit:
        id = "bybit"

        def fetch_ohlcv(self, *_args, **_kwargs):
            pytest.fail("must not issue an unbounded request")

    exchange = venue.target_candle_exchange(Bybit(), 3_600_000)
    with pytest.raises(TypeError):
        exchange.fetch_ohlcv("BTC/USDT:USDT", timeframe="1h", limit=1)
    with pytest.raises(ValueError, match="bounds"):
        exchange.fetch_ohlcv("BTC/USDT:USDT", timeframe="1h", since=3_600_000, limit=2)
    with pytest.raises(ValueError, match="bounds"):
        exchange.fetch_ohlcv("BTC/USDT:USDT", timeframe="1h", since=0, limit=1)


def test_public_binance_client_uses_futures_and_demo_profile(monkeypatch):
    captured = {}

    class FakeBinance(ccxt.Exchange):
        def __init__(self, options):
            captured["options"] = options
            self.options = options["options"]
            self.apiKey = self.secret = self.password = self.uid = ""
            self.demo = False
            self.loaded = False

        def enable_demo_trading(self, enabled):
            self.demo = enabled

        def load_markets(self):
            self.loaded = True

    monkeypatch.setattr(ccxt, "binance", FakeBinance)
    exchange = venue.public_futures_exchange({
        "exchange_id": "binance", "venue_profile": "demo", "sandbox": False,
    })
    assert captured["options"]["options"]["defaultType"] == "future"
    assert exchange.demo is True
    assert exchange.loaded is True
    assert not any((exchange.apiKey, exchange.secret, exchange.password, exchange.uid))


def test_public_client_rejects_credentials_and_closes_failed_client(monkeypatch):
    instances = []

    class CredentialedExchange(ccxt.Exchange):
        def __init__(self, options):
            self.options = options["options"]
            self.apiKey, self.secret, self.password, self.uid = "credential", "", "", ""
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(ccxt, "okx", CredentialedExchange)
    with pytest.raises(RuntimeError, match="credential-free"):
        venue.public_futures_exchange({"exchange_id": "okx"})
    assert instances[0].closed is True
