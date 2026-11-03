import ccxt
import pytest

from tradingagents.execution.futures_account import observe_futures_positions, read_futures_account


class _Exchange:
    def __init__(self, balance, positions):
        self.balance = balance
        self.positions = positions
        self.calls = []

    def fetch_balance(self, params=None):
        self.calls.append(("balance", params))
        return self.balance

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("positions", symbols, params))
        return self.positions

    def market(self, symbol):
        if "/" not in symbol:
            return {}
        pair, _, settle = symbol.partition(":")
        _, _, quote = pair.partition("/")
        return {"quote": quote, "settle": settle or quote, "inverse": False}


class _Bridge:
    exchange_id = "binance"
    config = {"exec_base_url": "https://demo-api.binance.com"}

    def __init__(self, exchange):
        self.exchange = exchange

    def client(self):
        return self.exchange


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


def test_read_futures_account_normalizes_positions_and_account_values(monkeypatch):
    client, calls = _client(
        monkeypatch,
        [
            {
                "symbol": "INJUSDT", "positionSide": "BOTH", "positionAmt": "-12",
                "entryPrice": "25.0", "markPrice": "24.0", "liquidationPrice": "30.0",
                "unRealizedProfit": "12.0", "notional": "288.0",
                "positionInitialMargin": "96.0", "leverage": "3",
                "marginType": "isolated", "marginAsset": "USDT",
            },
            _row(symbol="BTCUSDT", positionAmt="0", notional="0"),
        ],
        markets=[_market(), _market("INJUSDT", "INJ/USDT:USDT")],
    )

    def balance(params=None):
        calls.append(("balance", params))
        return {"info": {
            "totalWalletBalance": "1000.0",
            "totalUnrealizedProfit": "25.0",
            "totalMarginBalance": "1025.0",
            "availableBalance": "900.0",
        }}

    monkeypatch.setattr(client, "fetch_balance", balance)

    snapshot = read_futures_account(_Bridge(client))

    assert snapshot.status == "ok"
    assert snapshot.venue_profile == "demo"
    assert snapshot.equity == pytest.approx(1025.0)
    assert snapshot.wallet_balance == pytest.approx(1000.0)
    assert snapshot.available_balance == pytest.approx(900.0)
    assert snapshot.unrealized_pnl == pytest.approx(25.0)
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.symbol == "INJ/USDT:USDT"
    assert position.side == "short"
    assert position.contracts == pytest.approx(12.0)
    assert position.quantity == pytest.approx(-12.0)
    assert position.notional == pytest.approx(288.0)
    assert position.margin == pytest.approx(96.0)
    assert position.price_currency == "USDT"
    assert position.notional_currency == "USDT"
    assert position.margin_currency == "USDT"
    assert position.unrealized_pnl_currency == "USDT"
    assert calls == [
        ("balance", {"type": "future", "subType": "linear"}),
        ("risk", {}),
    ]


def test_read_futures_account_applies_linear_contract_size_to_quantity(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [{
            "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-250",
            "entryPrice": "100000", "markPrice": "99000", "marginAsset": "USDT",
        }],
        markets=[_market(contract_size=0.001)],
    )
    monkeypatch.setattr(client, "fetch_balance", lambda params=None: {})

    snapshot = read_futures_account(_Bridge(client))

    position = snapshot.positions[0]
    assert position.contracts == pytest.approx(250.0)
    assert position.quantity == pytest.approx(-0.25)


def test_read_futures_account_applies_inverse_contract_size_to_base_quantity():
    class _InverseExchange(_Exchange):
        def market(self, symbol):
            return {
                "quote": "USD", "settle": "BTC", "inverse": True,
                "contractSize": 100.0,
            }

    exchange = _InverseExchange(
        {},
        [{
            "symbol": "BTC/USD:BTC", "side": "long", "contracts": "10",
            "entryPrice": "50000", "markPrice": "40000",
        }],
    )
    bridge = _Bridge(exchange)
    bridge.exchange_id = "bybit"

    snapshot = read_futures_account(bridge)

    assert snapshot.positions[0].quantity == pytest.approx(0.02)
    assert snapshot.positions[0].notional_currency == "BTC"


def test_read_okx_futures_account_selects_swap_positions_and_reads_nested_equity():
    exchange = _Exchange(
        {"info": {"data": [{"totalEq": "1234.5", "adjEq": "1200.0"}]}},
        [],
    )
    bridge = _Bridge(exchange)
    bridge.exchange_id = "okx"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity == pytest.approx(1234.5)
    assert snapshot.wallet_balance is None
    assert snapshot.available_balance is None
    assert snapshot.equity_currency == "USD"
    assert snapshot.unrealized_pnl_currency is None
    assert snapshot.wallet_balance_currency is None
    assert snapshot.account_scope == "trading"
    assert exchange.calls == [
        ("balance", {"type": "trading"}),
        ("positions", None, {"instType": "SWAP"}),
    ]



def test_read_bybit_futures_account_selects_linear_swap_and_reads_nested_totals():
    exchange = _Exchange(
        {"info": {"result": {"list": [{
            "accountType": "UNIFIED",
            "totalEquity": "1000.0",
            "totalMarginBalance": "990.0",
            "totalWalletBalance": "995.0",
            "totalAvailableBalance": "900.0",
            "totalPerpUPL": "-5.0",
        }]}}},
        [],
    )
    bridge = _Bridge(exchange)
    bridge.exchange_id = "bybit"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity == pytest.approx(1000.0)
    assert snapshot.wallet_balance == pytest.approx(995.0)
    assert snapshot.available_balance == pytest.approx(900.0)
    assert snapshot.unrealized_pnl == pytest.approx(-5.0)
    assert snapshot.wallet_balance_currency == "USD"
    assert snapshot.equity_currency == "USD"
    assert snapshot.available_balance_currency == "USD"
    assert snapshot.unrealized_pnl_currency == "USD"
    assert snapshot.account_scope == "unified"
    params = {"type": "swap", "subType": "linear"}
    position_params = {**params, "settleCoin": "USDT"}
    assert exchange.calls == [
        ("balance", params),
        ("positions", None, position_params),
    ]


def test_read_bybit_futures_account_does_not_label_missing_unified_totals():
    bridge = _Bridge(_Exchange({"info": {"result": {"list": [{"accountType": "UNIFIED"}]}}}, []))
    bridge.exchange_id = "bybit"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity is None
    assert snapshot.wallet_balance is None
    assert snapshot.equity_currency == "USD"
    assert snapshot.wallet_balance_currency == "USD"
    assert snapshot.account_scope == "unified"


def test_read_bybit_contract_account_uses_only_single_reported_coin():
    bridge = _Bridge(_Exchange({"info": {"result": {"list": [{
        "accountType": "CONTRACT",
        "coin": [{
            "coin": "USDC",
            "equity": "140.0",
            "walletBalance": "135.0",
            "availableToWithdraw": "120.0",
            "unrealisedPnl": "5.0",
        }],
    }]}}}, []))
    bridge.exchange_id = "bybit"
    bridge.config = {"exec_quote_currency": "USDT"}

    snapshot = read_futures_account(bridge)

    assert snapshot.equity == pytest.approx(140.0)
    assert snapshot.wallet_balance == pytest.approx(135.0)
    assert snapshot.available_balance == pytest.approx(120.0)
    assert snapshot.unrealized_pnl == pytest.approx(5.0)
    assert snapshot.equity_currency == "USDC"
    assert snapshot.wallet_balance_currency == "USDC"
    assert snapshot.available_balance_currency == "USDC"
    assert snapshot.unrealized_pnl_currency == "USDC"
    assert snapshot.account_scope == "derivatives"


def test_read_bybit_contract_account_does_not_invent_values_for_empty_totals():
    bridge = _Bridge(_Exchange({"info": {"result": {"list": [{
        "accountType": "CONTRACT",
        "coin": [],
    }]}}}, []))
    bridge.exchange_id = "bybit"
    bridge.config = {"exec_quote_currency": "USDT"}

    snapshot = read_futures_account(bridge)

    assert snapshot.equity is None
    assert snapshot.wallet_balance is None
    assert snapshot.available_balance is None
    assert snapshot.unrealized_pnl is None
    assert snapshot.equity_currency is None
    assert snapshot.wallet_balance_currency is None
    assert snapshot.available_balance_currency is None
    assert snapshot.unrealized_pnl_currency is None
    assert snapshot.account_scope == "derivatives"


def test_read_bybit_contract_account_does_not_combine_mixed_coin_totals():
    bridge = _Bridge(_Exchange({"info": {"result": {"list": [{
        "accountType": "CONTRACT",
        "coin": [
            {"coin": "USDT", "walletBalance": "100.0"},
            {"coin": "USDC", "walletBalance": "25.0"},
        ],
    }]}}}, []))
    bridge.exchange_id = "bybit"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity is None
    assert snapshot.wallet_balance is None
    assert snapshot.available_balance is None
    assert snapshot.unrealized_pnl is None
    assert snapshot.wallet_balance_currency is None
    assert snapshot.account_scope == "derivatives"


def test_read_bybit_account_without_account_type_does_not_assume_unified():
    bridge = _Bridge(_Exchange({"info": {"result": {"list": [{
        "totalEquity": "1000.0",
        "totalWalletBalance": "995.0",
    }]}}}, []))
    bridge.exchange_id = "bybit"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity is None
    assert snapshot.wallet_balance is None
    assert snapshot.equity_currency is None
    assert snapshot.wallet_balance_currency is None
    assert snapshot.account_scope == "derivatives"


def test_read_okx_futures_account_keeps_detail_balances_currency_specific():
    exchange = _Exchange(
        {"info": {"data": [{
            "totalEq": "1300.0",
            "upl": "12.0",
            "details": [
                {"ccy": "BTC", "cashBal": "0.01", "availEq": "0.008"},
                {"ccy": "USDT", "cashBal": "500.0", "availEq": "450.0"},
            ],
        }]}},
        [{
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": "2",
            "entryPrice": "100000",
            "markPrice": "99000",
            "notional": "198000",
            "initialMargin": "1000",
            "unrealizedPnl": "20",
            "info": {"ccy": "USDT", "notionalUsd": "198000"},
        }],
    )
    bridge = _Bridge(exchange)
    bridge.exchange_id = "okx"

    snapshot = read_futures_account(bridge)

    assert snapshot.equity_currency == "USD"
    assert snapshot.unrealized_pnl_currency == "USD"
    assert snapshot.wallet_balance_currency is None
    assert snapshot.available_balance_currency is None
    position = snapshot.positions[0]
    assert position.price_currency == "USDT"
    assert position.notional_currency == "USD"
    assert position.margin_currency == "USDT"
    assert position.unrealized_pnl_currency == "USDT"



def test_read_futures_account_rejects_unconfigured_exchange_routing():
    bridge = _Bridge(_Exchange({}, []))
    bridge.exchange_id = "gateio"

    with pytest.raises(ValueError, match="not configured"):
        read_futures_account(bridge)



def test_read_futures_account_does_not_treat_per_currency_balances_as_account_totals(monkeypatch):
    client, _ = _client(monkeypatch, [])
    monkeypatch.setattr(client, "fetch_balance", lambda params=None: {
        "total": {"BTC": 0.25, "USDT": 800.0},
        "free": {"USDT": 700.0},
        "used": {"USDT": 100.0},
    })

    snapshot = read_futures_account(_Bridge(client))

    assert snapshot.wallet_balance is None
    assert snapshot.equity is None
    assert snapshot.available_balance is None
    assert snapshot.unrealized_pnl is None


def test_read_futures_account_infers_direction_from_signed_contracts(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [{"symbol": "NEARUSDT", "positionSide": "BOTH", "positionAmt": "-5"}],
        markets=[_market("NEARUSDT", "NEAR/USDT:USDT")],
    )
    monkeypatch.setattr(client, "fetch_balance", lambda params=None: {})

    snapshot = read_futures_account(_Bridge(client))

    assert snapshot.positions[0].side == "short"
    assert snapshot.positions[0].quantity == pytest.approx(-5.0)


def test_read_futures_account_rejects_non_finite_numbers(monkeypatch):
    client, _ = _client(
        monkeypatch,
        [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "Infinity"}],
    )
    monkeypatch.setattr(
        client, "fetch_balance",
        lambda params=None: {"info": {"totalWalletBalance": "NaN"}},
    )

    with pytest.raises(ValueError, match="malformed futures position rows"):
        read_futures_account(_Bridge(client))


@pytest.mark.parametrize("changes,expected_unknown", [
    ({"positionSide": "LONG", "positionAmt": "-1"}, {"BTC/USDT:USDT"}),
    ({"positionSide": "SHORT", "positionAmt": "1"}, {"BTC/USDT:USDT"}),
    ({"positionAmt": True}, {"BTCUSDT"}),
    ({"positionAmt": "1e-400"}, {"BTCUSDT"}),
    ({"positionAmt": "1_0"}, {"BTCUSDT"}),
])
def test_invalid_or_conflicting_position_is_unknown(monkeypatch, changes, expected_unknown):
    client, _ = _client(
        monkeypatch, [_row(**changes), _row(symbol="ETHUSDT", positionAmt="2")],
        markets=[_market(), _market("ETHUSDT", "ETH/USDT:USDT")],
    )
    monkeypatch.setattr(client, "fetch_balance", lambda params=None: {"info": {}})
    observation = observe_futures_positions(_Bridge(client))
    assert observation.unknown_symbols == expected_unknown
    assert [(position.symbol, position.contracts) for position in observation.positions] == [("ETH/USDT:USDT", 2.0)]
    with pytest.raises(ValueError, match="malformed futures position rows"):
        read_futures_account(_Bridge(client))


def test_ambiguous_market_identity_fails_scope(monkeypatch):
    client, _ = _client(monkeypatch, [_row()])
    client.markets_by_id["BTCUSDT"].append(dict(client.markets_by_id["BTCUSDT"][0]))

    observation = observe_futures_positions(_Bridge(client))

    assert observation.unknown_symbols == {"BTCUSDT"}
    assert observation.unknown_scope is False
    assert observation.positions == ()
    assert observation.scope_complete is False


def test_clean_binance_read_claims_complete_scope(monkeypatch):
    client, _ = _client(monkeypatch, [])

    observation = observe_futures_positions(_Bridge(client))

    assert observation.scope_complete is True


def test_read_futures_account_reports_production_profile(monkeypatch):
    client, _ = _client(monkeypatch, [], profile="production")
    monkeypatch.setattr(client, "fetch_balance", lambda params=None: {})
    bridge = _Bridge(client)
    bridge.config = {"exec_base_url": "https://api.binance.com"}

    snapshot = read_futures_account(bridge)

    assert snapshot.venue_profile == "production"
