"""Offline checks for the futures entry account-identity boundary."""

from dataclasses import replace

import pytest

from server import job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _recovery_db as _recovery_db,
    bind_identity,
    bracket_kwargs,
    enable_auto_futures as enable_auto_futures,
    live_config,
    live_env,
    make_executor,
    read_audit,
)
from tradingagents.execution.futures_account import observe_futures_positions
from tradingagents.portfolio import PortfolioContext

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("enable_auto_futures")]


def test_live_entry_binds_identity_to_intent_position_and_bracket(tmp_path, monkeypatch):
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

    entries = [
        call for call in exchange.calls
        if call[0] == "create_order" and call[3] == "buy"
    ]
    assert len(entries) == 1
    intent = next(
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )
    assert intent["account_identity_hash"] == VERIFIED_IDENTITY.identity_hash
    assert intent["account_identity_profile"] == "demo"
    assert intent["account_identity_type"] == "usds_futures"
    assert intent["account_identity_exchange"] == "binance"
    position_key = f"{order.ccxt_symbol}:buy:{intent['placed_ids'][0]}"
    state = job_store.load_position_state(position_key)
    assert state is not None
    assert state["account_identity_hash"] == VERIFIED_IDENTITY.identity_hash
    bracket = job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:{intent['placed_ids'][0]}")
    assert bracket is not None
    assert bracket["account_identity_hash"] == VERIFIED_IDENTITY.identity_hash
    exits = [
        call for call in exchange.calls
        if call[0] == "create_order" and call[5].get("reduceOnly")
    ]
    assert [call[4] for call in exits] == [order.quantity, order.quantity]


def test_execute_live_without_any_identity_sends_nothing(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())

    with pytest.raises(RuntimeError, match="account identity"):
        executor._execute_live(order, True)

    assert not [
        call for call in exchange.calls
        if call[0] in {"create_order", "set_leverage", "set_margin_mode"}
    ]
    assert job_store.active_bracket_recoveries() == []


def test_complete_position_scope_does_not_prove_account_identity(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    monkeypatch.setattr(
        "tradingagents.execution.futures_account.observe_futures_positions",
        lambda bridge: replace(observe_futures_positions(bridge), scope_complete=True),
    )

    with pytest.raises(PermissionError, match="authenticated account identity"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert executor.risk_guard_decisions[-1]["stage"] == "execute"
    assert executor.risk_guard_decisions[-1]["allowed"] is True
    assert not [
        call for call in exchange.calls
        if call[0] in {"set_leverage", "set_margin_mode", "create_order", "cancel_order"}
    ]
    assert job_store.active_bracket_recoveries() == []
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "derivatives_denied"
    assert "authenticated account identity" in event["reason"]
