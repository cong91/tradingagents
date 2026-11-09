"""Concurrency and durable intent checks for live futures entries."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import job_store
from tests.execution_derivatives_support import (
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
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


@pytest.fixture
def entry_setup(tmp_path, monkeypatch, enable_auto_futures):
    exchange = FakeDerivExchange()
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    order = _live_bracket_order(executor, PortfolioContext(cash=1_000.0))
    return exchange, executor, order


def _entry_intent():
    return next(
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )


def _run_entry(executor, order):
    try:
        executor._execute_live(order, True)
    except Exception as exc:
        return exc
    pytest.fail("entry unexpectedly succeeded without bracket verification")


def test_simultaneous_entries_are_fenced_during_send(entry_setup, monkeypatch):
    exchange, executor, order = entry_setup
    sending = threading.Event()
    release = threading.Event()
    original = exchange.create_order

    def block_first(symbol, order_type, side, amount, price=None, params=None):
        if params and params.get("clientOrderId", "").startswith("entry-"):
            sending.set()
            assert release.wait(10)
        return original(symbol, order_type, side, amount, price, params)

    monkeypatch.setattr(exchange, "create_order", block_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_run_entry, executor, order)
        try:
            assert sending.wait(10)
            second = pool.submit(_run_entry, executor, order).result(timeout=10)
            assert "lock" in str(second).lower()
            assert len([task for task in job_store.active_bracket_recoveries()
                        if task["task_id"].startswith("entry-intent:")]) == 1
        finally:
            release.set()
        assert isinstance(first.result(timeout=10), RuntimeError)
    entries = [call for call in exchange.calls if call[0] == "create_order"
               and call[5] and call[5].get("clientOrderId", "").startswith("entry-")]
    assert len(entries) == 1


def test_entry_journal_captures_actual_submitted_terms_before_send(entry_setup, monkeypatch):
    exchange, executor, order = entry_setup
    original = exchange.create_order
    observed = []

    def inspect_intent(symbol, order_type, side, amount, price=None, params=None):
        if params and params.get("clientOrderId", "").startswith("entry-"):
            task = _entry_intent()
            leg = task["legs"][0]
            observed.append(leg)
            assert task["venue_key"] == leg["venue_key"]
            assert leg["profile"] == VERIFIED_IDENTITY.profile
            assert leg["symbol"] == symbol
            assert leg["side"] == side
            assert leg["type"] == order_type
            assert leg["quantity"] == amount
            assert leg["price"] is None
            assert leg["params"] == params
            assert leg["status"] == "uncertain"
        return original(symbol, order_type, side, amount, price, params)

    monkeypatch.setattr(exchange, "create_order", inspect_intent)
    _run_entry(executor, order)
    assert len(observed) == 1
    assert _entry_intent()["legs"][0]["quantity"] == observed[0]["quantity"]


def test_connection_loss_during_entry_keeps_intent_and_fences_exits(entry_setup, monkeypatch):
    from tradingagents.execution.futures_operation_lock import require_operation_lock

    exchange, executor, order = entry_setup
    original = exchange.create_order

    def lose_lock(symbol, order_type, side, amount, price=None, params=None):
        result = original(symbol, order_type, side, amount, price, params)
        if params and params.get("clientOrderId", "").startswith("entry-"):
            require_operation_lock(symbol).connection.close()
        return result

    monkeypatch.setattr(exchange, "create_order", lose_lock)
    error = _run_entry(executor, order)
    assert "lock" in str(error).lower()
    assert _entry_intent()["legs"][0]["status"] == "uncertain"
    assert not [call for call in exchange.calls if call[0] == "create_order"
                and call[5] and call[5].get("reduceOnly")]


def test_operation_lock_reuses_guard_in_thread_and_fences_other_connection(postgres_schema):
    from tradingagents.execution.futures_operation_lock import operation_lock

    with operation_lock("BTC/USDT:USDT") as first:
        with operation_lock("BTC/USDT:USDT") as nested:
            assert first is nested
            nested.check()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def contend():
                with operation_lock("BTC/USDT:USDT"):
                    return True

            with pytest.raises(RuntimeError, match="(?i)lock"):
                pool.submit(contend).result(timeout=10)
        first.check()
    with operation_lock("BTC/USDT:USDT") as later:
        later.check()


def test_monitor_cannot_reconcile_unacknowledged_entry(entry_setup, monkeypatch):
    exchange, executor, order = entry_setup
    sending = threading.Event()
    release = threading.Event()
    original = exchange.create_order

    def block_entry(symbol, order_type, side, amount, price=None, params=None):
        if params and params.get("clientOrderId", "").startswith("entry-"):
            sending.set()
            assert release.wait(10)
        return original(symbol, order_type, side, amount, price, params)

    monkeypatch.setattr(exchange, "create_order", block_entry)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_run_entry, executor, order)
        try:
            assert sending.wait(10)
            task_id = _entry_intent()["task_id"]
            with pytest.raises(RuntimeError, match="(?i)lock"):
                executor.monitor_bracket(task_id)
        finally:
            release.set()
        first.result(timeout=10)
