"""Replacement stops must converge after venue rounding and verified execution."""

from datetime import UTC, datetime

import ccxt
import pytest

from server import job_store
from tests.execution_derivatives_support import VERIFIED_IDENTITY, bind_identity
from tests.test_execution_brackets import _rounded_bracket, _RoundingV3Exchange
from tradingagents.execution.brackets import BracketManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema, monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    return postgres_schema


class _LostVerificationExchange(_RoundingV3Exchange):
    def __init__(self):
        super().__init__()
        self.contracts = 0.5123
        self.lose_verification = True

    def fetch_order(self, order_id, symbol, params=None):
        if order_id == "3" and self.lose_verification:
            self.lose_verification = False
            raise ccxt.NetworkError("replacement resting read lost")
        return super().fetch_order(order_id, symbol, params)


def _replacement(*, filled=True):
    exchange = _LostVerificationExchange()
    manager, bridge, exchange, task_id = _rounded_bracket(exchange)
    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000.123456, quantity=0.51234,
        allow_mutation=True,
    ) == "submission_uncertain"
    if filled:
        exchange._confirm_child_fill("3")
        exchange.contracts = 0.0
    return BracketManager(bridge), bridge, exchange, task_id


def _mutations(exchange, before):
    return [call for call in exchange.calls[before:] if call[0] in {"create", "cancel"}]


def test_tick_rounded_replacement_uses_its_own_trigger_and_quantity():
    exchange = _RoundingV3Exchange()
    manager, _bridge, exchange, task_id = _rounded_bracket(exchange)

    assert manager.replace_leg(
        task_id, "stop-loss", trigger_price=49_000.123456, quantity=0.51234,
        allow_mutation=True,
    ) == "replaced"

    task = job_store.load_bracket_recovery(task_id)
    stop = task["legs"][0]
    assert stop["params"] == {"stopLossPrice": 49_000.1}
    assert stop["quantity"] == pytest.approx(0.5123)
    assert stop["order_id"] == "3"
    assert not stop.get("retired_ids")
    assert [call[1] for call in exchange.calls if call[0] == "cancel"] == ["1"]


def test_filled_replacement_after_restart_cleans_old_stop_and_target_once():
    manager, bridge, exchange, task_id = _replacement()
    before = len(exchange.calls)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"

    task = job_store.load_bracket_recovery(task_id)
    stop = task["legs"][0]
    assert stop["order_id"] == "3"
    assert stop["status"] == "filled"
    assert stop["params"] == {"stopLossPrice": 49_000.1}
    assert stop["quantity"] == pytest.approx(0.5123)
    assert not stop.get("retired_ids")
    assert task["legs"][1]["status"] == "cancelled"
    assert sorted(call[1] for call in _mutations(exchange, before)) == ["1", "2"]
    assert all(call[0] == "cancel" for call in _mutations(exchange, before))
    after = len(exchange.calls)
    assert BracketManager(bridge).reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"
    assert _mutations(exchange, after) == []


@pytest.mark.parametrize("fault", ["missing_child", "partial_child", "wrong_client", "wrong_trigger"])
def test_unverified_replacement_fill_retains_old_protection(fault):
    manager, _bridge, exchange, task_id = _replacement()
    replacement = exchange.orders["3"]
    if fault == "missing_child":
        replacement["info"].pop("actualOrderId")
    elif fault == "partial_child":
        exchange.orders["3-fill"]["info"]["executedQty"] = "0.2"
    elif fault == "wrong_client":
        replacement["info"]["clientAlgoId"] = "other-client"
    else:
        replacement["info"]["triggerPrice"] = "49000.4"
    before = len(exchange.calls)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"

    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == "1"
    expected_price = 49_000.123456 if fault in {"wrong_client", "wrong_trigger"} else 49_000.1
    assert task["legs"][0]["replacement_price"] == expected_price
    assert _mutations(exchange, before) == []


@pytest.mark.parametrize("fault", ["position_still_open", "position_read_failed", "cancellation_gated"])
def test_filled_replacement_cleanup_waits_for_verified_flat_position(fault):
    manager, _bridge, exchange, task_id = _replacement()
    if fault == "position_still_open":
        exchange.contracts = 0.1
    elif fault == "position_read_failed":
        exchange.raw_position_error = ccxt.NetworkError("position evidence lost")
    before = len(exchange.calls)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=fault != "cancellation_gated",
    ) == "reconcile_incomplete"

    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == "3"
    assert task["legs"][0]["status"] == "filled"
    assert task["legs"][0]["retired_ids"] == ["1"]
    assert task["status"] not in {"completed", "reconciled_stop_loss"}
    assert _mutations(exchange, before) == []


def test_rejected_replacement_retains_original_unrounded_intent():
    manager, _bridge, exchange, task_id = _replacement(filled=False)
    exchange.orders["3"]["info"]["algoStatus"] = "REJECTED"
    before = len(exchange.calls)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "monitoring"

    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["order_id"] == "1"
    assert task["legs"][0]["params"] == {"stopLossPrice": 48_000.1}
    assert "replacement_price" not in task["legs"][0]
    assert _mutations(exchange, before) == []


def test_cleanup_retry_rechecks_flat_even_after_retired_stop_was_cancelled():
    manager, bridge, exchange, task_id = _replacement()
    cancel = exchange.cancel_order

    def fail_target(order_id, symbol, params=None):
        if order_id == "2":
            raise ccxt.NetworkError("target cancel response lost")
        return cancel(order_id, symbol, params)

    exchange.cancel_order = fail_target
    assert manager.reconcile(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"
    assert job_store.load_bracket_recovery(task_id)["legs"][0]["retired_ids"] == []
    exchange.cancel_order = cancel
    exchange.contracts = 0.1
    before = len(exchange.calls)

    assert BracketManager(bridge).reconcile(
        task_id, repair_missing=False, allow_sibling_cancel=True,
    ) == "reconcile_incomplete"

    assert _mutations(exchange, before) == []


def test_rejected_target_after_filled_stop_does_not_create_a_new_target():
    manager, _bridge, exchange, task_id = _replacement()
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][1]["status"] = "cancelled"
    exchange.orders["2"]["info"]["algoStatus"] = "REJECTED"
    job_store.update_bracket_recovery(
        task_id, status=task["status"], placed_ids=task["placed_ids"], attempts=0,
        last_error=task["last_error"], now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )
    before = len(exchange.calls)

    assert manager.reconcile(
        task_id, repair_missing=True, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"

    assert _mutations(exchange, before) == [("cancel", "1", "BTC/USDT:USDT", {"trigger": True})]
