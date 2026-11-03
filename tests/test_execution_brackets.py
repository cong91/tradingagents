"""Offline edge cases for the durable protective-exit lifecycle."""

from copy import deepcopy
from datetime import UTC, datetime

import ccxt
import pytest

from server import db, job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    api_key_identity,
    bind_identity,
)
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.sizing import PlannedOrder

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, postgres_schema):
    return postgres_schema


@pytest.fixture(autouse=True)
def verified_identity(monkeypatch):
    """The bracket manager only places and mutates with a verified binding."""
    bind_identity(monkeypatch, VERIFIED_IDENTITY)


def _strip_stored_identity(task_id: str) -> None:
    """Rewind a task to the legacy shape (pre-identity columns all NULL)."""
    with db.session() as conn:
        conn.execute(
            "UPDATE bracket_recovery_tasks SET account_identity_exchange = NULL, "
            "account_identity_profile = NULL, account_identity_type = NULL, "
            "account_identity_hash = NULL WHERE task_id = %s",
            (task_id,),
        )


class _Exchange:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.create_error = None
        self.cancel_error = None
        self.cancel_response = None
        self.emergency_response = {"id": "exit", "status": "closed"}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", symbol, side, amount, params))
        if params.get("clientOrderId", "").startswith("emergency-"):
            return self.emergency_response
        if params.get("clientOrderId", "").startswith("timestop-"):
            # A plain reduce-only market close: no conditional algo status, so
            # the confirmed "closed" status proves the fill.
            return {"id": "time-stop-exit", "status": "closed"}
        if self.create_error:
            raise self.create_error
        # Ids count only the algo orders; filled-child evidence rows never
        # consume a venue id slot.
        order_id = str(max([int(key) for key in self.orders if key.isdigit()] or [0]) + 1)
        self.orders[order_id] = {
            "id": order_id, "symbol": symbol, "side": side, "amount": amount,
            "status": "open", "info": {
                "algoId": order_id, "algoStatus": "NEW", "reduceOnly": True,
                "orderType": "STOP_MARKET" if "stopLossPrice" in params else "TAKE_PROFIT_MARKET",
                "triggerPrice": params.get("stopLossPrice", params.get("takeProfitPrice")),
                "quantity": str(amount), "side": side.upper(),
                "clientAlgoId": params.get("clientOrderId"),
                "workingType": params.get("workingType"),
            },
        }
        return {"id": order_id}

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append(("open", symbol, params))
        return [deepcopy(order) for order in self.orders.values() if order["info"]["algoStatus"] == "NEW"]

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("get", order_id, symbol, params))
        return deepcopy(self.orders[order_id])

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id, symbol, params))
        if self.cancel_error:
            raise self.cancel_error
        response = self.cancel_response or {"id": order_id, "info": {"algoId": order_id, "code": 200, "msg": "success"}}
        self.orders[order_id]["info"]["algoStatus"] = "CANCELED"
        return response

    def _confirm_child_fill(self, order_id):
        """Mark an algo leg filled WITH the child execution evidence the
        inspector independently verifies (FINISHED alone is 'transitioning')."""
        order = self.orders[order_id]
        child_id = f"{order_id}-fill"
        quantity = order["info"]["quantity"]
        order["info"]["algoStatus"] = "FINISHED"
        order["info"]["actualOrderId"] = child_id
        order["info"]["actualQty"] = quantity
        self.orders[child_id] = {
            "id": child_id, "symbol": order["symbol"], "side": order["side"],
            "amount": quantity, "type": "market", "status": "closed",
            "info": {
                "orderId": child_id, "symbol": "BTCUSDT", "side": "SELL",
                "status": "FILLED", "executedQty": quantity, "algoStatus": "FILLED",
            },
        }


class _Bridge:
    exchange_id = "binance"

    def __init__(self, exchange):
        self.config = {"exec_base_url": "https://demo-api.binance.com", "exec_sandbox": True}
        self.exchange = exchange
        self.audit = []

    def client(self):
        return self.exchange

    def _redact(self, message):
        return message.replace("secret-value", "[redacted]")

    def audit_plan(self, order, **kwargs):
        self.audit.append(kwargs)


def _bracket(exchange=None):
    exchange = exchange or _Exchange()
    bridge = _Bridge(exchange)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", market="derivatives",
        signal="Buy", side="buy", quantity=0.005, estimated_price=50000,
        cost=250, stop_loss=48000, take_profit=55000,
    )
    manager = BracketManager(bridge)
    manager.place(order, True, "entry-1")
    return manager, bridge, exchange, f"bracket:{order.ccxt_symbol}:entry-1"


def test_initial_acknowledgements_are_not_claimed_resting():
    manager, _bridge, _exchange, task_id = _bracket()
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "submission_uncertain"
    assert task["last_error"] == "conditional ACKs require independent resting verification"
    assert manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True) == "monitoring"


def test_legacy_or_switched_account_cannot_mutate_bracket():
    manager, _bridge, exchange, task_id = _bracket()
    _strip_stored_identity(task_id)
    before = len(exchange.calls)
    original = job_store.load_bracket_recovery(task_id)
    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "account_identity_unknown"
    assert manager.replace_leg(task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True) == "account_identity_unknown"
    assert manager.cancel_all(task_id, allow_mutation=True) == "account_identity_unknown"
    assert manager.close_position(task_id, reason="time_stop", allow_mutation=True) == "account_identity_unknown"
    assert exchange.calls[before:] == []
    assert job_store.load_bracket_recovery(task_id) == original


def test_same_profile_new_client_cannot_mutate_existing_task(monkeypatch):
    exchange = _Exchange()
    exchange.apiKey = "account-a"
    bind_identity(monkeypatch, api_key_identity)
    manager, bridge, _exchange, task_id = _bracket(exchange)
    second_exchange = _Exchange()
    second_exchange.apiKey = "account-b"
    second_bridge = _Bridge(second_exchange)
    assert second_bridge.config == bridge.config
    assert BracketManager(second_bridge).cancel_all(task_id, allow_mutation=True) == "account_identity_mismatch"
    assert BracketManager(second_bridge).replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True,
    ) == "account_identity_mismatch"
    assert second_exchange.calls == []
    assert job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"] == "1"


def test_existing_task_cannot_be_reused_for_new_bracket():
    manager, _bridge, exchange, task_id = _bracket()
    before = len(exchange.calls)
    original = job_store.load_bracket_recovery(task_id)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", market="derivatives",
        signal="Buy", side="buy", quantity=0.005, estimated_price=50000,
        cost=250, stop_loss=48000, take_profit=55000,
    )
    manager.place(order, True, "entry-1")
    assert exchange.calls[before:] == []
    assert job_store.load_bracket_recovery(task_id) == original


def test_monitor_cannot_use_bracket_from_different_venue_profile():
    manager, bridge, exchange, task_id = _bracket()
    original = job_store.load_bracket_recovery(task_id)
    assert original["status"] == "submission_uncertain"
    bridge.config["exec_base_url"] = "https://api.binance.com"
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "venue_mismatch"
    assert len(exchange.calls) == calls_before
    assert job_store.load_bracket_recovery(task_id) == original


def test_ambiguous_matching_orders_are_not_adopted():
    manager, bridge, exchange, task_id = _bracket()
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][0].update(order_id=None, status="pending")
    exchange.orders["second-stop"] = deepcopy(exchange.orders["1"])
    exchange.orders["second-stop"].update(id="second-stop")
    exchange.orders["second-stop"]["info"]["algoId"] = "second-stop"
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=["2"], attempts=0, last_error=None,
        now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "reconcile_incomplete"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create", "cancel"}]
    assert "ambiguous" in job_store.load_bracket_recovery(task_id)["last_error"]


@pytest.mark.parametrize("response", [
    {"id": "1", "status": "open"},
    {"id": "other", "status": "canceled"},
    {"id": "1", "info": {"code": "400", "msg": "failed"}},
])
def test_cancel_without_matching_success_stays_monitoring(response):
    manager, bridge, exchange, task_id = _bracket()
    exchange._confirm_child_fill("2")
    exchange.cancel_response = response

    assert manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True) == "reconcile_incomplete"
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "monitoring"
    assert task["legs"][1]["status"] == "filled"
    assert task["legs"][0]["status"] == "placed"


def test_cancel_failure_retries_only_sibling_after_restart():
    manager, bridge, exchange, task_id = _bracket()
    exchange._confirm_child_fill("2")
    exchange.cancel_error = ccxt.NetworkError("secret-value unavailable")

    assert manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True) == "reconcile_incomplete"
    assert "secret-value" not in job_store.load_bracket_recovery(task_id)["last_error"]
    exchange.cancel_error = None
    fresh_manager = BracketManager(bridge)
    assert fresh_manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True) == "reconciled_take_profit"
    assert [call[1] for call in exchange.calls if call[0] == "cancel"] == ["1", "1"]
    assert len([call for call in exchange.calls if call[0] == "create"]) == 2


def test_replacing_leg_places_and_persists_new_order_before_canceling_old():
    manager, bridge, exchange, task_id = _bracket()
    original_task = job_store.load_bracket_recovery(task_id)
    old_stop_id = original_task["legs"][0]["order_id"]

    status = manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    )

    task = job_store.load_bracket_recovery(task_id)
    new_stop_id = task["legs"][0]["order_id"]
    calls = [call[0:2] for call in exchange.calls]
    assert status == "replaced"
    assert new_stop_id != old_stop_id
    assert task["legs"][0]["params"] == {"stopLossPrice": 49_000}
    assert calls.index(("create", "BTC/USDT:USDT")) < calls.index(("cancel", old_stop_id))
    assert old_stop_id not in [order["id"] for order in exchange.fetch_open_orders("BTC/USDT:USDT")]


def test_replacing_leg_refuses_to_cancel_a_triggering_or_filled_order():
    manager, bridge, exchange, task_id = _bracket()
    task = job_store.load_bracket_recovery(task_id)
    old_stop_id = task["legs"][0]["order_id"]
    exchange.orders[old_stop_id]["info"]["algoStatus"] = "TRIGGERING"
    calls_before = len(exchange.calls)

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    ) == "leg_triggering"
    new_calls = exchange.calls[calls_before:]
    assert not [call for call in new_calls if call[0] in {"create", "cancel"}]


def test_uncertain_replacement_is_adopted_after_restart_without_duplicate_submit():
    class _TimeoutAfterAccept(_Exchange):
        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            response = super().create_order(symbol, kind, side, amount, price, params)
            if params and params.get("stopLossPrice") == 49_000:
                raise ccxt.RequestTimeout("replacement acknowledgement lost")
            return response

    exchange = _TimeoutAfterAccept()
    manager, bridge, exchange, task_id = _bracket(exchange)
    old_stop_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    ) == "submission_uncertain"
    submissions = len([call for call in exchange.calls if call[0] == "create"])

    recovered = BracketManager(bridge)
    assert recovered.reconcile(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "monitoring"

    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["params"] == {"stopLossPrice": 49_000}
    assert task["legs"][0]["order_id"] != old_stop_id
    assert len([call for call in exchange.calls if call[0] == "create"]) == submissions
    assert [call[1] for call in exchange.calls if call[0] == "cancel"].count(old_stop_id) == 1


def test_replacement_ack_is_not_proof_of_a_resting_leg():
    """An accepted create with an id, followed by a venue state showing the
    replacement dead, must keep the old leg and never claim "replaced"."""
    manager, bridge, exchange, task_id = _bracket()
    old_stop_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]
    accepted = exchange.create_order

    def accept_then_expire(symbol, kind, side, amount, price=None, params=None):
        response = accepted(symbol, kind, side, amount, price, params)
        exchange.orders[response["id"]]["info"]["algoStatus"] = "EXPIRED"
        return response

    exchange.create_order = accept_then_expire

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    ) == "replacement_failed"
    task = job_store.load_bracket_recovery(task_id)
    leg = task["legs"][0]
    assert leg["order_id"] == old_stop_id
    assert leg["status"] == "placed"
    assert leg["params"] == {"stopLossPrice": 48_000}
    assert leg.get("replacement_price") is None
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_replacement_resting_read_failure_keeps_old_leg_until_reconcile():
    """A lost verification read leaves the replacement uncertain with the old
    leg live; the next reconcile adopts the resting order and retires the old."""

    class _VerificationBlind(_Exchange):
        def __init__(self):
            super().__init__()
            self.verification_failures = 1

        def fetch_order(self, order_id, symbol, params=None):
            if order_id == "3" and self.verification_failures > 0:
                self.verification_failures -= 1
                raise ccxt.NetworkError("replacement resting read lost")
            return super().fetch_order(order_id, symbol, params)

    exchange = _VerificationBlind()
    manager, bridge, exchange, task_id = _bracket(exchange)
    old_stop_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    ) == "submission_uncertain"
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == old_stop_id
    assert task["legs"][0]["status"] == "uncertain"
    assert task["legs"][0]["replacement_price"] == 49_000
    assert task["legs"][0]["replacement_order_id"] == "3"
    assert task["legs"][0]["replacement_client_id"]
    assert not [call for call in exchange.calls if call[0] == "cancel"]

    assert BracketManager(bridge).reconcile(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "monitoring"
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] != old_stop_id
    assert task["legs"][0]["status"] == "placed"
    assert task["legs"][0]["params"] == {"stopLossPrice": 49_000}
    assert [call[1] for call in exchange.calls if call[0] == "cancel"].count(old_stop_id) == 1


@pytest.mark.parametrize("raw_status", ["TRIGGERING", "TRIGGERED", "PARTIALLY_FILLED", "FINISHED", "UNKNOWN"])
def test_replacement_non_resting_state_keeps_old_stop(raw_status):
    manager, bridge, exchange, task_id = _bracket()
    old_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]
    original = exchange.create_order

    def create(symbol, kind, side, amount, price=None, params=None):
        response = original(symbol, kind, side, amount, price, params)
        exchange.orders[response["id"]]["info"]["algoStatus"] = raw_status
        return response

    exchange.create_order = create
    assert manager._replace_leg_for_verified_account(task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True) == "submission_uncertain"
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == old_id
    assert task["legs"][0]["replacement_order_id"] == "3"
    assert task["legs"][0]["replacement_client_id"]
    assert not [call for call in exchange.calls if call[0] == "cancel"]
    assert BracketManager(bridge)._reconcile_for_verified_account(task_id, repair_missing=False, allow_sibling_cancel=True) == "reconcile_incomplete"
    assert not [call for call in exchange.calls if call[0] == "cancel"]


@pytest.mark.parametrize("field,value", [
    ("id", "wrong"), ("symbol", "ETH/USDT:USDT"), ("side", "buy"),
    ("reduceOnly", False), ("triggerPrice", 47_000), ("quantity", 0.004),
])
def test_replacement_requires_matching_independent_order(field, value):
    manager, _bridge, exchange, task_id = _bracket()
    old_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]
    original = exchange.fetch_order

    def fetch(order_id, symbol, params=None):
        item = original(order_id, symbol, params)
        if order_id == "3":
            if field in {"id", "symbol"}:
                item[field] = value
            else:
                item["info"][field] = value
        return item

    exchange.fetch_order = fetch
    assert manager._replace_leg_for_verified_account(task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True) == "submission_uncertain"
    assert job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"] == old_id
    assert not [call for call in exchange.calls if call[0] == "cancel"]


@pytest.mark.parametrize("change", [
    {"info": {"symbol": "ETHUSDT"}},
    {"side": "buy"},
    {"reduceOnly": False},
    {"amount": 0.004},
    {"triggerPrice": 47_000},
    {"info": {"algoId": "other"}},
    {"info": {"origType": "TAKE_PROFIT_MARKET"}},
    {"info": {"workingType": "CONTRACT_PRICE"}},
    {"info": {"workingType": None}},
    {"info": {"clientAlgoId": "wrong-client"}},
    {"info": {"clientAlgoId": None}},
])
def test_conflicting_order_evidence_keeps_old_leg(change):
    manager, _bridge, exchange, task_id = _bracket()
    original = exchange.fetch_order

    def fetch(order_id, symbol, params=None):
        item = original(order_id, symbol, params)
        if order_id == "3":
            for key, value in change.items():
                if key == "info":
                    item["info"].update(value)
                else:
                    item[key] = value
        return item

    exchange.fetch_order = fetch
    assert manager._replace_leg_for_verified_account(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True,
    ) == "submission_uncertain"
    assert job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"] == "1"
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_replacement_ack_same_id_as_old_stop_never_cancels():
    manager, _bridge, exchange, task_id = _bracket()
    original = exchange.create_order

    def create(symbol, kind, side, amount, price=None, params=None):
        response = original(symbol, kind, side, amount, price, params)
        exchange.orders["1"] = deepcopy(exchange.orders[response["id"]])
        exchange.orders["1"]["id"] = "1"
        exchange.orders["1"]["info"]["algoId"] = "1"
        return {"id": "1"}

    exchange.create_order = create
    assert manager._replace_leg_for_verified_account(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True,
    ) == "submission_uncertain"
    assert not [call for call in exchange.calls if call[0] == "cancel"]
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == task["legs"][0]["replacement_order_id"] == "1"


def test_lost_replacement_response_recovers_using_persisted_client_id():
    class LostResponse(_Exchange):
        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            response = super().create_order(symbol, kind, side, amount, price, params)
            if params and params.get("stopLossPrice") == 49_000:
                raise ccxt.RequestTimeout("response lost")
            return response

    manager, bridge, exchange, task_id = _bracket(LostResponse())
    old_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]
    assert manager._replace_leg_for_verified_account(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005, allow_mutation=True,
    ) == "submission_uncertain"
    before = job_store.load_bracket_recovery(task_id)["legs"][0]
    assert before["replacement_client_id"] and before["order_id"] == old_id
    assert not before.get("replacement_order_id")
    assert BracketManager(bridge)._reconcile_for_verified_account(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "monitoring"
    after = job_store.load_bracket_recovery(task_id)["legs"][0]
    assert after["order_id"] == "3"
    assert [call[1] for call in exchange.calls if call[0] == "cancel"] == [old_id]


def test_binance_conditional_orders_use_mark_price_trigger_source():
    _manager, _bridge, exchange, _task_id = _bracket()

    conditional_calls = [
        call for call in exchange.calls
        if call[0] == "create" and call[4]
    ]

    assert all(call[4]["workingType"] == "MARK_PRICE" for call in conditional_calls)


def test_replacement_below_minimum_stop_move_is_ignored():
    manager, _bridge, _exchange, task_id = _bracket()

    status = manager.replace_leg(
        task_id, "stop-loss", trigger_price=48_005, quantity=0.005,
        allow_mutation=True, min_stop_move=10.0,
    )

    assert status == "below_minimum_stop_move"


class _RawV3Exchange(_Exchange):
    """Adds the account-scope surface the raw V3 position read validates."""

    def __init__(self):
        super().__init__()
        self.contracts = 0.0
        self.raw_position_error = None
        self.urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        self.options = {"enableDemoTrading": True, "sandboxMode": False}
        self.verbose = False
        self.markets_by_id = {"BTCUSDT": {
            "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "contract": True,
            "linear": True, "inverse": False, "quote": "USDT", "settle": "USDT",
            "settleId": "USDT",
        }}

    def fapiPrivateV3GetPositionRisk(self, params=None):
        if self.raw_position_error is not None:
            raise self.raw_position_error
        return [{
            "symbol": "BTCUSDT", "positionSide": "BOTH",
            "positionAmt": f"{self.contracts:.6f}",
            "entryPrice": "50000.00", "markPrice": "50000.00", "marginAsset": "USDT",
        }]

    def market(self, symbol):
        return {"quote": "USDT", "settle": "USDT", "inverse": False}


class _LadderExchange(_RawV3Exchange):
    """Adds the position size the ladder settlement and closes read."""

    def __init__(self, contracts):
        super().__init__()
        self.contracts = contracts


def _ladder(contracts=0.005):
    exchange = _LadderExchange(contracts)
    bridge = _Bridge(exchange)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", market="derivatives",
        signal="Buy", side="buy", quantity=contracts, estimated_price=50_000,
        cost=250, stop_loss=48_000, take_profit=55_000,
    )
    manager = BracketManager(bridge)
    manager.place_ladder(
        order, [(52_000.0, 0.00165), (54_000.0, 0.00165)], True, "entry-1",
    )
    return manager, bridge, exchange, f"bracket:{order.ccxt_symbol}:entry-1"


def test_ladder_places_full_size_stop_and_partial_targets():
    _manager, _bridge, exchange, _task_id = _ladder()

    creates = [call for call in exchange.calls if call[0] == "create"]
    assert [(call[2], call[3]) for call in creates] == [
        ("sell", 0.005), ("sell", 0.00165), ("sell", 0.00165),
    ]
    assert [call[4].get("stopLossPrice") for call in creates] == [48_000, None, None]
    assert [call[4].get("takeProfitPrice") for call in creates] == [None, 52_000, 54_000]
    assert all(call[4]["workingType"] == "MARK_PRICE" for call in creates)


def test_filled_tranche_keeps_the_rest_of_the_ladder_and_resizes_the_stop():
    manager, _bridge, exchange, task_id = _ladder()
    task = job_store.load_bracket_recovery(task_id)
    first_tp = next(leg for leg in task["legs"] if leg["name"] == "take-profit-1")
    exchange._confirm_child_fill(first_tp["order_id"])
    exchange.contracts = 0.00335  # 0.005 - 0.00165 after the first tranche
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True)

    task = job_store.load_bracket_recovery(task_id)
    stop = next(leg for leg in task["legs"] if leg["name"] == "stop-loss")
    second_tp = next(leg for leg in task["legs"] if leg["name"] == "take-profit-2")
    assert status == "monitoring"
    assert stop["quantity"] == pytest.approx(0.00335)
    assert second_tp["status"] == "placed"  # a deliberate tranche is never cancelled
    cancels = [call[1] for call in exchange.calls[calls_before:] if call[0] == "cancel"]
    assert first_tp["order_id"] not in cancels
    assert second_tp["order_id"] not in cancels


def test_ladder_unknown_position_does_not_resize_or_complete():
    manager, _bridge, exchange, task_id = _ladder()
    task = job_store.load_bracket_recovery(task_id)
    first_tp = next(leg for leg in task["legs"] if leg["name"] == "take-profit-1")
    exchange._confirm_child_fill(first_tp["order_id"])
    exchange.fapiPrivateV3GetPositionRisk = lambda params=None: []
    before = len(exchange.calls)

    assert manager._reconcile_for_verified_account(task_id, repair_missing=False, allow_sibling_cancel=True) == "monitoring"
    assert not [call for call in exchange.calls[before:] if call[0] in {"create", "cancel"}]
    assert "position unknown" in job_store.load_bracket_recovery(task_id)["last_error"]


def test_ladder_uncertain_resize_keeps_persisted_replacement_intent(monkeypatch):
    monkeypatch.setattr(BracketManager, "replace_leg", BracketManager._replace_leg_for_verified_account)
    manager, _bridge, exchange, task_id = _ladder()
    first_tp = job_store.load_bracket_recovery(task_id)["legs"][1]["order_id"]
    exchange._confirm_child_fill(first_tp)
    exchange.contracts = 0.00335
    original = exchange.fetch_order

    def fetch(order_id, symbol, params=None):
        if order_id == "4":
            raise ccxt.NetworkError("verification lost")
        return original(order_id, symbol, params)

    exchange.fetch_order = fetch
    assert manager._reconcile_for_verified_account(task_id, repair_missing=False, allow_sibling_cancel=True) == "submission_uncertain"
    stop = job_store.load_bracket_recovery(task_id)["legs"][0]
    assert stop["replacement_order_id"] == "4"
    assert stop["replacement_quantity"] == pytest.approx(0.00335)
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_close_position_flattens_only_the_remaining_contracts_then_cancels_legs():
    manager, _bridge, exchange, task_id = _ladder()
    exchange.contracts = 0.00335  # a tranche already filled; 0.005 is stale
    task = job_store.load_bracket_recovery(task_id)
    resting_ids = {leg["order_id"] for leg in task["legs"]}
    calls_before = len(exchange.calls)

    status = manager.close_position(task_id, reason="time_stop", allow_mutation=True)

    new_calls = exchange.calls[calls_before:]
    creates = [call for call in new_calls if call[0] == "create"]
    cancels = [call[1] for call in new_calls if call[0] == "cancel"]
    assert status == "time_stop_closed"
    assert len(creates) == 1
    _kind, _symbol, close_side, amount, params = creates[0]
    assert close_side == "sell"
    assert amount == pytest.approx(0.00335)
    assert params["reduceOnly"] is True
    assert params["clientOrderId"].startswith("timestop-")
    assert set(cancels) == resting_ids
    assert new_calls.index(creates[0]) < new_calls.index(
        next(call for call in new_calls if call[0] == "cancel")
    )
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"


def test_close_position_only_cancels_legs_when_the_venue_is_already_flat():
    manager, _bridge, exchange, task_id = _ladder(contracts=0.0)
    # Flatness is never inferred: with the position book unreadable the close
    # refuses instead of cancelling legs.
    exchange.raw_position_error = ccxt.NetworkError("position read lost")
    task = job_store.load_bracket_recovery(task_id)
    resting_ids = {leg["order_id"] for leg in task["legs"]}
    calls_before = len(exchange.calls)

    status = manager.close_position(task_id, reason="time_stop", allow_mutation=True)

    new_calls = exchange.calls[calls_before:]
    assert status == "time_stop_unknown_position"
    assert not [call for call in new_calls if call[0] in {"create", "cancel"}]
    assert {leg["order_id"] for leg in job_store.load_bracket_recovery(task_id)["legs"]} == resting_ids


def test_close_position_is_gated_without_mutation_permission():
    manager, _bridge, exchange, task_id = _ladder()
    calls_before = len(exchange.calls)

    assert manager.close_position(task_id, reason="time_stop", allow_mutation=False) == "mutation_gated"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create", "cancel"}]


def _mark_time_stop_uncertain(task_id: str, status: str, error: str) -> None:
    task = job_store.load_bracket_recovery(task_id)
    job_store.update_bracket_recovery(
        task_id, status=status, placed_ids=[leg["order_id"] for leg in task["legs"]],
        attempts=1, last_error=error, now=datetime.now(UTC).isoformat(),
    )


def test_reconcile_re_drives_a_time_stop_close_that_never_confirmed():
    exchange = _LadderExchange(0.00335)  # a tranche filled before the close went uncertain
    manager, _bridge, exchange, task_id = _bracket(exchange)
    task = job_store.load_bracket_recovery(task_id)
    resting_ids = {leg["order_id"] for leg in task["legs"]}
    _mark_time_stop_uncertain(
        task_id, "time_stop_submission_uncertain",
        "close submission uncertain; inspect before retry",
    )
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True)

    new_calls = exchange.calls[calls_before:]
    creates = [call for call in new_calls if call[0] == "create"]
    assert status == "time_stop_closed"
    assert len(creates) == 1
    _kind, _symbol, close_side, amount, params = creates[0]
    assert close_side == "sell"
    assert amount == pytest.approx(0.00335)
    assert params["reduceOnly"] is True
    assert params["clientOrderId"].startswith("timestop-")
    assert {call[1] for call in new_calls if call[0] == "cancel"} == resting_ids
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"


def test_time_stop_close_fence_survives_a_failed_venue_read():
    exchange = _Exchange()  # no fetch_positions: the remaining size is unknowable
    manager, _bridge, exchange, task_id = _bracket(exchange)
    _mark_time_stop_uncertain(
        task_id, "time_stop_submission_uncertain",
        "close submission uncertain; inspect before retry",
    )
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True)

    assert status == "time_stop_unknown_position"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create", "cancel"}]
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "time_stop_submission_uncertain"
    assert "venue position read failed" in task["last_error"]


def test_time_stop_close_fence_respects_the_mutation_gate():
    exchange = _LadderExchange(0.005)
    manager, _bridge, exchange, task_id = _bracket(exchange)
    _mark_time_stop_uncertain(task_id, "time_stop_failed", "close order failed")
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=False) == "mutation_gated"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create", "cancel"}]
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_failed"


def test_cancel_failure_after_replacement_keeps_old_id_for_later_cleanup():
    manager, bridge, exchange, task_id = _bracket()
    old_stop_id = job_store.load_bracket_recovery(task_id)["legs"][0]["order_id"]
    exchange.cancel_error = ccxt.NetworkError("cancel response unavailable")

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
        allow_mutation=True,
    ) == "replacement_cleanup_pending"
    task = job_store.load_bracket_recovery(task_id)
    assert old_stop_id in task["legs"][0]["retired_ids"]
    assert task["legs"][0]["order_id"] != old_stop_id

    exchange.cancel_error = None
    assert BracketManager(bridge).reconcile(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "monitoring"
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["retired_ids"] == []
    assert [call[1] for call in exchange.calls if call[0] == "cancel"].count(old_stop_id) == 2


@pytest.mark.parametrize("response", [{"id": "exit"}, {"id": "exit", "status": "open"}, None])
def test_emergency_acknowledgement_is_not_a_confirmed_close(response):
    exchange = _LadderExchange(0.005)
    exchange.create_error = ccxt.ExchangeError("protection refused")
    manager, bridge, exchange, task_id = _bracket(exchange)
    exchange.emergency_response = response

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "emergency_close_failed"
    assert job_store.load_bracket_recovery(task_id)["status"] == "emergency_close_failed"
    calls_before = len(exchange.calls)
    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "emergency_close_failed"
    assert len(exchange.calls) > calls_before


def test_emergency_close_scales_to_the_venue_remaining_contracts():
    exchange = _LadderExchange(0.00335)  # one tranche already filled; 0.005 is stale
    exchange.create_error = ccxt.ExchangeError("protection refused")
    manager, _bridge, exchange, task_id = _bracket(exchange)
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    new_calls = exchange.calls[calls_before:]
    creates = [call for call in new_calls if call[0] == "create"]
    # Both the stop repair and the emergency close submit only what remains.
    assert [call[3] for call in creates] == [pytest.approx(0.00335), pytest.approx(0.00335)]
    emergency = next(call for call in creates if str(call[4].get("clientOrderId", "")).startswith("emergency-"))
    assert emergency[4]["reduceOnly"] is True
    assert status == "emergency_closed"
    assert job_store.load_bracket_recovery(task_id)["status"] == "emergency_closed"


class _PositionReadDiesAfterFirstUse(_RawV3Exchange):
    """The repair's position read succeeds once, then the venue stops answering."""

    def __init__(self):
        super().__init__()
        self.position_reads = 0

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.position_reads += 1
        if self.position_reads > 1:
            raise ccxt.NetworkError("position read lost")
        return super().fapiPrivateV3GetPositionRisk(params)


def test_repair_without_a_venue_position_read_submits_nothing_and_retries():
    exchange = _RawV3Exchange()  # the position read starts broken: size unknowable
    exchange.raw_position_error = ccxt.NetworkError("position read lost")
    manager, _bridge, exchange, task_id = _bracket(exchange)
    task = job_store.load_bracket_recovery(task_id)
    lost_id = task["legs"][1]["order_id"]
    task["legs"][1].update(status="pending", order_id=None)
    exchange.orders.pop(lost_id)
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=[task["legs"][0]["order_id"]], attempts=0,
        last_error=None, now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    new_calls = exchange.calls[calls_before:]
    assert status == "pending"
    assert not [call for call in new_calls if call[0] in {"create", "cancel"}]
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "pending"
    assert "venue position read failed" in task["last_error"]

    exchange.raw_position_error = None
    exchange.contracts = 0.005  # the venue answers with the live position
    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "monitoring"
    creates = [call for call in exchange.calls[calls_before:] if call[0] == "create"]
    assert [call[3] for call in creates] == [pytest.approx(0.005)]
    assert job_store.load_bracket_recovery(task_id)["status"] == "monitoring"


def test_emergency_close_after_a_failed_second_position_read_submits_nothing():
    exchange = _PositionReadDiesAfterFirstUse()
    exchange.contracts = 0.005
    exchange.create_error = ccxt.ExchangeError("protection refused")
    manager, _bridge, exchange, task_id = _bracket(exchange)
    calls_before = len(exchange.calls)

    status = manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    new_calls = exchange.calls[calls_before:]
    assert status == "emergency_close_failed"
    assert not [
        call for call in new_calls
        if call[0] == "create" and str(call[4].get("clientOrderId", "")).startswith("emergency-")
    ]
    assert job_store.load_bracket_recovery(task_id)["status"] == "emergency_close_failed"


def test_timed_out_initial_submit_is_monitored_without_retry():
    class _SecondLegTimeout(_Exchange):
        def __init__(self):
            super().__init__()
            self.algo_submits = 0

        def create_order(self, symbol, kind, side, amount, price=None, params=None):
            if params and ("stopLossPrice" in params or "takeProfitPrice" in params):
                self.algo_submits += 1
                if self.algo_submits == 2:
                    self.calls.append(("create", symbol, side, amount, params))
                    raise ccxt.RequestTimeout("submit response lost")
            return super().create_order(symbol, kind, side, amount, price, params)

    exchange = _SecondLegTimeout()
    manager, bridge, _exchange, task_id = _bracket(exchange)
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "submission_uncertain"
    assert [leg["status"] for leg in task["legs"]] == ["placed", "uncertain"]
    submit_count = exchange.algo_submits

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "reconcile_incomplete"
    assert exchange.algo_submits == submit_count
    assert job_store.load_bracket_recovery(task_id)["status"] == "submission_uncertain"


def test_timed_out_repair_does_not_emergency_close():
    exchange = _LadderExchange(0.005)  # a readable venue lets the repair submit
    manager, bridge, exchange, task_id = _bracket(exchange)
    task = job_store.load_bracket_recovery(task_id)
    uncertain_id = task["legs"][1]["order_id"]
    task["legs"][1].update(status="pending", order_id=None)
    exchange.orders.pop(uncertain_id)
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=[task["legs"][0]["order_id"]], attempts=0,
        last_error=None, now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )
    exchange.create_error = ccxt.RequestTimeout("no response")
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "reconcile_incomplete"
    new_calls = exchange.calls[calls_before:]
    # The timed-out repair submit is recorded, but it never escalates to an
    # emergency close and nothing resting is cancelled.
    assert not [call for call in new_calls if call[0] == "cancel"]
    assert not [
        call for call in new_calls
        if call[0] == "create" and str(call[4].get("clientOrderId", "")).startswith("emergency-")
    ]
    assert job_store.load_bracket_recovery(task_id)["status"] == "submission_uncertain"
