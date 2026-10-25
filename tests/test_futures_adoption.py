"""Offline tests for applying an adoption plan on a fake venue."""

import pytest

from server import job_store
from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.execution.futures_adoption import adopt_position


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, postgres_schema):
    return postgres_schema
class _Exchange:
    def __init__(self, legs):
        self.legs = legs
        self.calls = []
        self.create_error = None
        self.cancel_error = None
        self.next_id = 900

    def fapiPrivateGetOpenAlgoOrders(self):
        self.calls.append(("list",))
        return [dict(leg) for leg in self.legs if leg["algoStatus"] in {"NEW", "TRIGGERED"}]

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", side, amount, params))
        if self.create_error is not None:
            raise self.create_error
        self.next_id += 1
        order_id = str(self.next_id)
        return {"id": order_id}

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id))
        if self.cancel_error is not None:
            raise self.cancel_error
        for leg in self.legs:
            if str(leg["algoId"]) == str(order_id):
                leg["algoStatus"] = "CANCELED"
        return {"id": order_id, "info": {"code": 200, "msg": "success"}}


class _Bridge:
    exchange_id = "binance"

    def __init__(self, exchange):
        self.config = {"exec_base_url": "https://demo-api.binance.com", "exec_sandbox": True}
        self.exchange = exchange

    def client(self):
        return self.exchange

    def _redact(self, message):
        return message


def _position(contracts=510.3):
    return FuturesPosition(
        symbol="ZRO/USDT:USDT", side="short", contracts=contracts,
        quantity=-contracts, entry_price=2.0368, mark_price=1.8553,
    )


def _legs():
    def leg(algo_id, order_type, quantity, trigger):
        return {
            "algoId": algo_id, "symbol": "ZROUSDT", "orderType": order_type,
            "quantity": str(quantity), "triggerPrice": str(trigger), "algoStatus": "NEW",
        }
    return [
        leg(1, "STOP_MARKET", 161.9, 2.2782),
        leg(2, "STOP_MARKET", 190.3, 2.3126),
        leg(3, "STOP_MARKET", 158.1, 2.4088),
        leg(4, "TAKE_PROFIT_MARKET", 161.9, 1.55),
        leg(5, "TAKE_PROFIT_MARKET", 190.3, 1.5514),
        leg(6, "TAKE_PROFIT_MARKET", 158.1, 1.6684),
    ]


def test_adoption_places_full_size_legs_before_cancelling_the_ladder():
    exchange = _Exchange(_legs())

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=_legs(), allow_mutation=True)

    assert report["status"] == "adopted"
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("create") < kinds.index("cancel")
    creates = [call for call in exchange.calls if call[0] == "create"]
    assert [call[2] for call in creates] == [510.3, 510.3]
    assert all(call[3]["workingType"] == "MARK_PRICE" for call in creates)
    assert all(call[3]["reduceOnly"] is True for call in creates)
    assert report["cancelled"] == ["1", "2", "3", "4", "5", "6"]

    state = job_store.load_position_state("ZRO/USDT:USDT:sell:adopted")
    assert state["snapshot"]["contracts"] == pytest.approx(510.3)
    assert state["bracket_task_id"] == "bracket:ZRO/USDT:USDT:adopted"
    task = job_store.load_bracket_recovery(state["bracket_task_id"])
    assert task["status"] == "monitoring"


def test_replacement_failure_leaves_the_ladder_untouched():
    exchange = _Exchange(_legs())
    exchange.create_error = RuntimeError("venue rejected the stop")

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=_legs(), allow_mutation=True)

    assert report["status"] == "replacement_failed"
    assert not [call for call in exchange.calls if call[0] == "cancel"]
    assert job_store.load_position_state("ZRO/USDT:USDT:sell:adopted") is None


def test_cancel_failure_reports_pending_without_writing_state():
    exchange = _Exchange(_legs())
    exchange.cancel_error = RuntimeError("network down")

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=_legs(), allow_mutation=True)

    assert report["status"] == "replacement_cleanup_pending"
    assert job_store.load_position_state("ZRO/USDT:USDT:sell:adopted") is None


def test_mutation_gate_reports_the_plan_only():
    exchange = _Exchange(_legs())

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=_legs(), allow_mutation=False)

    assert report["status"] == "mutation_gated"
    assert report["plan"]["stop"] == pytest.approx(
        (161.9 * 2.2782 + 190.3 * 2.3126 + 158.1 * 2.4088) / 510.3
    )
    assert not exchange.calls


def test_ladder_without_a_stop_is_refused():
    legs = [leg for leg in _legs() if leg["orderType"] == "TAKE_PROFIT_MARKET"]
    exchange = _Exchange(legs)

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=legs, allow_mutation=True)

    assert report["status"] == "no_stop_to_adopt"
    assert not exchange.calls


def test_duplicate_states_are_pruned_to_one():
    for index in range(3):
        job_store.persist_futures_entry_state(
            f"ZRO/USDT:USDT:sell:old-{index}", side="short", entry=2.0,
            quantity=1.0, stop=2.3, targets=[1.5], mark=2.0, order_id=f"old-{index}",
        )
    exchange = _Exchange(_legs())

    report = adopt_position(_Bridge(exchange), _position(), venue_legs=_legs(), allow_mutation=True)

    assert report["pruned_states"] == 3
    remaining = [s for s in job_store.active_position_states()
                 if s["position_key"].startswith("ZRO/USDT:USDT:")]
    assert [s["position_key"] for s in remaining] == ["ZRO/USDT:USDT:sell:adopted"]
