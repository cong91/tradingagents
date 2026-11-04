"""Risk markers used by derivatives execution must not price spot lots as futures."""

import pytest

from tradingagents.execution.derivatives import DerivativesExecutor

pytestmark = pytest.mark.unit


class RecordingExchange:
    def __init__(self):
        self.symbols = []

    def fetch_ticker(self, symbol):
        self.symbols.append(symbol)
        return {"last": 100.0}


def test_derivatives_executor_does_not_replace_shared_spot_marker():
    exchange = RecordingExchange()
    executor = DerivativesExecutor(exchange_factory=lambda: exchange)

    assert executor.risk_guard.price_of("BTC-USD") == 100.0
    assert exchange.symbols == ["BTC/USDT"]
