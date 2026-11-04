"""Offline direction and scope checks for execution position observations."""

from types import SimpleNamespace

import ccxt
import pytest

from tradingagents.execution.futures_account import observe_futures_positions, read_futures_account

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


def _client(monkeypatch, rows=None):
    client = ccxt.binance()
    client.enable_demo_trading(True)
    client.set_markets([_market()])
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


@pytest.mark.parametrize("row,expected", [
    ({"positionSide": "SHORT", "positionAmt": "-1"}, "short"),
    ({"positionSide": "LONG", "positionAmt": "1"}, "long"),
    ({"positionSide": "BOTH", "positionAmt": "-1"}, "short"),
])
def test_normalized_direction_uses_signed_evidence(monkeypatch, row, expected):
    client, _ = _client(monkeypatch, [_row(**row)])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_symbols == frozenset()
    assert observation.positions[0].side == expected
    assert observation.positions[0].quantity == (-1 if expected == "short" else 1)


@pytest.mark.parametrize("row,expected_unknown", [
    ({"positionSide": "LONG", "positionAmt": "-1"}, {SYMBOL}),
    ({"positionSide": "SHORT", "positionAmt": "1"}, {SYMBOL}),
    ({"positionSide": "INVALID"}, {"BTCUSDT"}),
    ({"positionSide": "BOTH", "positionAmt": None}, {SYMBOL}),
    ({"positionSide": "BOTH", "positionAmt": True}, {"BTCUSDT"}),
])
def test_conflicting_direction_is_unknown(monkeypatch, row, expected_unknown):
    client, _ = _client(monkeypatch, [_row(**row)])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_symbols == expected_unknown
    assert observation.positions == ()


def test_account_claims_complete_scope_for_clean_binance_read(monkeypatch):
    client, _ = _client(monkeypatch, [])
    monkeypatch.setattr(
        client, "fetch_balance",
        lambda params=None: {"info": {"totalMarginBalance": "100"}},
    )

    account = read_futures_account(_Bridge(client))

    assert account.positions == ()
    assert account.positions_scope_complete is True


def test_account_preserves_unverified_scope_for_non_binance_venues():
    exchange = SimpleNamespace(
        fetch_balance=lambda params=None: {"info": {"totalMarginBalance": "100"}},
        fetch_positions=lambda symbols=None, params=None: [],
    )
    bridge = SimpleNamespace(
        exchange_id="okx", config={}, client=lambda: exchange,
    )

    account = read_futures_account(bridge)

    assert account.positions == ()
    assert account.positions_scope_complete is False
