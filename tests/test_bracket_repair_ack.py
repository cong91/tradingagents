"""Repair acknowledgements require independently verified venue state."""

from copy import deepcopy

import ccxt
import pytest

from server import job_store
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema):
    return postgres_schema


_DEMO_MARKET = {
    "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC",
    "quote": "USDT", "settle": "USDT", "settleId": "USDT", "type": "swap",
    "spot": False, "swap": True, "future": False, "contract": True,
    "linear": True, "inverse": False,
}


class _Exchange:
    def __init__(
        self, *, repair_status="NEW", fail_verification=False, lose_ack=False,
        expected_leg_index=0,
    ):
        self.repair_status = repair_status
        self.fail_verification = fail_verification
        self.lose_ack = lose_ack
        self.expected_leg_index = expected_leg_index
        self.calls = []
        self.orders = {}
        # The position observer reads the raw V3 endpoint through a
        # profile-validated client; the fake must carry the same surface.
        self.urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        self.options = {"enableDemoTrading": True, "sandboxMode": False}
        self.verbose = False
        self.markets_by_id = {"BTCUSDT": [_DEMO_MARKET]}

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.calls.append(("risk", params))
        return [{
            "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.005",
            "entryPrice": "50000", "markPrice": "50000", "notional": "250",
            "marginAsset": "USDT",
        }]

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("positions", symbols, params))
        return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.005}]

    def market(self, symbol):
        return {"quote": "USDT", "settle": "USDT", "inverse": False}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", symbol, side, amount, params))
        persisted = job_store.load_bracket_recovery("bracket:BTC/USDT:USDT:repair-test")
        assert persisted["status"] == "submission_uncertain"
        assert persisted["legs"][self.expected_leg_index]["status"] == "uncertain"
        assert persisted["legs"][self.expected_leg_index]["repair_client_id"] == params["clientOrderId"]
        assert persisted["legs"][self.expected_leg_index]["repair_quantity"] == amount
        order_id = str(len(self.orders) + 1)
        trigger_key = "stopLossPrice" if "stopLossPrice" in params else "takeProfitPrice"
        order_type = "STOP_MARKET" if trigger_key == "stopLossPrice" else "TAKE_PROFIT_MARKET"
        self.orders[order_id] = {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "type": "market",
            "status": "open" if self.repair_status == "NEW" else "expired",
            "info": {
                "algoId": order_id,
                "symbol": "BTCUSDT",
                "side": side.upper(),
                "algoStatus": self.repair_status,
                "orderType": order_type,
                "triggerPrice": str(params[trigger_key]),
                "quantity": str(amount),
                "reduceOnly": True,
                "workingType": params.get("workingType"),
                "clientAlgoId": params.get("clientOrderId"),
            },
        }
        if self.lose_ack:
            raise ccxt.NetworkError("repair ACK lost")
        return {"id": order_id}

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("fetch_order", order_id, symbol, params))
        if self.fail_verification:
            self.fail_verification = False
            raise OSError("verification read unavailable")
        return deepcopy(self.orders[order_id])

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("open_orders", symbol, params))
        return [
            deepcopy(order) for order in self.orders.values()
            if order["info"]["algoStatus"] == "NEW"
        ]


class _Bridge:
    exchange_id = "binance"

    def __init__(self, exchange):
        self.config = {
            "exec_base_url": "https://demo-api.binance.com",
            "exec_sandbox": True,
        }
        self.exchange = exchange

    def client(self):
        return self.exchange

    def _redact(self, message):
        return message


def _pending_task(exchange, *, legs=None):
    bridge = _Bridge(exchange)
    task_id = "bracket:BTC/USDT:USDT:repair-test"
    job_store.create_bracket_recovery(
        task_id,
        symbol="BTC/USDT:USDT",
        entry_order_id="repair-test",
        close_side="sell",
        quantity=0.005,
        legs=legs if legs is not None else [{
            "name": "stop-loss",
            "params": {"stopLossPrice": 48_000.0},
            "status": "pending",
            "order_id": None,
        }],
        now="2026-11-03T00:00:00+00:00",
        venue_key=venue_key(bridge),
    )
    return BracketManager(bridge), bridge, task_id


def _repair_with_lock(manager, exchange, task_id):
    task = job_store.load_bracket_recovery(task_id)
    with operation_lock(task["symbol"]):
        return manager._repair(exchange, task)


def test_repair_ack_with_expired_order_is_not_reported_monitoring():
    exchange = _Exchange(repair_status="EXPIRED")
    manager, _bridge, task_id = _pending_task(exchange)

    result = _repair_with_lock(manager, exchange, task_id)

    stored = job_store.load_bracket_recovery(task_id)
    assert result == "reconcile_incomplete"
    assert stored["status"] == "pending"
    assert stored["legs"][0]["status"] == "pending"
    assert stored["legs"][0].get("order_id") is None
    assert stored["legs"][0].get("repair_client_id") is None
    assert [call[0] for call in exchange.calls].count("create") == 1


def test_repair_ack_with_mismatched_read_stays_uncertain_without_emergency_close():
    exchange = _Exchange(repair_status="NEW")
    manager, _bridge, task_id = _pending_task(exchange)
    original_fetch = exchange.fetch_order

    def mismatched_fetch(order_id, symbol, params=None):
        result = original_fetch(order_id, symbol, params)
        result["info"]["quantity"] = "0.004"
        return result

    exchange.fetch_order = mismatched_fetch
    result = _repair_with_lock(manager, exchange, task_id)

    stored = job_store.load_bracket_recovery(task_id)
    assert result == "reconcile_incomplete"
    assert stored["status"] == "submission_uncertain"
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["repair_client_id"]
    assert not [
        call for call in exchange.calls
        if call[0] == "create" and call[4].get("clientOrderId", "").startswith("emergency-")
    ]
    assert [call[0] for call in exchange.calls].count("create") == 1


def test_unverified_repair_ack_is_reconciled_without_duplicate_submit():
    exchange = _Exchange(fail_verification=True)
    manager, _bridge, task_id = _pending_task(exchange)

    result = _repair_with_lock(manager, exchange, task_id)

    uncertain = job_store.load_bracket_recovery(task_id)
    assert result == "reconcile_incomplete"
    assert uncertain["status"] == "submission_uncertain"
    assert uncertain["legs"][0]["status"] == "uncertain"
    assert uncertain["legs"][0]["order_id"] == "1"
    assert uncertain["legs"][0]["repair_client_id"]
    assert [call[0] for call in exchange.calls].count("create") == 1

    assert BracketManager(_Bridge(exchange))._reconcile_for_verified_account(
        task_id, repair_missing=False, allow_sibling_cancel=False,
    ) == "monitoring"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["status"] == "monitoring"
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "1"
    assert stored["legs"][0].get("repair_client_id") is None
    assert [call[0] for call in exchange.calls].count("create") == 1


def test_interrupted_pending_task_inspects_existing_repair_before_retry():
    exchange = _Exchange(expected_leg_index=1)
    manager, _bridge, task_id = _pending_task(
        exchange,
        legs=[{
            "name": "stop-loss",
            "params": {"stopLossPrice": 48_000.0},
            "status": "uncertain",
            "order_id": None,
            "repair_client_id": "br-persisted-intent",
            "repair_quantity": 0.005,
        }, {
            "name": "take-profit",
            "params": {"takeProfitPrice": 55_000.0},
            "status": "pending",
            "order_id": None,
        }],
    )
    exchange.orders["existing-repair"] = {
        "id": "existing-repair", "symbol": "BTC/USDT:USDT", "side": "sell",
        "amount": 0.005, "type": "market", "status": "open",
        "info": {
            "algoId": "existing-repair", "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": "NEW", "orderType": "STOP_MARKET",
            "triggerPrice": "48000.0", "quantity": "0.005",
            "reduceOnly": True, "workingType": "MARK_PRICE",
            "clientAlgoId": "br-persisted-intent",
        },
    }

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "monitoring"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "existing-repair"
    assert stored["legs"][0].get("repair_client_id") is None
    assert [call[0] for call in exchange.calls].count("create") == 1


def test_unresolved_persisted_repair_intent_is_not_retried_without_venue_evidence():
    exchange = _Exchange()
    manager, _bridge, task_id = _pending_task(
        exchange,
        legs=[{
            "name": "stop-loss",
            "params": {"stopLossPrice": 48_000.0},
            "status": "uncertain",
            "order_id": None,
            "repair_client_id": "br-no-venue-evidence",
            "repair_quantity": 0.005,
        }],
    )
    task = job_store.load_bracket_recovery(task_id)
    job_store.update_bracket_recovery(
        task_id, status="submission_uncertain",
        placed_ids=[], attempts=0, last_error="repair intent persisted before submit",
        now="2026-11-03T00:00:00+00:00", legs=task["legs"],
    )

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["status"] == "submission_uncertain"
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["repair_client_id"] == "br-no-venue-evidence"
    assert not [call for call in exchange.calls if call[0] == "create"]


def test_lost_repair_ack_is_found_by_client_id_without_duplicate_submit():
    exchange = _Exchange(lose_ack=True)
    manager, bridge, task_id = _pending_task(exchange)

    assert _repair_with_lock(manager, exchange, task_id) == "reconcile_incomplete"
    uncertain = job_store.load_bracket_recovery(task_id)
    assert uncertain["status"] == "submission_uncertain"
    assert uncertain["legs"][0]["order_id"] is None
    assert uncertain["legs"][0]["repair_client_id"]

    reloaded = BracketManager(bridge)
    assert reloaded._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "monitoring"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "1"
    assert [call[0] for call in exchange.calls].count("create") == 1


class _RacingExchange(_Exchange):
    def __init__(self, raw_status, actual_qty, *, actual_order_id="execution-1"):
        super().__init__()
        self.raw_status = raw_status
        self.actual_qty = actual_qty
        self.actual_order_id = actual_order_id
        self.child_order = {
            "id": "execution-1", "symbol": "BTC/USDT:USDT", "side": "sell",
            "status": "closed", "amount": 0.005, "filled": 0.005,
            "info": {
                "orderId": "execution-1", "symbol": "BTCUSDT", "side": "SELL",
                "status": "FILLED", "origQty": "0.005", "executedQty": "0.005",
            },
        }
        self.orders = {
            "repair-stop": {
                "id": "repair-stop", "symbol": "BTC/USDT:USDT", "side": "sell",
                "amount": 0.005, "type": "market", "status": "open",
                "info": {
                    "algoId": "repair-stop", "symbol": "BTCUSDT", "side": "SELL",
                    "algoStatus": "NEW", "orderType": "STOP_MARKET",
                    "triggerPrice": "48000", "quantity": "0.005", "reduceOnly": True,
                    "workingType": "MARK_PRICE", "clientAlgoId": "br-repair-intent",
                },
            },
            "sibling": {
                "id": "sibling", "symbol": "BTC/USDT:USDT", "side": "sell",
                "amount": 0.005, "type": "market", "status": "open",
                "info": {
                    "algoId": "sibling", "symbol": "BTCUSDT", "side": "SELL",
                    "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                    "triggerPrice": "55000", "quantity": "0.005", "reduceOnly": True,
                    "workingType": "MARK_PRICE",
                },
            },
        }

    def fetch_order(self, order_id, symbol, params=None):
        if order_id == "execution-1":
            self.calls.append(("fetch_order", order_id, symbol, params))
            if isinstance(self.child_order, Exception):
                raise self.child_order
            return deepcopy(self.child_order)
        order = super().fetch_order(order_id, symbol, params)
        if order_id == "repair-stop":
            order["status"] = "closed"
            order["info"]["algoStatus"] = self.raw_status
            if self.actual_qty is not None:
                order["info"]["actualQty"] = self.actual_qty
            if self.actual_order_id is not None:
                order["info"]["actualOrderId"] = self.actual_order_id
        return order

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id, symbol, params))
        return {"id": order_id, "status": "canceled"}


def _pending_racing_repair(exchange, *, known_id):
    manager, bridge, task_id = _pending_task(exchange, legs=[{
        "name": "stop-loss", "params": {"stopLossPrice": 48_000.0},
        "status": "uncertain", "order_id": "repair-stop" if known_id else None,
        "repair_client_id": "br-repair-intent", "repair_quantity": 0.005,
    }, {
        "name": "take-profit", "params": {"takeProfitPrice": 55_000.0},
        "status": "placed", "order_id": "sibling",
    }])
    task = job_store.load_bracket_recovery(task_id)
    job_store.update_bracket_recovery(
        task_id, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error=None, now="2026-11-03T00:00:00+00:00", legs=task["legs"],
    )
    return manager, bridge, task_id


@pytest.mark.parametrize("raw_status", ["FILLED", "FINISHED"])
@pytest.mark.parametrize("known_id", [True, False])
def test_later_filled_repair_read_wins_over_stale_open_snapshot(raw_status, known_id):
    exchange = _RacingExchange(raw_status, "0.005")
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=known_id)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"

    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "filled"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][1]["status"] == "cancelled"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"),
        ("fetch_order", "repair-stop"),
        ("fetch_order", "execution-1"),
        ("cancel", "sibling"),
    ]
    assert exchange.calls[1][3] == {"trigger": True}
    assert not exchange.calls[2][3]


@pytest.mark.parametrize("raw_status", ["FILLED", "FINISHED"])
@pytest.mark.parametrize("actual_qty,actual_order_id", [
    (None, "execution-1"), ("0", "execution-1"), ("0.002", "execution-1"),
    ("0.006", "execution-1"), ("NaN", "execution-1"), ("Infinity", "execution-1"),
    ("0.005", None), ("0.005", "0"),
])
def test_terminal_repair_without_full_fill_evidence_keeps_intent_and_sibling(
    raw_status, actual_qty, actual_order_id,
):
    exchange = _RacingExchange(raw_status, actual_qty, actual_order_id=actual_order_id)
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=False)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"

    stored = job_store.load_bracket_recovery(task_id)
    assert stored["status"] == "submission_uncertain"
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][0]["repair_client_id"] == "br-repair-intent"
    assert stored["legs"][0]["repair_quantity"] == 0.005
    assert stored["legs"][1]["status"] == "placed"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"), ("fetch_order", "repair-stop"),
    ]


def test_filled_repair_with_conflicting_identity_does_not_cancel_sibling():
    exchange = _RacingExchange("FINISHED", "0.005")
    original_fetch = exchange.fetch_order

    def mismatched_fetch(order_id, symbol, params=None):
        order = original_fetch(order_id, symbol, params)
        order["info"]["clientAlgoId"] = "wrong-client"
        return order

    exchange.fetch_order = mismatched_fetch
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=False)
    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][0]["repair_client_id"] == "br-repair-intent"
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_lost_replacement_ack_does_not_adopt_stale_open_snapshot():
    exchange = _RacingExchange("FINISHED", None)
    exchange.orders["repair-stop"]["info"]["clientAlgoId"] = "br-replacement-intent"
    manager, _bridge, task_id = _pending_task(exchange, legs=[{
        "name": "stop-loss", "params": {"stopLossPrice": 47_000.0},
        "status": "uncertain", "order_id": "old-stop",
        "replacement_price": 48_000.0, "replacement_quantity": 0.005,
        "replacement_client_id": "br-replacement-intent", "price_key": "stopLossPrice",
    }, {
        "name": "take-profit", "params": {"takeProfitPrice": 55_000.0},
        "status": "placed", "order_id": "sibling",
    }])

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "old-stop"
    assert stored["legs"][0]["replacement_order_id"] == "repair-stop"
    assert stored["legs"][0]["replacement_client_id"] == "br-replacement-intent"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"), ("fetch_order", "repair-stop"),
    ]


@pytest.mark.parametrize("known_id", [True, False])
@pytest.mark.parametrize("raw_status", ["FILLED", "FINISHED"])
def test_initial_uncertain_leg_uses_later_filled_read(known_id, raw_status):
    exchange = _RacingExchange(raw_status, "0.005")
    manager, _bridge, task_id = _pending_task(exchange, legs=[{
        "name": "stop-loss", "params": {"stopLossPrice": 48_000.0},
        "status": "uncertain", "order_id": "repair-stop" if known_id else None,
    }, {
        "name": "take-profit", "params": {"takeProfitPrice": 55_000.0},
        "status": "placed", "order_id": "sibling",
    }])
    task = job_store.load_bracket_recovery(task_id)
    job_store.update_bracket_recovery(
        task_id, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error=None, now="2026-11-03T00:00:00+00:00", legs=task["legs"],
    )

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "filled"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"),
        ("fetch_order", "repair-stop"),
        ("fetch_order", "execution-1"),
        ("cancel", "sibling"),
    ]
    assert exchange.calls[1][3] == {"trigger": True}
    assert not exchange.calls[2][3]


def test_lost_repair_read_preserves_discovered_order_id_and_intent():
    exchange = _RacingExchange("FINISHED", "0.005")
    exchange.fail_verification = True
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=False)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][0]["repair_client_id"] == "br-repair-intent"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


@pytest.mark.parametrize("raw_status", ["FILLED", "FINISHED"])
def test_repair_ack_terminal_without_quantity_stays_uncertain(raw_status):
    exchange = _Exchange(repair_status=raw_status)
    manager, _bridge, task_id = _pending_task(exchange)

    assert _repair_with_lock(manager, exchange, task_id) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["status"] == "submission_uncertain"
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "1"
    assert stored["legs"][0]["repair_client_id"]
    assert [call[0] for call in exchange.calls].count("create") == 1


@pytest.mark.parametrize("change", [
    {"info": {"status": "CANCELED"}, "status": "canceled"},
    {"info": {"status": "PARTIALLY_FILLED", "executedQty": "0.002"}, "status": "open", "filled": 0.002},
    {"info": {"orderId": "wrong"}},
    {"id": "wrong"},
    {"symbol": "ETH/USDT:USDT"},
    {"info": {"symbol": "ETHUSDT"}},
    {"side": "buy"},
    {"info": {"side": "BUY"}},
    {"info": {"origQty": "0.006"}},
    {"info": {"executedQty": "0.002"}},
    {"info": {"executedQty": None}, "filled": None},
    {"info": {"status": None}},
    {"status": "open"},
])
def test_terminal_repair_requires_matching_fully_filled_child(change):
    exchange = _RacingExchange("FINISHED", "0.005")
    for key, value in change.items():
        if key == "info":
            exchange.child_order["info"].update(value)
        else:
            exchange.child_order[key] = value
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=False)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][0]["repair_client_id"] == "br-repair-intent"
    assert stored["legs"][1]["status"] == "placed"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"),
        ("fetch_order", "repair-stop"),
        ("fetch_order", "execution-1"),
    ]
    assert not exchange.calls[2][3]


def test_terminal_repair_child_read_failure_retains_intent_and_sibling():
    exchange = _RacingExchange("FINISHED", "0.005")
    exchange.child_order = OSError("child read unavailable")
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=False)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert stored["legs"][0]["repair_client_id"] == "br-repair-intent"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


@pytest.mark.parametrize("child_filled", [False, True])
def test_legacy_filled_leg_requires_fresh_child_evidence(child_filled):
    exchange = _RacingExchange("FINISHED", "0.005")
    if not child_filled:
        exchange.child_order["status"] = "canceled"
        exchange.child_order["info"]["status"] = "CANCELED"
    manager, _bridge, task_id = _pending_task(exchange, legs=[{
        "name": "stop-loss", "params": {"stopLossPrice": 48_000.0},
        "status": "filled", "order_id": "repair-stop", "quantity": 0.005,
    }, {
        "name": "take-profit", "params": {"takeProfitPrice": 55_000.0},
        "status": "placed", "order_id": "sibling",
    }])

    result = manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    )
    assert result == ("reconciled_stop_loss" if child_filled else "reconcile_incomplete")
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "filled"
    assert stored["legs"][0]["order_id"] == "repair-stop"
    assert [(call[0], call[1]) for call in exchange.calls] == [
        ("open_orders", "BTC/USDT:USDT"),
        ("fetch_order", "repair-stop"),
        ("fetch_order", "execution-1"),
    ] + ([("cancel", "sibling")] if child_filled else [])
    assert not exchange.calls[2][3]


def test_child_closed_without_raw_executed_quantity_is_not_fill_evidence():
    exchange = _RacingExchange("FINISHED", "0.005")
    exchange.child_order["info"]["executedQty"] = None
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=True)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_legacy_filled_without_order_id_stays_unresolved():
    exchange = _RacingExchange("FINISHED", "0.005")
    manager, _bridge, task_id = _pending_task(exchange, legs=[{
        "name": "stop-loss", "params": {"stopLossPrice": 48_000.0},
        "status": "filled", "order_id": None, "quantity": 0.005,
    }, {
        "name": "take-profit", "params": {"takeProfitPrice": 55_000.0},
        "status": "placed", "order_id": "sibling",
    }])

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_confirmed_fill_does_not_override_sibling_cancel_gate():
    exchange = _RacingExchange("FINISHED", "0.005")
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=True)

    assert manager._reconcile_for_verified_account(
        task_id, repair_missing=True, allow_sibling_cancel=False,
    ) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "filled"
    assert stored["legs"][1]["status"] == "placed"
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_repair_immediate_terminal_ack_requires_child_evidence():
    exchange = _Exchange(repair_status="FINISHED")
    original_create = exchange.create_order

    def create_with_child(symbol, kind, side, amount, price=None, params=None):
        response = original_create(symbol, kind, side, amount, price, params)
        exchange.orders[response["id"]]["info"].update(
            actualOrderId="child", actualQty="0.005",
        )
        return response

    original_fetch = exchange.fetch_order

    def fetch_with_failed_child(order_id, symbol, params=None):
        if order_id == "child":
            exchange.calls.append(("fetch_order", order_id, symbol, params))
            return {
                "id": "child", "symbol": symbol, "side": "sell", "status": "canceled",
                "amount": 0.005, "filled": 0.0,
                "info": {
                    "orderId": "child", "symbol": "BTCUSDT", "side": "SELL",
                    "status": "CANCELED", "origQty": "0.005", "executedQty": "0",
                },
            }
        return original_fetch(order_id, symbol, params)

    exchange.create_order = create_with_child
    exchange.fetch_order = fetch_with_failed_child
    manager, _bridge, task_id = _pending_task(exchange)

    assert _repair_with_lock(manager, exchange, task_id) == "reconcile_incomplete"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["order_id"] == "1"
    assert stored["legs"][0]["repair_client_id"]
    assert [(call[0], call[1]) for call in exchange.calls[-2:]] == [
        ("fetch_order", "1"), ("fetch_order", "child"),
    ]
    assert not exchange.calls[-1][3]


def test_public_reconcile_keeps_identity_guard_without_inspecting_orders():
    exchange = _RacingExchange("FINISHED", "0.005")
    manager, _bridge, task_id = _pending_racing_repair(exchange, known_id=True)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "account_identity_unknown"
    assert not exchange.calls
