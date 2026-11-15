"""Offline derivatives bracket recovery orchestration tests."""

import pytest

from server import job_store
from server.auto_modes import AutoMode
from tests.execution_derivatives_support import (
    FROZEN_NOW,
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _live_bracket_order,
    _recovery_db as _recovery_db,
    bind_identity,
    enable_auto_futures as enable_auto_futures,
    live_config,
    live_env,
    make_executor,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.execution.auto_authorization import mode_update_lock
from tradingagents.execution.bracket_recovery import _valid_open_position
from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.portfolio import PortfolioContext

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("enable_auto_futures")]


@pytest.fixture(autouse=True)
def _restore_entry_gates():
    original = get_config()
    try:
        yield
    finally:
        with mode_update_lock((AutoMode.FUTURES,)):
            set_config({
                key: original.get(key, False)
                for key in ("auto_futures_enabled", "exec_derivatives", "exec_live")
            })

# --- exchange-side brackets on derivatives (reduceOnly required) -----------------


def _failed_bracket_task(tmp_path, monkeypatch, exchange):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    # A fully processed entry still ends "durable intent retained": the intent
    # row carries the venue entry id the bracket task hangs off.
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    intent = next(
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )
    entry_id = intent["placed_ids"][0]
    return executor, order, f"bracket:{order.ccxt_symbol}:{entry_id}", job_store


def _placed_bracket_task(tmp_path, monkeypatch, exchange=None):
    exchange = exchange or FakeDerivExchange()
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.contracts = order.quantity
    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "submission_uncertain"
    assert len(task["placed_ids"]) == 2
    return executor, order, task_id, store, exchange


def _algo_order(order_id, order, *, kind="stop-loss", status="NEW", **info):
    trigger = order.stop_loss if kind == "stop-loss" else order.take_profit
    payload = {
        "id": order_id, "symbol": order.ccxt_symbol, "side": "sell",
        "amount": order.quantity, "type": "market", "status": "open",
        "info": {
            "algoId": order_id, "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": status,
            "orderType": "STOP_MARKET" if kind == "stop-loss" else "TAKE_PROFIT_MARKET",
            "triggerPrice": str(trigger), "quantity": str(order.quantity),
            "reduceOnly": True, "workingType": "MARK_PRICE", **info,
        },
    }
    if status in {"FILLED", "FINISHED"}:
        # A terminal algo status only proves a fill through its child execution.
        payload["info"].setdefault("actualOrderId", f"{order_id}-fill")
        payload["info"].setdefault("actualQty", str(order.quantity))
    return payload


def _register_child_fill(exchange, algo):
    """Make the algo order's child execution independently fetchable."""
    child_id = algo["info"]["actualOrderId"]
    exchange.orders_by_id[child_id] = {
        "id": child_id, "symbol": algo["symbol"], "side": algo["side"],
        "amount": float(algo["info"]["actualQty"]), "type": "market", "status": "closed",
        "info": {
            "orderId": child_id, "symbol": "BTCUSDT", "side": "SELL",
            "status": "FILLED", "executedQty": algo["info"]["actualQty"],
        },
    }


def _close_entry_gates(monkeypatch):
    with mode_update_lock((AutoMode.FUTURES,)):
        set_config({
            "auto_futures_enabled": False,
            "exec_derivatives": False,
            "exec_live": False,
        })
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)


def _open_position(exchange, quantity):
    exchange.contracts = quantity


def test_verified_open_position_accepts_signed_short_quantity():
    short = FuturesPosition(
        symbol="BTC/USDT:USDT", side="short", contracts=1.0,
        quantity=-1.0, entry_price=100.0, mark_price=99.0,
    )
    long = FuturesPosition(
        symbol="BTC/USDT:USDT", side="long", contracts=1.0,
        quantity=1.0, entry_price=100.0, mark_price=101.0,
    )

    assert _valid_open_position(short, "short")
    assert not _valid_open_position(short, "long")
    assert _valid_open_position(long, "long")


def test_closed_entry_gates_allow_verified_open_bracket_protection(
    tmp_path, monkeypatch,
):
    executor, order, task_id, store, exchange = _placed_bracket_task(
        tmp_path, monkeypatch,
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    _open_position(exchange, order.quantity)
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    result = executor.recover_bracket(task_id)

    assert result == "monitoring"
    calls = exchange.calls[calls_before:]
    assert any(call[0] == "fapiPrivateV3GetPositionRisk" for call in calls)
    assert any(call[0] == "fetch_open_orders" for call in calls)
    assert not any(call[0] == "create_order" and call[5] and not (
        "stopLossPrice" in call[5] or "takeProfitPrice" in call[5]
    ) for call in calls)


@pytest.mark.parametrize("method_name", ["recover_bracket", "monitor_bracket"])
def test_recovery_reloads_task_after_lock_symbol_change(tmp_path, monkeypatch, method_name):
    executor, _order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    original = store.load_bracket_recovery
    reads = []

    def load(task):
        current = original(task)
        reads.append(current)
        if len(reads) == 2:
            current["symbol"] = "ETH/USDT:USDT"
        return current

    monkeypatch.setattr(store, "load_bracket_recovery", load)
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    assert getattr(executor, method_name)(task_id) == "bracket_identity_mismatch"
    assert len(reads) >= 2
    assert not [
        call for call in exchange.calls[calls_before:]
        if call[0] in {"create_order", "cancel_order"}
    ]


def test_closed_entry_gates_refuse_unbound_bracket_without_order_mutation(
    tmp_path, monkeypatch,
):
    executor, _order, task_id, store, exchange = _placed_bracket_task(
        tmp_path, monkeypatch,
    )
    bind_identity(monkeypatch, None)
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    result = executor.recover_bracket(task_id)

    assert result == "account_identity_unknown"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {
        "fetch_open_orders", "create_order", "cancel_order",
    }]
    assert store.load_bracket_recovery(task_id)["status"] == "submission_uncertain"


def test_closed_entry_gates_refuse_emergency_bracket_without_order_mutation(
    tmp_path, monkeypatch,
):
    executor, _order, task_id, store, exchange = _placed_bracket_task(
        tmp_path, monkeypatch,
    )
    store.update_bracket_recovery(
        task_id, status="emergency_close_failed", placed_ids=[], attempts=1,
        last_error="manual test state", now=FROZEN_NOW.isoformat(),
    )
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    assert executor.recover_bracket(task_id) == "manual_reconciliation_required"
    assert not [
        call for call in exchange.calls[calls_before:]
        if call[0] in {"fetch_open_orders", "create_order", "cancel_order"}
    ]
    assert store.load_bracket_recovery(task_id)["status"] == "emergency_close_failed"


def test_closed_entry_gates_refuse_when_open_position_evidence_is_unknown(
    tmp_path, monkeypatch,
):
    executor, _order, task_id, _store, exchange = _placed_bracket_task(
        tmp_path, monkeypatch,
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    exchange.contracts = None
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    result = executor.recover_bracket(task_id)

    assert result == "position_unknown"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {
        "fetch_open_orders", "create_order", "cancel_order",
    }]


def test_closed_entry_gates_refuse_missing_position_to_bracket_link(
    tmp_path, monkeypatch,
):
    executor, order, task_id, store, exchange = _placed_bracket_task(
        tmp_path, monkeypatch,
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key = f"{order.ccxt_symbol}:buy:{task_id.rsplit(':', 1)[-1]}"
    state = store.load_position_state(position_key)
    store.update_bracket_recovery(
        task_id, status="submission_uncertain", placed_ids=state.get("placed_ids", []),
        attempts=0, last_error=None, now=FROZEN_NOW.isoformat(),
    )
    state.pop("bracket_task_id")
    assert store.save_position_state(position_key, state["revision"], state)
    _close_entry_gates(monkeypatch)
    calls_before = len(exchange.calls)

    result = executor.recover_bracket(task_id)

    assert result == "missing_position_link"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {
        "fetch_open_orders", "create_order", "cancel_order",
    }]
