"""Offline quality checks for portfolio futures snapshots."""

import json
from datetime import UTC, datetime

import ccxt
import pytest

import server.futures_snapshot as futures_snapshot
from server.futures_snapshot import _position_side, _position_size, read_futures_snapshot
from tradingagents.execution.futures_account import read_futures_account

pytestmark = pytest.mark.unit


class _Exchange:
    def __init__(self, balance=None, positions=None, market=None):
        self.balance = {"info": {"totalMarginBalance": "100"}} if balance is None else balance
        self.positions = [] if positions is None else positions
        self.market_data = (
            {"quote": "USDT", "settle": "USDT", "inverse": False, "contractSize": 0.01}
            if market is None else market
        )
        self.calls = []

    def fetch_balance(self, params=None):
        self.calls.append(("balance", params))
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("positions", symbols, params))
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    def market(self, symbol):
        if isinstance(self.market_data, Exception):
            raise self.market_data
        return self.market_data


class _Bridge:
    def __init__(self, exchange, exchange_id="okx", config=None):
        self.exchange = exchange
        self.exchange_id = exchange_id
        self.config = config or {
            "exec_base_url": "https://demo-api.binance.com",
            "exec_quote_currency": "USDC",
        }
        self.client_calls = 0

    def client(self):
        self.client_calls += 1
        return self.exchange


def _assert_positions_read(snapshot, *, skipped, issues, observed=True):
    quality = snapshot["positions_read"]
    assert quality["status"] == "partial"
    assert quality["skipped"] == skipped
    assert quality["issues"] == issues
    if observed:
        timestamp = datetime.fromisoformat(quality["observed_at"])
        assert timestamp.tzinfo is not None and timestamp.utcoffset() == UTC.utcoffset(timestamp)
    else:
        assert quality["observed_at"] is None


@pytest.mark.parametrize(
    ("exchange_id", "balance", "balance_params", "position_params"),
    [
        (
            "bybit",
            {"info": {"result": {"list": [{
                "accountType": "UNIFIED", "totalEquity": "100", "totalWalletBalance": "90",
            }]}}},
            {"type": "swap", "subType": "linear"},
            {"type": "swap", "subType": "linear", "settleCoin": "USDC"},
        ),
        (
            "okx",
            {"info": {"data": [{"totalEq": "100", "details": [{"ccy": "USDT", "cashBal": "90"}]}]}},
            {"type": "trading"},
            {"instType": "SWAP"},
        ),
    ],
)
def test_snapshot_reuses_account_normalization_and_fetches_once(
    exchange_id, balance, balance_params, position_params,
):
    rows = [{
        "symbol": "BTC/USDT:USDT", "side": "short", "contracts": "2",
        "entryPrice": "100", "markPrice": "90", "unrealizedPnl": "20",
        "notional": "-180", "initialMargin": "60", "leverage": "3",
        "info": {"marginAsset": "USDT"},
    }]
    exchange = _Exchange(balance, rows)
    bridge = _Bridge(exchange, exchange_id)
    expected = read_futures_account(_Bridge(_Exchange(balance, rows), exchange_id)).as_dict()

    snapshot = read_futures_snapshot(bridge)

    assert {key: value for key, value in snapshot.items() if key != "positions_read"} == expected
    _assert_positions_read(
        snapshot, skipped=0, issues=[{"symbol": None, "reason": "scope_unverified"}],
    )
    assert bridge.client_calls == 1
    assert exchange.calls == [("balance", balance_params), ("positions", None, position_params)]


@pytest.mark.parametrize("rows", [[], [{"symbol": "BTC/USDT:USDT", "contracts": 0}]])
def test_empty_and_flat_rows_are_never_reported_as_complete(rows):
    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=rows)))

    assert snapshot["positions"] == []
    _assert_positions_read(
        snapshot, skipped=0, issues=[{"symbol": None, "reason": "scope_unverified"}],
    )


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (None, "invalid_row"),
        ("secret-row", "invalid_row"),
        ({"symbol": "BTC/USDT:USDT"}, "missing_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": None}, "missing_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": ""}, "missing_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": "NaN"}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": float("inf")}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": True}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": False}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 0, "size": "bad"}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 0, "size": 2}, "inconsistent_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "info": {"positionAmt": "-3"}}, "inconsistent_size"),
        ({"symbol": None, "side": "long", "contracts": 2}, "missing_symbol"),
        ({"symbol": "  ", "contracts": 0}, "missing_symbol"),
        ({"symbol": ["BTC"], "contracts": 2}, "missing_symbol"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2}, "missing_side"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "side": "private-secret"}, "invalid_side"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "side": "long", "positionSide": "SHORT"}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "side": "long", "positionAmt": -2}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "side": "short", "positionAmt": 2, "positionSide": "BOTH"}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "side": "long", "positionAmt": "1e-400"}, "invalid_size"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "positionAmt": 2, "position_amount": -2}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "size": -2, "side": "long"}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 2, "side": "long", "hedged": "yes"}, "invalid_side"),
        ({"symbol": "BTC/USDT:USDT", "contracts": 0, "side": "long", "positionSide": "SHORT"}, "inconsistent_side"),
        ({"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2, "info": "bad"}, "invalid_row"),
    ],
)
def test_invalid_rows_are_partial_and_diagnostics_do_not_expose_raw_data(row, reason):
    exchange = _Exchange(
        balance={"info": {"data": [{"totalEq": "100"}]}},
        positions=[row],
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["equity"] == 100
    assert snapshot["status"] == "ok"
    assert snapshot["positions"] == []
    _assert_positions_read(
        snapshot,
        skipped=1,
        issues=[
            {"symbol": None, "reason": "scope_unverified"},
            {"symbol": None, "reason": reason},
        ],
    )
    assert "secret" not in json.dumps(snapshot["positions_read"])


def test_partial_read_keeps_valid_positions_and_does_not_count_flat_rows_as_skipped():
    rows = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2},
        {"symbol": "ETH/USDT:USDT", "contracts": 0},
        {"symbol": "secret", "contracts": None},
        {"symbol": "SOL/USDT:USDT", "side": "short", "contracts": 3},
    ]
    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=rows)))

    assert [item["symbol"] for item in snapshot["positions"]] == [
        "BTC/USDT:USDT", "SOL/USDT:USDT",
    ]
    _assert_positions_read(
        snapshot,
        skipped=1,
        issues=[
            {"symbol": None, "reason": "scope_unverified"},
            {"symbol": None, "reason": "missing_size"},
        ],
    )


@pytest.mark.parametrize(
    ("row", "side"),
    [
        ({"contracts": -2}, "short"),
        ({"positionAmt": 2}, "long"),
        ({"positionAmt": -2}, "short"),
        ({"contracts": 2, "positionAmt": 2}, "long"),
        ({"contracts": 2, "side": "short", "info": {"positionSide": "BOTH", "positionAmt": "-2"}}, "short"),
        ({"contracts": 2, "side": "short", "info": {"positionSide": "SHORT", "positionAmt": "-2"}}, "short"),
        ({"contracts": 2, "side": "short", "hedged": True, "info": {"posSide": "short", "pos": "2"}}, "short"),
        ({"contracts": 2, "info": {"positionIdx": 2, "side": "Sell", "size": "2"}}, "short"),
        ({"contracts": 2, "info": {"positionSide": "BOTH", "positionAmt": "2"}}, "long"),
    ],
)
def test_position_direction_distinguishes_signed_one_way_and_hedge_sizes(row, side):
    row = {"symbol": "BTC/USDT:USDT", **row}
    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=[row])))

    (position,) = snapshot["positions"]
    assert snapshot["positions_read"]["status"] == "partial"
    assert snapshot["positions_read"]["skipped"] == 0
    assert position["side"] == side
    assert position["contracts"] == 2
    assert position["quantity"] == pytest.approx(0.02 if side == "long" else -0.02)


@pytest.mark.parametrize(
    "row",
    [
        {"contracts": 2, "side": "short", "hedged": True,
         "info": {"positionSide": "SHORT", "positionAmt": "2"}},
        {"contracts": 2, "side": "short", "positionSide": "SHORT", "positionAmt": "2"},
    ],
)
def test_binance_positive_signed_amount_conflicts_with_hedge_short(row):
    raw = {"symbol": "BTC/USDT:USDT", **row}
    info = raw.get("info") or {}
    size, directional, reason = _position_size(raw, info)
    assert reason is None
    _, reason = _position_side(raw, info, size, directional, "binance")
    assert reason == "inconsistent_side"


@pytest.mark.parametrize(
    ("exchange_id", "row"),
    [
        ("bybit", {"side": "short", "contracts": 2, "hedged": True,
                   "info": {"positionIdx": 2, "side": "Sell", "size": "2"}}),
        ("okx", {"side": "short", "contracts": 2, "hedged": True,
                 "info": {"posSide": "short", "pos": "2"}}),
    ],
)
def test_non_binance_unsigned_hedge_sizes_remain_valid(exchange_id, row):
    exchange = _Exchange(positions=[{"symbol": "BTC/USDT:USDT", **row}])

    snapshot = read_futures_snapshot(_Bridge(exchange, exchange_id))

    _assert_positions_read(
        snapshot, skipped=0, issues=[{"symbol": None, "reason": "scope_unverified"}],
    )
    assert snapshot["positions"][0]["side"] == "short"
    assert snapshot["positions"][0]["quantity"] == pytest.approx(-0.02)


@pytest.mark.parametrize(
    "notional",
    [
        {"notional": "25"},
        {"notional": "-25"},
        {"info": {"notional": "25"}},
        {"info": {"notionalUsd": "25"}},
        {"info": {"notionalValue": "25"}},
        {"info": {"positionValue": "25"}},
        {"notional": "0", "info": {"notional": "25"}},
    ],
)
def test_flat_size_with_nonzero_notional_is_partial(notional):
    row = {"symbol": "BTC/USDT:USDT", "contracts": 0, **notional}

    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=[row])))

    assert snapshot["positions"] == []
    _assert_positions_read(
        snapshot,
        skipped=1,
        issues=[
            {"symbol": None, "reason": "scope_unverified"},
            {"symbol": None, "reason": "inconsistent_notional"},
        ],
    )


@pytest.mark.parametrize("value", [None, "", 0, "-0.0000"])
def test_flat_size_with_absent_or_zero_notional_is_not_skipped(value):
    row = {"symbol": "BTC/USDT:USDT", "contracts": 0, "notional": value}

    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=[row])))

    _assert_positions_read(
        snapshot, skipped=0, issues=[{"symbol": None, "reason": "scope_unverified"}],
    )


@pytest.mark.parametrize("value", [True, "bad", "NaN", "1e-400"])
def test_flat_size_with_malformed_notional_is_partial(value):
    row = {"symbol": "BTC/USDT:USDT", "contracts": 0, "notional": value}

    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=[row])))

    _assert_positions_read(
        snapshot,
        skipped=1,
        issues=[
            {"symbol": None, "reason": "scope_unverified"},
            {"symbol": None, "reason": "invalid_notional"},
        ],
    )


def test_hedge_long_and_short_remain_separate():
    rows = [
        {"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2, "hedged": True},
        {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2, "hedged": True},
    ]
    snapshot = read_futures_snapshot(_Bridge(_Exchange(positions=rows)))

    assert [position["side"] for position in snapshot["positions"]] == ["long", "short"]
    assert snapshot["positions_read"]["status"] == "partial"


@pytest.mark.parametrize(
    "market",
    [
        {},
        {"inverse": False},
        {"inverse": None, "contractSize": 1},
        {"inverse": False, "contractSize": 0},
        {"inverse": False, "contractSize": -1},
        {"inverse": False, "contractSize": True},
        {"inverse": False, "contractSize": "NaN"},
        {"inverse": False, "linear": False, "contractSize": 1},
        RuntimeError("private market detail"),
    ],
)
def test_unknown_or_invalid_market_metadata_never_invents_quantity(market):
    exchange = _Exchange(positions=[{
        "symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2,
    }], market=market)

    snapshot = read_futures_snapshot(_Bridge(exchange))

    (position,) = snapshot["positions"]
    assert position["contracts"] == 2
    assert position["quantity"] is None
    assert snapshot["positions_read"]["status"] == "partial"
    assert snapshot["status"] == "ok"


def test_reported_contract_size_can_supply_missing_market_size():
    exchange = _Exchange(
        positions=[{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2, "contractSize": 0.1,
        }],
        market={"inverse": False, "quote": "USDT", "settle": "USDT"},
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["positions"][0]["quantity"] == pytest.approx(0.2)


def test_conflicting_market_and_position_contract_sizes_leave_quantity_unknown():
    exchange = _Exchange(positions=[{
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2, "contractSize": 0.1,
    }])

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["positions"][0]["quantity"] is None


def test_inverse_quantity_requires_a_positive_entry_price():
    exchange = _Exchange(
        positions=[
            {"symbol": "BTC/USD:BTC", "side": "long", "contracts": 10, "entryPrice": 50000},
            {"symbol": "ETH/USD:ETH", "side": "short", "contracts": 10, "entryPrice": -1},
        ],
        market={"inverse": True, "contractSize": 100, "quote": "USD", "settle": "BTC"},
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["positions"][0]["quantity"] == pytest.approx(0.02)
    assert snapshot["positions"][1]["quantity"] is None


def test_invalid_optional_numeric_values_remain_unknown():
    exchange = _Exchange(
        balance={"info": {"data": [{"totalEq": "100", "details": [
            {"ccy": "USDT", "cashBal": True},
        ]}]}},
        positions=[{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
            "entryPrice": True, "markPrice": "NaN", "leverage": False,
            "info": {"initialMargin": True},
        }],
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["equity"] == 100
    assert snapshot["wallet_balance"] is None
    position = snapshot["positions"][0]
    assert all(position[key] is None for key in ("entry_price", "mark_price", "leverage", "margin"))
    assert snapshot["positions_read"]["status"] == "partial"


def test_non_list_positions_preserve_account_but_are_not_a_complete_empty_read():
    exchange = _Exchange(balance={"info": {"data": [{"totalEq": "100"}]}}, positions={"private": "x"})

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["equity"] == 100
    assert snapshot["positions"] == []
    _assert_positions_read(
        snapshot, skipped=None, issues=[{"symbol": None, "reason": "invalid_response"}],
    )


def test_invalid_optional_metadata_is_not_reported_as_a_currency_or_margin_mode():
    exchange = _Exchange(
        positions=[{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1,
            "marginMode": {"private": "secret"}, "info": {"notionalUsd": "NaN"},
        }],
        market={"inverse": None, "contractSize": 1, "quote": "USDT", "settle": "USDT"},
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    position = snapshot["positions"][0]
    assert position["quantity"] is None
    assert position["notional_currency"] is None
    assert position["margin_mode"] is None
    assert "secret" not in json.dumps(snapshot)


def test_positions_read_failure_preserves_account_without_private_error_text():
    exchange = _Exchange(
        balance={"info": {"data": [{"totalEq": "100"}]}},
        positions=RuntimeError("private account secret"),
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["equity"] == 100
    assert snapshot["status"] == "ok"
    assert snapshot["positions"] == []
    _assert_positions_read(
        snapshot, skipped=None, issues=[{"symbol": None, "reason": "read_failed"}], observed=False,
    )
    assert "secret" not in json.dumps(snapshot)
    assert len(exchange.calls) == 2


def test_balance_read_failure_propagates_without_fetching_positions():
    exchange = _Exchange(balance=RuntimeError("balance unavailable"))

    with pytest.raises(RuntimeError, match="balance unavailable"):
        read_futures_snapshot(_Bridge(exchange))

    assert len(exchange.calls) == 1


def test_unknown_market_and_info_currency_types_are_not_exposed():
    exchange = _Exchange(
        balance={"info": {"totalMarginBalance": "100", "asset": {"private": "secret"}}},
        positions=[{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1,
            "info": {"marginAsset": {"private": "secret"}},
        }],
        market={"inverse": False, "contractSize": 1, "quote": [], "settle": {"private": "secret"}},
    )

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert snapshot["equity_currency"] is None
    position = snapshot["positions"][0]
    assert position["quantity"] == 1
    assert position["price_currency"] is None
    assert position["margin_currency"] is None
    assert "secret" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    ("exchange_id", "balance"),
    [
        ("okx", {"info": {"data": [{"totalEq": "100", "details": [
            {"ccy": ["private"], "cashBal": "10"},
        ]}]}}),
        ("bybit", {"total": {"USDT": 0}, "info": {"result": {"list": [{
            "accountType": ["private"], "totalEquity": "100",
        }]}}}),
    ],
)
def test_malformed_account_currency_metadata_does_not_break_snapshot(exchange_id, balance):
    snapshot = read_futures_snapshot(_Bridge(_Exchange(balance=balance), exchange_id))

    assert snapshot["status"] == "ok"
    assert all(
        snapshot[key] is None or isinstance(snapshot[key], str)
        for key in ("equity_currency", "wallet_balance_currency", "available_balance_currency")
    )
    assert "private" not in json.dumps(snapshot)


def _binance_raw_bridge(exchange):
    exchange.urls = {"api": {
        "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
        "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
        "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
    }}
    exchange.options = {"enableDemoTrading": True, "sandboxMode": False}
    exchange.fapiPrivateV3GetPositionRisk = lambda params: pytest.fail("unexpected V3 request")
    return _Bridge(exchange, "binance")


@pytest.mark.parametrize("balance", [
    {"info": [{"asset": "USDT", "umWalletBalance": "31", "umUnrealizedPNL": "0"}],
     "free": {"USDT": 31}, "used": {"USDT": 0}, "total": {"USDT": 31}},
    {"info": [{"asset": "USDT", "umWalletBalance": "31"}]},
    {"info": [{"asset": "USDT", "umWalletBalance": "31"}],
     "totalMarginBalance": "999", "totalWalletBalance": "999"},
])
def test_binance_portfolio_margin_balance_has_unverified_positions(balance):
    exchange = _Exchange(balance=balance, positions=[{
        "symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2,
        "hedged": True, "info": {"positionSide": "SHORT", "positionAmt": "-2"},
    }])

    snapshot = read_futures_snapshot(_binance_raw_bridge(exchange))

    assert snapshot["status"] == "ok"
    assert snapshot["account_scope"] == "derivatives"
    assert all(snapshot[key] is None for key in (
        "equity", "wallet_balance", "available_balance", "unrealized_pnl",
        "equity_currency", "wallet_balance_currency", "available_balance_currency",
        "unrealized_pnl_currency",
    ))
    _assert_positions_read(
        snapshot, skipped=None, issues=[{"symbol": None, "reason": "account_type_unverified"}],
        observed=False,
    )
    assert exchange.calls == [("balance", {"type": "future", "subType": "linear"})]


@pytest.mark.parametrize("rows", [
    [],
    ["private"],
    [{"asset": "USDT"}],
    [{"asset": "USDT", "umWalletBalance": "NaN"}],
    [{"asset": "USDT", "umWalletBalance": True}],
    [{"asset": "USDT", "umWalletBalance": "1"}, "private"],
    [{"asset": "", "umWalletBalance": "1"}],
])
def test_malformed_binance_portfolio_margin_list_is_rejected(rows):
    exchange = _Exchange(balance={"info": rows, "free": {}, "used": {}, "total": {}})

    with pytest.raises(ValueError, match="invalid futures account response"):
        read_futures_snapshot(_binance_raw_bridge(exchange))

    assert len(exchange.calls) == 1


@pytest.mark.parametrize("exchange_id", ["bybit", "okx"])
@pytest.mark.parametrize("rows", [
    [],
    [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2}],
    [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
      "info": {"nextPageCursor": "private-next-cursor"}}],
    [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2,
      "nextPageCursor": "private-next-cursor"}],
    [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0,
      "info": {"nextPageCursor": "private-next-cursor"}}],
    [{"symbol": "private-secret", "contracts": None,
      "info": {"nextPageCursor": "private-next-cursor"}}],
])
def test_normalized_position_scope_is_unverified_for_all_venues(exchange_id, rows):
    balance = {
        "binance": {"info": {"totalMarginBalance": "100"}},
        "bybit": {"info": {"result": {"list": [{
            "accountType": "UNIFIED", "totalEquity": "100",
        }]}}},
        "okx": {"info": {"data": [{"totalEq": "100"}]}},
    }[exchange_id]
    exchange = _Exchange(balance=balance, positions=rows)
    snapshot = read_futures_snapshot(_Bridge(exchange, exchange_id))

    issues = [{"symbol": None, "reason": "scope_unverified"}]
    if rows and rows[0].get("contracts") is None:
        issues.append({"symbol": None, "reason": "missing_size"})
    _assert_positions_read(snapshot, skipped=len(issues) - 1, issues=issues)
    assert snapshot["account_scope"] == {
        "binance": "derivatives", "bybit": "unified", "okx": "trading",
    }[exchange_id]
    assert "private-next-cursor" not in json.dumps(snapshot)
    assert [call[0] for call in exchange.calls] == ["balance", "positions"]
    if rows and rows[0].get("contracts") == 2:
        assert snapshot["positions"][0]["quantity"] == pytest.approx(0.02)
    else:
        assert snapshot["positions"] == []


def test_real_ccxt_binance_can_filter_open_position_before_snapshot(monkeypatch):
    exchange = ccxt.binance()
    raw_positions = [{
        "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1",
        "entryPrice": "0", "notional": "100",
    }]
    calls = []
    monkeypatch.setattr(exchange, "markets", {})
    monkeypatch.setattr(exchange, "load_leverage_brackets", lambda *args: None)
    monkeypatch.setattr(
        exchange, "fapiPrivateV3GetPositionRisk",
        lambda params: calls.append(params) or raw_positions,
    )
    normalized = exchange.fetch_positions(None, {"type": "future", "subType": "linear"})
    assert normalized == []
    assert len(calls) == 1


def test_positions_observed_at_is_recorded_before_normalization(monkeypatch):
    events = []
    observed = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    class Clock:
        @classmethod
        def now(cls, tz):
            events.append("clock")
            assert tz is UTC
            return observed

    class Exchange(_Exchange):
        def fetch_positions(self, symbols=None, params=None):
            events.append("positions")
            return super().fetch_positions(symbols, params)

        def market(self, symbol):
            events.append("market")
            return super().market(symbol)

    monkeypatch.setattr(futures_snapshot, "datetime", Clock)
    exchange = Exchange(positions=[{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 2}])

    snapshot = read_futures_snapshot(_Bridge(exchange))

    assert events == ["positions", "clock", "market"]
    assert snapshot["positions_read"]["observed_at"] == observed.isoformat()


def test_unsupported_exchange_fails_before_reading_the_venue():
    exchange = _Exchange()

    with pytest.raises(ValueError, match="not configured"):
        read_futures_snapshot(_Bridge(exchange, "unsupported"))

    assert exchange.calls == []
