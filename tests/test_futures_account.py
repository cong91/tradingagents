import pytest

from tradingagents.execution.futures_account import read_futures_account


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


def test_read_futures_account_normalizes_positions_and_account_values():
    exchange = _Exchange(
        {
            "info": {
                "totalWalletBalance": "1000.0",
                "totalUnrealizedProfit": "25.0",
                "totalMarginBalance": "1025.0",
                "availableBalance": "900.0",
            }
        },
        [
            {
                "symbol": "INJ/USDT:USDT",
                "side": "short",
                "contracts": "12",
                "entryPrice": "25.0",
                "markPrice": "24.0",
                "liquidationPrice": "30.0",
                "unrealizedPnl": "12.0",
                "notional": "288.0",
                "initialMargin": "96.0",
                "leverage": "3",
                "marginMode": "isolated",
            },
            {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0},
        ],
    )

    snapshot = read_futures_account(_Bridge(exchange))

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
    assert exchange.calls == [
        ("balance", {"type": "future", "subType": "linear"}),
        ("positions", None, {"type": "future", "subType": "linear"}),
    ]


def test_read_futures_account_applies_linear_contract_size_to_quantity():
    class _ContractExchange(_Exchange):
        def market(self, symbol):
            return {
                "quote": "USDT", "settle": "USDT", "inverse": False,
                "contractSize": 0.001,
            }

    exchange = _ContractExchange(
        {},
        [{
            "symbol": "BTC/USDT:USDT", "side": "short", "contracts": "250",
            "entryPrice": "100000", "markPrice": "99000",
        }],
    )

    snapshot = read_futures_account(_Bridge(exchange))

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

    snapshot = read_futures_account(_Bridge(exchange))

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



def test_read_futures_account_does_not_treat_per_currency_balances_as_account_totals():
    exchange = _Exchange(
        {
            "total": {"BTC": 0.25, "USDT": 800.0},
            "free": {"USDT": 700.0},
            "used": {"USDT": 100.0},
        },
        [],
    )

    snapshot = read_futures_account(_Bridge(exchange))

    assert snapshot.wallet_balance is None
    assert snapshot.equity is None
    assert snapshot.available_balance is None
    assert snapshot.unrealized_pnl is None


def test_read_futures_account_infers_direction_from_signed_contracts():
    exchange = _Exchange({}, [{"symbol": "NEAR/USDT:USDT", "contracts": "-5"}])

    snapshot = read_futures_account(_Bridge(exchange))

    assert snapshot.positions[0].side == "short"
    assert snapshot.positions[0].quantity == pytest.approx(-5.0)


def test_read_futures_account_rejects_non_finite_numbers():
    exchange = _Exchange(
        {"info": {"totalWalletBalance": "NaN"}},
        [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": "Infinity"}],
    )

    snapshot = read_futures_account(_Bridge(exchange))

    assert snapshot.wallet_balance is None
    assert snapshot.positions == ()


def test_read_futures_account_reports_production_profile():
    exchange = _Exchange({}, [])
    bridge = _Bridge(exchange)
    bridge.config = {"exec_base_url": "https://api.binance.com"}

    snapshot = read_futures_account(bridge)

    assert snapshot.venue_profile == "production"
