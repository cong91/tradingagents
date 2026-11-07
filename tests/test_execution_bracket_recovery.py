"""Offline derivatives bracket placement, recovery, and monitoring tests."""

import ccxt
import pytest

from server import job_store
from tests.execution_derivatives_support import (
    FROZEN_NOW,
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _live_bracket_order,
    _recovery_db as _recovery_db,
    bind_identity,
    bracket_kwargs,
    live_config,
    live_env,
    make_executor,
    read_audit,
)
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

# --- exchange-side brackets on derivatives (reduceOnly required) -----------------


def _failed_bracket_task(tmp_path, monkeypatch, exchange):
    from server import job_store

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


def test_cancel_binance_algo_success_response_is_terminal(tmp_path, monkeypatch):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    stop_id, take_id = store.load_bracket_recovery(task_id)["placed_ids"]
    exchange.open_orders = [_algo_order(stop_id, order)]
    finished = _algo_order(take_id, order, kind="take-profit", status="FINISHED")
    exchange.orders_by_id[take_id] = finished
    _register_child_fill(exchange, finished)
    exchange.cancel_response = {"info": {"algoId": stop_id, "code": "200", "msg": "success"}}
    intent = next(task for task in store.active_bracket_recoveries() if task["task_id"].startswith("entry-intent:"))
    store.update_bracket_recovery(
        intent["task_id"], status="completed", placed_ids=intent["placed_ids"], attempts=0,
        last_error=None, now=FROZEN_NOW.isoformat(),
    )

    assert executor.recover_bracket(task_id) == "reconciled_take_profit"
    calls_before = len(exchange.calls)
    assert executor.monitor_bracket(task_id) == "reconciled_take_profit"
    assert len(exchange.calls) == calls_before


@pytest.mark.parametrize("changes", [
    {"symbol": "ETH/USDT:USDT", "info": {"symbol": "ETHUSDT"}},
    {"amount": 500, "info": {"quantity": "500"}},
    {"info": {"reduceOnly": False}},
    {"info": {"orderType": "TAKE_PROFIT_MARKET"}},
])
def test_pending_recovery_does_not_adopt_unrelated_algo(tmp_path, monkeypatch, changes):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.contracts = order.quantity  # the repair path requires a readable venue
    unrelated = _algo_order("unrelated-stop", order)
    unrelated["info"].update(changes.get("info", {}))
    unrelated.update({key: value for key, value in changes.items() if key != "info"})
    exchange.open_orders = [unrelated]

    assert executor.recover_bracket(task_id) == "monitoring"
    assert "unrelated-stop" not in store.load_bracket_recovery(task_id)["placed_ids"]


def test_recorded_algo_id_is_not_replaced_by_same_trigger_order(tmp_path, monkeypatch):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    stop_id, take_id = store.load_bracket_recovery(task_id)["placed_ids"]
    exchange.open_orders = [_algo_order("other-bracket-stop", order), _algo_order(stop_id, order)]
    finished = _algo_order(take_id, order, kind="take-profit", status="FINISHED")
    exchange.orders_by_id[take_id] = finished
    _register_child_fill(exchange, finished)
    intent = next(task for task in store.active_bracket_recoveries() if task["task_id"].startswith("entry-intent:"))
    store.update_bracket_recovery(
        intent["task_id"], status="completed", placed_ids=intent["placed_ids"], attempts=0,
        last_error=None, now=FROZEN_NOW.isoformat(),
    )

    assert executor.recover_bracket(task_id) == "reconciled_take_profit"
    assert [call[1] for call in exchange.calls if call[0] == "cancel_order"] == [stop_id]


def test_leg_inspection_failure_blocks_pending_repair(tmp_path, monkeypatch):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    task = store.load_bracket_recovery(task_id)
    stop_id = task["legs"][0]["order_id"]
    task["legs"][1].update(status="pending", order_id=None)
    store.update_bracket_recovery(
        task_id, status="pending", placed_ids=[stop_id], attempts=0,
        last_error=None, now=FROZEN_NOW.isoformat(), legs=task["legs"],
    )
    exchange.open_orders = []
    exchange.orders_by_id = {}
    calls_before = len(exchange.calls)

    assert executor.recover_bracket(task_id) == "reconcile_incomplete"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create_order", "cancel_order"}]


@pytest.mark.parametrize("status", ["TRIGGERING", "TRIGGERED"])
def test_algo_trigger_without_execution_confirmation_keeps_sibling(tmp_path, monkeypatch, status):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    stop_id, take_id = store.load_bracket_recovery(task_id)["placed_ids"]
    exchange.open_orders = [_algo_order(stop_id, order)]
    exchange.orders_by_id[take_id] = _algo_order(take_id, order, kind="take-profit", status=status)
    calls_before = len(exchange.calls)

    assert executor.recover_bracket(task_id) in {"monitoring", "reconcile_incomplete"}
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create_order", "cancel_order"}]


def test_partial_bracket_failure_persists_each_leg(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    _, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)

    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "pending"
    assert [leg["status"] for leg in task["legs"]] == ["pending", "pending"]
    assert task["attempts"] == 0

    position_key = f"{order.ccxt_symbol}:{order.side}:{task_id.rsplit(':', 1)[-1]}"
    position = store.load_position_state(position_key)
    assert position["entry_order_id"] == task_id.rsplit(":", 1)[-1]
    assert position["snapshot"]["side"] == "long"
    assert position["snapshot"]["entry"] == order.estimated_price
    assert position["snapshot"]["stop"] == order.stop_loss
    assert position["snapshot"]["targets"] == [order.take_profit]


def test_bracket_recovery_retries_after_restart(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.contracts = order.quantity  # the repair path requires a readable venue
    reloaded_executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )

    assert store.load_bracket_recovery(task_id)["status"] == "pending"
    assert reloaded_executor.recover_bracket(task_id) == "monitoring"
    assert store.load_bracket_recovery(task_id)["status"] == "monitoring"
    send_count = len([call for call in exchange.calls if call[0] == "create_order"])
    assert reloaded_executor.recover_bracket(task_id) == "monitoring"
    assert len([call for call in exchange.calls if call[0] == "create_order"]) == send_count
    assert executor is not None


def test_recovery_reconciles_existing_leg_without_duplicate(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.contracts = order.quantity  # the repair path requires a readable venue
    task = store.load_bracket_recovery(task_id)
    task["legs"][0]["status"] = "uncertain"
    client_id = task["legs"][0]["client_order_id"]
    store.update_bracket_recovery(
        task_id, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error="simulated lost ACK", now=FROZEN_NOW.isoformat(), legs=task["legs"],
    )
    exchange.open_orders = [{
        "id": "existing-stop", "symbol": order.ccxt_symbol, "side": "sell",
        "amount": order.quantity, "type": "market", "status": "open",
        "info": {
            "algoId": "existing-stop", "clientAlgoId": client_id, "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": "NEW", "orderType": "STOP_MARKET",
            "triggerPrice": "48000", "quantity": str(order.quantity),
            "reduceOnly": True, "workingType": "MARK_PRICE",
        },
    }]
    exchange.orders_by_id["existing-stop"] = exchange.open_orders[0]

    assert executor.recover_bracket(task_id) == "monitoring"
    recovered = [call for call in exchange.calls if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert [call[5]["takeProfitPrice"] for call in recovered[-1:]] == [55_000.0]
    assert recovered[-1][5]["reduceOnly"] is True
    assert recovered[-1][5]["workingType"] == "MARK_PRICE"
    assert recovered[-1][5]["clientOrderId"].startswith("br-")
    assert ("fetch_open_orders", order.ccxt_symbol, None, None, {"trigger": True}) in exchange.calls
    task = store.load_bracket_recovery(task_id)
    assert "existing-stop" in task["placed_ids"]


def test_recovery_cancels_open_sibling_after_algo_leg_finishes(tmp_path, monkeypatch):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    task = store.load_bracket_recovery(task_id)
    stop_id, take_id = task["placed_ids"]
    exchange.open_orders = [{
        "id": stop_id, "symbol": order.ccxt_symbol, "side": "sell",
        "amount": order.quantity, "type": "market", "status": "open",
        "info": {
            "algoId": stop_id, "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": "NEW", "orderType": "STOP_MARKET",
            "triggerPrice": "48000", "quantity": str(order.quantity),
            "reduceOnly": True, "workingType": "MARK_PRICE",
        },
    }]
    exchange.orders_by_id = {
        stop_id: exchange.open_orders[0],
        take_id: {
            "id": take_id, "symbol": order.ccxt_symbol, "side": "sell",
            "amount": order.quantity, "type": "market", "status": "closed",
            "info": {
                "algoId": take_id, "symbol": "BTCUSDT", "side": "SELL",
                "algoStatus": "FINISHED", "orderType": "TAKE_PROFIT_MARKET",
                "triggerPrice": "55000", "quantity": str(order.quantity),
                "actualOrderId": "venue-execution-id", "actualQty": str(order.quantity),
                "reduceOnly": True, "workingType": "MARK_PRICE",
            },
        },
        "venue-execution-id": {
            "id": "venue-execution-id", "symbol": order.ccxt_symbol, "side": "sell",
            "amount": order.quantity, "type": "market", "status": "closed",
            "info": {
                "orderId": "venue-execution-id", "symbol": "BTCUSDT", "side": "SELL",
                "status": "FILLED", "executedQty": str(order.quantity),
            },
        },
    }

    intent = next(task for task in store.active_bracket_recoveries() if task["task_id"].startswith("entry-intent:"))
    store.update_bracket_recovery(
        intent["task_id"], status="completed", placed_ids=intent["placed_ids"], attempts=0,
        last_error=None, now=FROZEN_NOW.isoformat(),
    )
    assert executor.recover_bracket(task_id) == "reconciled_take_profit"
    assert ("cancel_order", stop_id, order.ccxt_symbol, {"trigger": True}) in exchange.calls
    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "reconciled_take_profit"
    assert task["legs"][0]["status"] == "cancelled"
    assert task["legs"][1]["status"] == "filled"


def test_monitor_is_read_only_when_live_gate_is_closed(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor, order, task_id, store, _ = _placed_bracket_task(tmp_path, monkeypatch, exchange)
    task = store.load_bracket_recovery(task_id)
    stop_id, take_id = task["placed_ids"]
    exchange.open_orders = []
    exchange.orders_by_id = {
        stop_id: {
            "id": stop_id, "status": "open", "amount": order.quantity,
            "info": {"algoId": stop_id, "symbol": "BTCUSDT", "algoStatus": "NEW",
                     "orderType": "STOP_MARKET", "triggerPrice": "48000",
                     "quantity": str(order.quantity), "side": "SELL",
                     "reduceOnly": True, "workingType": "MARK_PRICE"},
        },
        take_id: {
            "id": take_id, "status": "closed",
            "info": {"algoId": take_id, "symbol": "BTCUSDT", "algoStatus": "FINISHED",
                     "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "55000",
                     "quantity": str(order.quantity), "side": "SELL",
                     "reduceOnly": True, "workingType": "MARK_PRICE",
                     "actualOrderId": f"{take_id}-fill", "actualQty": str(order.quantity)},
        },
        f"{take_id}-fill": {
            "id": f"{take_id}-fill", "symbol": order.ccxt_symbol, "side": "sell",
            "amount": order.quantity, "type": "market", "status": "closed",
            "info": {"orderId": f"{take_id}-fill", "symbol": "BTCUSDT", "side": "SELL",
                     "status": "FILLED", "executedQty": str(order.quantity)},
        },
    }
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)
    intent = next(task for task in store.active_bracket_recoveries() if task["task_id"].startswith("entry-intent:"))
    store.update_bracket_recovery(
        intent["task_id"], status="completed", placed_ids=intent["placed_ids"], attempts=0,
        last_error=None, now=FROZEN_NOW.isoformat(),
    )
    calls_before = len(exchange.calls)

    assert executor.monitor_bracket(task_id) == "reconcile_incomplete"
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create_order", "cancel_order"}]
    current = store.load_bracket_recovery(task_id)
    assert current["legs"][1]["status"] == "filled"
    assert current["legs"][0]["status"] == "placed"
    assert current["status"] == "monitoring"


def test_monitor_inspection_error_does_not_send_orders(tmp_path, monkeypatch):
    executor, _, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    exchange.fetch_open_orders_error = ccxt.NetworkError("offline")
    calls_before = len(exchange.calls)

    assert executor.monitor_bracket(task_id) == "inspection_failed"
    assert len(exchange.calls) == calls_before + 1
    assert not [call for call in exchange.calls[calls_before:] if call[0] in {"create_order", "cancel_order"}]
    assert store.load_bracket_recovery(task_id)["status"] == "submission_uncertain"


def test_recovery_does_not_cancel_sibling_when_leg_was_user_canceled(tmp_path, monkeypatch):
    executor, order, task_id, store, exchange = _placed_bracket_task(tmp_path, monkeypatch)
    task = store.load_bracket_recovery(task_id)
    stop_id, take_id = task["placed_ids"]
    exchange.open_orders = []
    exchange.orders_by_id = {
        stop_id: {
            "id": stop_id, "symbol": order.ccxt_symbol, "side": "sell",
            "amount": order.quantity, "type": "market", "status": "canceled",
            "info": {
                "algoId": stop_id, "symbol": "BTCUSDT", "side": "SELL",
                "algoStatus": "CANCELED", "orderType": "STOP_MARKET",
                "triggerPrice": "48000", "quantity": str(order.quantity),
                "reduceOnly": True, "workingType": "MARK_PRICE",
            },
        },
        take_id: {
            "id": take_id, "symbol": order.ccxt_symbol, "side": "sell",
            "amount": order.quantity, "type": "market", "status": "open",
            "info": {
                "algoId": take_id, "symbol": "BTCUSDT", "side": "SELL",
                "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                "triggerPrice": "55000", "quantity": str(order.quantity),
                "reduceOnly": True, "workingType": "MARK_PRICE",
            },
        },
    }

    assert executor.monitor_bracket(task_id) == "monitoring"
    assert not [call for call in exchange.calls if call[0] == "cancel_order"]
    task = store.load_bracket_recovery(task_id)
    assert task["legs"][0]["status"] == "cancelled"
    assert task["legs"][1]["status"] == "placed"


def test_completed_bracket_recovery_is_idempotent(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.contracts = order.quantity  # the repair path requires a readable venue
    assert executor.recover_bracket(task_id) == "monitoring"
    current = store.load_bracket_recovery(task_id)
    assert current["status"] == "monitoring"
    assert [leg["status"] for leg in current["legs"]] == ["placed", "placed"]
    send_count = len([call for call in exchange.calls if call[0] == "create_order"])

    assert executor.recover_bracket(task_id) == "monitoring"
    assert len([call for call in exchange.calls if call[0] == "create_order"]) == send_count


def test_recovery_does_not_report_emergency_close_when_order_fails(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.bracket_error = ccxt.ExchangeError("protective leg still unavailable")
    exchange.emergency_error = ccxt.ExchangeError("emergency close refused")
    exchange.contracts = order.quantity  # the venue still holds the full entry

    assert executor.recover_bracket(task_id) == "emergency_close_failed"
    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "emergency_close_failed"
    assert "emergency close failed" in task["last_error"]
    assert not any(event["action"] == "emergency_closed" for event in read_audit(tmp_path))


def test_repeated_recovery_failure_emergency_closes_reduce_only(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.bracket_error = ccxt.ExchangeError("protective leg still unavailable")
    exchange.contracts = order.quantity  # the venue still holds the full entry

    assert executor.recover_bracket(task_id) == "emergency_closed"
    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "emergency_closed"
    emergency = [call for call in exchange.calls if call[0] == "create_order"][-1]
    assert emergency[5]["reduceOnly"] is True
    assert emergency[5]["clientOrderId"].startswith("emergency-")


def test_live_bracket_long_closes_sell_with_reduce_only(tmp_path, monkeypatch):
    """A bracketed LONG entry is followed by TWO reduceOnly conditional legs
    (stop below + target above) routed through the venue's conditional-order
    surface via the unified flat stopLossPrice/takeProfitPrice params."""
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    bracket_calls = [call for call in exchange.calls
                     if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert [call[:5] for call in bracket_calls] == [
        ("create_order", "BTC/USDT:USDT", "market", "sell", order.quantity),
        ("create_order", "BTC/USDT:USDT", "market", "sell", order.quantity),
    ]
    assert [call[5]["stopLossPrice"] for call in bracket_calls if "stopLossPrice" in call[5]] == [48_000.0]
    assert [call[5]["takeProfitPrice"] for call in bracket_calls if "takeProfitPrice" in call[5]] == [55_000.0]
    assert all(call[5]["reduceOnly"] is True and call[5]["workingType"] == "MARK_PRICE" for call in bracket_calls)
    client_ids = [call[5]["clientOrderId"] for call in bracket_calls]
    assert len(set(client_ids)) == 2 and all(value.startswith("br-") for value in client_ids)
    actions = [event["action"] for event in read_audit(tmp_path)]
    assert "bracket_verification_required" in actions


def test_live_bracket_short_closes_buy_with_reduce_only(tmp_path, monkeypatch):
    """A SELL entry (a short opener) gets reduceOnly BUY conditional legs --
    the mirrored close side the spot path never needs: stop ABOVE entry,
    target BELOW."""
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Sell", book, price=50_000.0,
                                stop_loss=52_000.0, take_profit=45_000.0,
                                futures_execution_intent="enter")
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    bracket_calls = [call for call in exchange.calls
                     if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert [call[:5] for call in bracket_calls] == [
        ("create_order", "BTC/USDT:USDT", "market", "buy", order.quantity),
        ("create_order", "BTC/USDT:USDT", "market", "buy", order.quantity),
    ]
    assert [call[5]["stopLossPrice"] for call in bracket_calls if "stopLossPrice" in call[5]] == [52_000.0]
    assert [call[5]["takeProfitPrice"] for call in bracket_calls if "takeProfitPrice" in call[5]] == [45_000.0]
    assert all(call[5]["reduceOnly"] is True and call[5]["workingType"] == "MARK_PRICE" for call in bracket_calls)
    client_ids = [call[5]["clientOrderId"] for call in bracket_calls]
    assert len(set(client_ids)) == 2 and all(value.startswith("br-") for value in client_ids)


def test_live_entry_without_bracket_is_refused_before_any_order(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        env=live_env(),
    )
    order = executor.plan_order("BTC-USD", "Buy", PortfolioContext(cash=1_000.0),
                                price=50_000.0,
                                futures_execution_intent="enter")
    assert order.side is None
    assert "explicit stop_loss" in order.reason



def test_bracket_failure_after_fill_is_audited_not_raised(tmp_path, monkeypatch):
    """The entry already filled: a bracket exchange error must never discard
    the fill -- audited as bracket_failed, the fill retained durably."""
    exchange = FakeDerivExchange(bracket_error=ccxt.ExchangeError("stop price invalid"))
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "bracket_failed"
    assert "stop price invalid" in event["reason"]
    intent = next(
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )
    assert intent["placed_ids"] and intent["placed_ids"][0].startswith("deriv-")


def test_live_entry_with_scale_out_places_two_tranches_and_a_runner(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_scale_out_enabled=True,
        ),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)

    bracket_calls = [call for call in exchange.calls if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    quantities = [call[4] for call in bracket_calls]
    assert len(bracket_calls) == 3
    assert quantities[0] == order.quantity  # the stop covers the whole position
    assert quantities[1] == pytest.approx(order.quantity * 0.33)
    assert quantities[2] == pytest.approx(order.quantity * 0.33)
    assert quantities[1] + quantities[2] < order.quantity
    assert all(call[5]["workingType"] == "MARK_PRICE" for call in bracket_calls)
    entry_task = next(
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )
    entry_id = entry_task["legs"][0]["order_id"]
    state = job_store.load_position_state(f"{order.ccxt_symbol}:{order.side}:{entry_id}")
    assert state["snapshot"]["targets"] == [
        float(exchange.price_to_precision(order.ccxt_symbol, call[5]["takeProfitPrice"]))
        for call in bracket_calls if "takeProfitPrice" in call[5]
    ]


def test_scale_out_disabled_keeps_the_single_target(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(_live_bracket_order(executor, book), confirm=True, portfolio=book)

    bracket_calls = [call for call in exchange.calls if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert len(bracket_calls) == 2
    assert bracket_calls[0][4] == bracket_calls[1][4]  # one stop + one target, both full size

def test_invalid_scale_out_fractions_fall_back_to_the_single_bracket(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_scale_out_enabled=True,
            risk_derivatives_scale_out_first_fraction=0.5,
            risk_derivatives_scale_out_second_fraction=0.5,
        ),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(_live_bracket_order(executor, book), confirm=True, portfolio=book)

    bracket_calls = [call for call in exchange.calls if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert len(bracket_calls) == 2  # one stop + one target, entry still protected
    assert any(event["action"] == "scale_out_refused" for event in read_audit(tmp_path))

def test_unknown_entry_fill_keeps_intent_and_blocks_retry(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(entry_response={"id": "unknown-fill"})
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    with pytest.raises(RuntimeError, match="fill is unknown"):
        executor._execute_live(order, True)
    intents = [task for task in job_store.active_bracket_recoveries() if task["task_id"].startswith("entry-intent:")]
    assert len(intents) == 1
    assert intents[0]["legs"][0]["order_id"] == "unknown-fill"
    assert intents[0]["legs"][0]["client_order_id"].startswith("entry-")
    with pytest.raises(PermissionError, match="existing active task"):
        executor._execute_live(order, True)
    assert len([call for call in exchange.calls if call[0] == "create_order"]) == 1


def test_partial_entry_submits_protection_for_confirmed_fill_and_fences_remainder(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(entry_response={
        "id": "partial-entry", "status": "open", "filled": 0.002, "average": 50_100.0,
    })
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    with pytest.raises(RuntimeError, match="partial or remaining quantity unknown"):
        executor._execute_live(order, True)
    task = job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:partial-entry")
    assert task["quantity"] == pytest.approx(0.002)
    intents = [item for item in job_store.active_bracket_recoveries() if item["task_id"].startswith("entry-intent:")]
    assert len(intents) == 1
    assert intents[0]["legs"][0]["order_id"] == "partial-entry"
    protective = [call for call in exchange.calls if call[0] == "create_order" and call[5] and call[5].get("reduceOnly")]
    assert [call[4] for call in protective] == [0.002, 0.002]


def test_live_entry_uses_confirmed_fill_for_initial_state_and_bracket(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(entry_response={
        "id": "fill-1", "filled": 0.004, "average": 50_125.0,
    })
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)

    with pytest.raises(RuntimeError, match="partial or remaining quantity unknown"):
        executor._execute_live(order, True)

    position_key = f"{order.ccxt_symbol}:{order.side}:fill-1"
    state = job_store.load_position_state(position_key)
    assert state["snapshot"]["entry"] == pytest.approx(50_125.0)
    assert state["snapshot"]["quantity"] == pytest.approx(0.004)
    assert state["snapshot"]["stop"] == order.stop_loss
    task = job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:fill-1")
    assert task["quantity"] == pytest.approx(0.004)
    bracket_calls = [call for call in exchange.calls if call[0] == "create_order" and call[5] and ("stopLossPrice" in call[5] or "takeProfitPrice" in call[5])]
    assert [call[4] for call in bracket_calls] == [0.004, 0.004]
    active = job_store.active_bracket_recoveries()
    assert any(task["task_id"].startswith("entry-intent:") for task in active)
    # The protective legs resting on the venue fence a retry through the
    # public gate before anything else is sent.
    with pytest.raises(PermissionError, match="fresh futures account or open orders unavailable"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert len([call for call in exchange.calls if call[0] == "create_order" and call[3] == "buy"]) == 1

    job_store.update_bracket_recovery(
        next(task["task_id"] for task in active if task["task_id"].startswith("entry-intent:")),
        status="cancelled", placed_ids=[], attempts=0, last_error=None, now=FROZEN_NOW.isoformat(),
    )
    job_store.update_bracket_recovery(
        f"bracket:{order.ccxt_symbol}:fill-1", status="cancelled", placed_ids=[],
        attempts=0, last_error=None, now=FROZEN_NOW.isoformat(),
    )


def test_live_entry_pins_explicit_leverage_one(tmp_path, monkeypatch):
    # 1x is still sent explicitly: without it the venue's per-symbol preset
    # would decide the real leverage.
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert ("set_leverage", 1, "BTC/USDT:USDT") in exchange.calls
