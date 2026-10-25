"""Offline tests for converting a single-target position into a ladder."""

import pytest

from server import job_store
from tradingagents.execution.scale_out_apply import convert_to_scale_out


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, postgres_schema):
    return postgres_schema
class _Exchange:
    def __init__(self):
        self.calls = []
        self.next_id = 500
        self.create_error = None

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", side, amount, params))
        if self.create_error is not None:
            raise self.create_error
        self.next_id += 1
        return {"id": str(self.next_id)}

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id))
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


def _position(mark=0.61):
    key = "TIA/USDT:USDT:sell:adopted"
    job_store.save_position_state(key, 0, {
        "snapshot": {
            "position_key": key, "side": "short", "entry": 0.6623,
            "initial_stop": 0.7563, "contracts": 6108.0, "quantity": 6108.0,
            "stop": 0.7563, "mark": mark,
        },
        "orders": [],
        "entry_order_id": "adopted",
        "bracket_task_id": "bracket:TIA/USDT:USDT:adopted",
    })
    job_store.create_bracket_recovery(
        "bracket:TIA/USDT:USDT:adopted", symbol="TIA/USDT:USDT", entry_order_id="adopted",
        close_side="buy", quantity=6108.0,
        legs=[
            {"name": "stop-loss", "params": {"stopLossPrice": 0.7563}, "status": "placed", "order_id": "old-stop"},
            {"name": "take-profit", "params": {"takeProfitPrice": 0.528}, "status": "placed", "order_id": "old-tp"},
        ],
        now="2026-10-20T00:00:00+00:00", venue_key="binance|https://demo-api.binance.com|true",
    )
    return key


def test_conversion_places_two_tranches_then_cancels_the_old_target():
    key = _position()
    exchange = _Exchange()

    report = convert_to_scale_out(_Bridge(exchange), position_key=key, allow_mutation=True, config={})

    assert report["status"] == "converted"
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("create") < kinds.index("cancel")
    creates = [call for call in exchange.calls if call[0] == "create"]
    assert len(creates) == 2
    assert [call[2] for call in creates] == pytest.approx([6108 * 0.33, 6108 * 0.33])
    assert [call[3]["workingType"] for call in creates] == ["MARK_PRICE", "MARK_PRICE"]
    assert report["runner_quantity"] == pytest.approx(6108 * 0.34)
    assert report["cancelled"] == ["old-tp"]

    task = job_store.load_bracket_recovery("bracket:TIA/USDT:USDT:adopted")
    names = [leg["name"] for leg in task["legs"]]
    assert names == ["stop-loss", "take-profit-1", "take-profit-2"]
    assert task["status"] == "monitoring"


def test_tranche_behind_the_mark_is_refused_before_any_order():
    key = _position(mark=0.45)  # already below the first tranche price
    exchange = _Exchange()

    report = convert_to_scale_out(_Bridge(exchange), position_key=key, allow_mutation=True, config={})

    assert report["status"] == "tranche_already_crossed"
    assert not exchange.calls


def test_placement_failure_keeps_the_old_target():
    key = _position()
    exchange = _Exchange()
    exchange.create_error = RuntimeError("venue rejected")

    report = convert_to_scale_out(_Bridge(exchange), position_key=key, allow_mutation=True, config={})

    assert report["status"] == "placement_failed"
    assert not [call for call in exchange.calls if call[0] == "cancel"]
    task = job_store.load_bracket_recovery("bracket:TIA/USDT:USDT:adopted")
    assert [leg["name"] for leg in task["legs"]] == ["stop-loss", "take-profit"]


def test_already_laddered_position_is_left_alone():
    key = _position()
    task = job_store.load_bracket_recovery("bracket:TIA/USDT:USDT:adopted")
    task["legs"].append(
        {"name": "take-profit-2", "params": {"takeProfitPrice": 0.47}, "status": "placed", "order_id": "tp2"}
    )
    job_store.update_bracket_recovery(
        "bracket:TIA/USDT:USDT:adopted", status="monitoring", placed_ids=[], attempts=0,
        last_error=None, now="2026-10-20T00:00:00+00:00", legs=task["legs"],
    )
    exchange = _Exchange()

    report = convert_to_scale_out(_Bridge(exchange), position_key=key, allow_mutation=True, config={})

    assert report["status"] == "already_scale_out"
    assert not exchange.calls
