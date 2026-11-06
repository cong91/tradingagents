"""A lost PostgreSQL operation lock must fence every bracket mutation."""

from datetime import UTC, datetime

import pytest

from server import job_store
from tests.execution_derivatives_support import VERIFIED_IDENTITY, bind_identity
from tests.test_execution_brackets import _bracket, _Bridge, _LadderExchange
from tradingagents.execution import brackets
from tradingagents.execution.futures_operation_lock import (
    OperationLockLostError,
    operation_lock,
    require_operation_lock,
)
from tradingagents.execution.sizing import PlannedOrder

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema):
    return postgres_schema


@pytest.fixture(autouse=True)
def verified_identity(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)


def _close_guard_before_callback(monkeypatch, operation):
    original = brackets.snapshot_observer.run

    def close_then_call(bridge, client, *, operation, symbol, call):
        if operation == close_then_call.target:
            require_operation_lock(symbol).connection.close()
        return original(bridge, client, operation=operation, symbol=symbol, call=call)

    close_then_call.target = operation
    monkeypatch.setattr(brackets.snapshot_observer, "run", close_then_call)


def _pending_repair(exchange, task_id):
    task = job_store.load_bracket_recovery(task_id)
    leg = task["legs"][1]
    exchange.orders.pop(leg["order_id"])
    leg.update(status="pending", order_id=None)
    job_store.update_bracket_recovery(
        task_id, status="pending", placed_ids=[task["legs"][0]["order_id"]],
        attempts=0, last_error=None, now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )


def _new_mutations(exchange, before):
    return [call for call in exchange.calls[before:] if call[0] in {"create", "cancel"}]


def test_repair_lock_loss_after_position_read_never_emergency_closes():
    class _PositionReadClosesLock(_LadderExchange):
        close_after_read = False

        def fapiPrivateV3GetPositionRisk(self, params=None):
            rows = super().fapiPrivateV3GetPositionRisk(params)
            if self.close_after_read:
                self.close_after_read = False
                require_operation_lock("BTC/USDT:USDT").connection.close()
            return rows

    exchange = _PositionReadClosesLock(0.005)
    manager, _bridge, exchange, task_id = _bracket(exchange)
    _pending_repair(exchange, task_id)
    exchange.close_after_read = True
    before = len(exchange.calls)

    with pytest.raises(OperationLockLostError):
        manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    assert _new_mutations(exchange, before) == []


def test_repair_callback_lock_loss_does_not_fall_through_to_emergency(monkeypatch):
    exchange = _LadderExchange(0.005)
    manager, _bridge, exchange, task_id = _bracket(exchange)
    _pending_repair(exchange, task_id)
    _close_guard_before_callback(monkeypatch, "repair_bracket_leg")
    before = len(exchange.calls)

    with pytest.raises(OperationLockLostError):
        manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    assert _new_mutations(exchange, before) == []


def test_replace_callback_rechecks_lock_after_order_read(monkeypatch):
    manager, _bridge, exchange, task_id = _bracket(_LadderExchange(0.005))
    _close_guard_before_callback(monkeypatch, "replace_bracket_leg")
    before = len(exchange.calls)

    with pytest.raises(OperationLockLostError):
        manager.replace_leg(
            task_id, "stop-loss", trigger_price=49_000, quantity=0.005,
            allow_mutation=True,
        )

    assert _new_mutations(exchange, before) == []


def test_initial_submit_rechecks_lock_before_create(monkeypatch):
    exchange = _LadderExchange(0.005)
    bridge = _Bridge(exchange)
    manager = brackets.BracketManager(bridge)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", market="derivatives",
        signal="Buy", side="buy", quantity=0.005, estimated_price=50_000,
        cost=250, stop_loss=48_000, take_profit=55_000,
    )
    _close_guard_before_callback(monkeypatch, "place_bracket_leg")
    before = len(exchange.calls)

    with pytest.raises(OperationLockLostError):
        manager.place(order, True, "entry-lock-loss")

    assert _new_mutations(exchange, before) == []


def test_time_stop_callback_rechecks_lock_after_position_read(monkeypatch):
    manager, _bridge, exchange, task_id = _bracket(_LadderExchange(0.005))
    _close_guard_before_callback(monkeypatch, "time_stop_close")
    before = len(exchange.calls)

    with pytest.raises(OperationLockLostError):
        manager.close_position(task_id, reason="time_stop", allow_mutation=True)

    assert _new_mutations(exchange, before) == []


def test_emergency_callback_rechecks_lock_before_create(monkeypatch):
    manager, _bridge, exchange, task_id = _bracket(_LadderExchange(0.005))
    task = job_store.load_bracket_recovery(task_id)
    _close_guard_before_callback(monkeypatch, "emergency_close")
    before = len(exchange.calls)

    with operation_lock(task["symbol"]), pytest.raises(OperationLockLostError):
        manager._emergency_close(exchange, task, RuntimeError("venue rejected repair"))

    assert _new_mutations(exchange, before) == []


@pytest.mark.parametrize("cleanup", ["siblings", "all", "retired", "resting"])
def test_cleanup_callbacks_recheck_lock_before_cancel(monkeypatch, cleanup):
    manager, _bridge, exchange, task_id = _bracket(_LadderExchange(0.005))
    task = job_store.load_bracket_recovery(task_id)
    if cleanup == "retired":
        task["legs"][0]["retired_ids"] = ["retired-stop"]
        exchange.orders["retired-stop"] = {
            "id": "retired-stop", "symbol": "BTC/USDT:USDT", "side": "sell",
            "amount": 0.005, "type": "market", "status": "open",
            "info": {"algoId": "retired-stop", "symbol": "BTCUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "STOP_MARKET", "triggerPrice": "48000",
                     "quantity": "0.005", "reduceOnly": True, "workingType": "MARK_PRICE"},
        }
        job_store.update_bracket_recovery(
            task_id, status=task["status"], placed_ids=task["placed_ids"], attempts=task["attempts"],
            last_error=None, now=task["updated_at"], legs=task["legs"],
        )
        manager._load_task(task_id)
        task = job_store.load_bracket_recovery(task_id)
    operation = "cancel_retired_bracket_leg" if cleanup == "retired" else "cancel_bracket_leg"
    _close_guard_before_callback(monkeypatch, operation)
    before = len(exchange.calls)

    with operation_lock(task["symbol"]), pytest.raises(OperationLockLostError):
        if cleanup == "siblings":
            task["legs"][0]["status"] = "filled"
            manager._cleanup(exchange, task, task["legs"][0], True)
        elif cleanup == "all":
            manager._cancel_all_for_verified_account(task_id, allow_mutation=True)
        elif cleanup == "retired":
            manager._cleanup_retired(exchange, task, True)
        else:
            manager._cancel_resting_legs(exchange, task, allow_cancel=True)

    assert _new_mutations(exchange, before) == []
