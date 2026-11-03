"""Binance raw V3 position scope evidence for the account reader."""

from types import SimpleNamespace

import ccxt
import pytest

from tradingagents.execution.futures_account import (
    observe_futures_positions,
    read_futures_account,
)

pytestmark = pytest.mark.unit


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


def _client(monkeypatch, rows=None, *, profile="demo", markets=None):
    client = ccxt.binance()
    if profile == "demo":
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
    monkeypatch.setattr(client, "create_order", refuse)
    monkeypatch.setattr(client, "cancel_order", refuse)
    return client, calls


class _Bridge:
    exchange_id = "binance"

    def __init__(self, client, profile="demo"):
        self.exchange = client
        self.config = {
            "exec_base_url": f"https://{'demo-api' if profile == 'demo' else 'api'}.binance.com",
        }

    def client(self):
        return self.exchange


def test_empty_raw_list_certifies_account_wide_scope(monkeypatch):
    client, calls = _client(monkeypatch, [])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.positions == ()
    assert observation.unknown_symbols == frozenset()
    assert observation.unknown_scope is False
    assert observation.scope_complete is True
    assert calls == [("risk", {})]


def test_clean_read_flows_complete_scope_through_read_futures_account(monkeypatch):
    client, _ = _client(monkeypatch, [])
    monkeypatch.setattr(
        client, "fetch_balance",
        lambda params=None: {"info": {"totalMarginBalance": "100"}},
    )

    snapshot = read_futures_account(_Bridge(client))

    assert snapshot.positions == ()
    assert snapshot.positions_scope_complete is True


def test_zero_entry_price_row_is_kept_and_certifies_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row()])

    observation = observe_futures_positions(_Bridge(client))

    assert [
        (p.symbol, p.side, p.contracts, p.entry_price) for p in observation.positions
    ] == [("BTC/USDT:USDT", "long", 1.0, 0.0)]
    assert observation.unknown_symbols == frozenset()
    assert observation.unknown_scope is False
    assert observation.scope_complete is True


def test_invalid_row_fails_scope_but_keeps_held_position(monkeypatch):
    client, _ = _client(
        monkeypatch, [_row(), _row(symbol="ETHUSDT", positionAmt="NaN")],
        markets=[_market(), _market("ETHUSDT", "ETH/USDT:USDT")],
    )

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"ETHUSDT"}
    assert [p.symbol for p in observation.positions] == ["BTC/USDT:USDT"]


def test_non_dict_row_sets_unknown_scope(monkeypatch):
    client, _ = _client(monkeypatch, ["private-secret"])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.positions == ()
    assert observation.unknown_symbols == frozenset()
    assert observation.unknown_scope is True
    assert observation.scope_complete is False


def test_pagination_cursor_row_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(nextPageCursor="cursor-1")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"BTCUSDT"}
    assert observation.positions == ()


def test_unknown_market_id_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(symbol="XYZUSDT")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"XYZUSDT"}
    assert observation.positions == ()


def test_duplicate_position_rows_fail_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(), _row()])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"BTCUSDT"}
    assert observation.positions == ()


def test_inconsistent_position_mode_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(), _row(positionSide="LONG")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"BTCUSDT"}
    assert observation.positions == ()


def test_invalid_margin_currency_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(marginAsset="BTC")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"BTCUSDT"}
    assert observation.positions == ()


def test_zero_size_row_with_conflicting_notional_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(positionAmt="0", notional="4")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is False
    assert observation.unknown_symbols == {"BTC/USDT:USDT"}
    assert observation.positions == ()


def test_clean_zero_size_row_certifies_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row(positionAmt="0", notional="0")])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.positions == ()
    assert observation.unknown_symbols == frozenset()
    assert observation.scope_complete is True


def test_okx_bridge_keeps_scope_incomplete():
    exchange = SimpleNamespace(
        fetch_balance=lambda params=None: {"info": {"data": []}},
        fetch_positions=lambda symbols=None, params=None: [],
    )
    bridge = SimpleNamespace(
        exchange_id="okx", config={}, client=lambda: exchange,
    )

    observation = observe_futures_positions(bridge)

    assert observation.positions == ()
    assert observation.unknown_symbols == frozenset()
    assert observation.unknown_scope is False
    assert observation.scope_complete is False


def test_bad_v3_route_propagates_before_any_request(monkeypatch):
    client, calls = _client(monkeypatch, [])
    client.urls["api"]["fapiPrivateV3"] = "https://fapi.binance.com/fapi/v3"

    with pytest.raises(ValueError, match="private routes"):
        observe_futures_positions(_Bridge(client))

    assert calls == []


def test_raising_reader_propagates(monkeypatch):
    client, calls = _client(monkeypatch, RuntimeError("private-secret"))

    with pytest.raises(RuntimeError, match="private-secret"):
        observe_futures_positions(_Bridge(client))

    assert calls == [("risk", {})]


def test_production_profile_clean_read_certifies_scope(monkeypatch):
    client, _ = _client(monkeypatch, [], profile="production")

    observation = observe_futures_positions(_Bridge(client, profile="production"))

    assert observation.scope_complete is True


def test_client_rebound_during_read_fails_closed(monkeypatch):
    client, _ = _client(monkeypatch, [])
    bridge = _Bridge(client)
    rebound = ccxt.binance()
    first_read = {"done": False}

    def unstable_client():
        if not first_read["done"]:
            first_read["done"] = True
            return client
        return rebound

    bridge.client = unstable_client

    observation = observe_futures_positions(bridge)

    assert observation.positions == ()
    assert observation.unknown_scope is True
    assert observation.scope_complete is False
