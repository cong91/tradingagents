"""Contradictory flat rows cannot certify a symbol's position evidence."""

import ccxt
import pytest

from tradingagents.execution.futures_account import (
    observe_futures_positions,
    read_futures_positions,
)

pytestmark = pytest.mark.unit

SYMBOL = "BTC/USDT:USDT"


def _market(market_id="BTCUSDT", symbol="BTC/USDT:USDT", contract_size=1):
    return {
        "id": market_id, "symbol": symbol, "base": symbol.split("/")[0],
        "quote": "USDT", "settle": "USDT", "settleId": "USDT", "type": "swap",
        "spot": False, "swap": True, "future": False, "contract": True,
        "linear": True, "inverse": False, "contractSize": contract_size,
    }


def _row(**changes):
    return {
        "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1",
        "entryPrice": "0", "markPrice": "100", "unRealizedProfit": "2.5",
        "liquidationPrice": "55", "notional": "100", "marginAsset": "USDT",
        "initialMargin": "15", "positionInitialMargin": "10", "updateTime": 123,
        **changes,
    }


def _client(monkeypatch, rows=None, *, markets=None):
    client = ccxt.binance()
    client.enable_demo_trading(True)
    client.set_markets(markets if markets is not None else [_market()])
    calls = []

    def raw_positions(params):
        calls.append(("risk", params))
        if isinstance(rows, Exception):
            raise rows
        return [] if rows is None else rows

    def refuse(*args, **kwargs):
        pytest.fail("account reader must use the raw V3 position endpoint")

    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", raw_positions)
    monkeypatch.setattr(client, "fetch_positions", refuse)
    return client, calls


class _Bridge:
    exchange_id = "binance"

    def __init__(self, client):
        self.exchange = client
        self.config = {"exec_base_url": "https://demo-api.binance.com"}

    def client(self):
        return self.exchange


@pytest.mark.parametrize(("notional", "expected_unknown"), [
    ("1", SYMBOL),
    ("-1", SYMBOL),
    ("1e-400", "BTCUSDT"),
    ("NaN", "BTCUSDT"),
    ("Infinity", "BTCUSDT"),
    (True, "BTCUSDT"),
    ("1_0", "BTCUSDT"),
])
def test_zero_size_with_invalid_or_nonzero_notional_is_unknown(monkeypatch, notional, expected_unknown):
    client, _ = _client(
        monkeypatch,
        [_row(positionAmt="0", notional=notional), _row(symbol="ETHUSDT")],
        markets=[_market(), _market("ETHUSDT", "ETH/USDT:USDT")],
    )
    bridge = _Bridge(client)

    observation = observe_futures_positions(bridge)

    assert observation.unknown_symbols == {expected_unknown}
    assert observation.scope_complete is False
    with pytest.raises(ValueError, match="malformed futures position rows"):
        read_futures_positions(bridge)


@pytest.mark.parametrize("value", [None, "", 0, 0.0, "0", "-0.000"])
def test_zero_size_with_missing_or_zero_notional_certifies_scope(monkeypatch, value):
    client, _ = _client(monkeypatch, [_row(positionAmt="0", notional=value)])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_symbols == frozenset()
    assert observation.positions == ()
    assert observation.scope_complete is True
