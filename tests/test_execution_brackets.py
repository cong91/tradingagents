"""Offline edge cases for the durable protective-exit lifecycle."""

from copy import deepcopy
from datetime import datetime, timezone

import ccxt
import pytest

from server import job_store
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.sizing import PlannedOrder

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, postgres_schema):
    return postgres_schema
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
        if self.create_error:
            raise self.create_error
        order_id = str(len(self.orders) + 1)
        self.orders[order_id] = {
            "id": order_id, "symbol": symbol, "side": side, "amount": amount,
            "status": "open", "info": {
                "algoId": order_id, "algoStatus": "NEW", "reduceOnly": True,
                "orderType": "STOP_MARKET" if "stopLossPrice" in params else "TAKE_PROFIT_MARKET",
                "triggerPrice": params.get("stopLossPrice", params.get("takeProfitPrice")),
                "quantity": str(amount), "side": side.upper(),
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


def test_monitor_cannot_use_bracket_from_different_venue_profile():
    manager, bridge, exchange, task_id = _bracket()
    bridge.config["exec_base_url"] = "https://api.binance.com"
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "venue_mismatch"
    assert len(exchange.calls) == calls_before
    assert job_store.load_bracket_recovery(task_id)["status"] == "monitoring"


def test_ambiguous_matching_orders_are_not_adopted():
    manager, bridge, exchange, task_id = _bracket()
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][0].update(order_id=None, status="pending")
    exchange.orders["second-stop"] = deepcopy(exchange.orders["1"])
    exchange.orders["second-stop"].update(id="second-stop")
    exchange.orders["second-stop"]["info"]["algoId"] = "second-stop"
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=["2"], attempts=0, last_error=None,
        now=datetime.now(timezone.utc).isoformat(), legs=task["legs"],
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
    exchange.orders["2"]["info"]["algoStatus"] = "FINISHED"
    exchange.cancel_response = response

    assert manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True) == "reconcile_incomplete"
    task = job_store.load_bracket_recovery(task_id)
    assert task["status"] == "monitoring"
    assert task["legs"][1]["status"] == "filled"
    assert task["legs"][0]["status"] == "placed"


def test_cancel_failure_retries_only_sibling_after_restart():
    manager, bridge, exchange, task_id = _bracket()
    exchange.orders["2"]["info"]["algoStatus"] = "FINISHED"
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


class _LadderExchange(_Exchange):
    """Adds the position read the ladder settlement needs."""

    def __init__(self, contracts):
        super().__init__()
        self.contracts = contracts

    def fetch_positions(self, symbols=None, params=None):
        return [{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": self.contracts,
            "entryPrice": 50_000.0, "markPrice": 50_000.0,
        }]

    def market(self, symbol):
        return {"quote": "USDT", "settle": "USDT", "inverse": False}


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
    exchange.orders[first_tp["order_id"]]["info"]["algoStatus"] = "FINISHED"
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
    exchange = _Exchange()
    exchange.create_error = ccxt.ExchangeError("protection refused")
    manager, bridge, exchange, task_id = _bracket(exchange)
    exchange.emergency_response = response

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "emergency_close_failed"
    assert job_store.load_bracket_recovery(task_id)["status"] == "emergency_close_failed"
    calls_before = len(exchange.calls)
    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "emergency_close_failed"
    assert len(exchange.calls) > calls_before


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
    exchange = _Exchange()
    manager, bridge, exchange, task_id = _bracket(exchange)
    task = job_store.load_bracket_recovery(task_id)
    uncertain_id = task["legs"][1]["order_id"]
    task["legs"][1].update(status="pending", order_id=None)
    exchange.orders.pop(uncertain_id)
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=[task["legs"][0]["order_id"]], attempts=0,
        last_error=None, now=datetime.now(timezone.utc).isoformat(), legs=task["legs"],
    )
    exchange.create_error = ccxt.RequestTimeout("no response")
    calls_before = len(exchange.calls)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "reconcile_incomplete"
    assert not [call for call in exchange.calls[calls_before:] if call[0] == "create" and "clientOrderId" in call[4]]
    assert job_store.load_bracket_recovery(task_id)["status"] == "submission_uncertain"
