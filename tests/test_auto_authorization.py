"""Offline tests for mode generations and serialized order submission."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from server.auto_modes import MODE_KEYS, AutoMode
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    bind_identity,
)
from tests.test_execution_bridge import FakeExchange
from tradingagents.dataflows.config import get_config, run_config, set_config
from tradingagents.execution import derivatives as executor_module
from tradingagents.execution.auto_authorization import (
    AutoAuthorizationError,
    capture_authorization,
    capture_process_config,
    mode_update_lock,
)
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.execution.daily import DailyResult, _handle_plan
from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


class _Guard:
    def check(self):
        pass


@pytest.fixture(autouse=True)
def offline_futures(monkeypatch):
    """No production database or network is used by these tests."""
    from tradingagents.execution import derivatives, futures_account

    @contextmanager
    def lock(_symbol):
        yield _Guard()

    monkeypatch.setattr(derivatives, "operation_lock", lock)
    monkeypatch.setattr(derivatives, "create_guarded_entry_intent", lambda *a, **k: None)
    monkeypatch.setattr(derivatives, "update_guarded_entry_intent", lambda *a, **k: None)
    monkeypatch.setattr(derivatives, "_refresh_live_risk_context", lambda *a, **k: None)
    monkeypatch.setattr(
        futures_account, "read_futures_balance",
        lambda *_: futures_account.FuturesAccountSnapshot(
            equity=1_000.0, equity_currency="USDT",
        ),
    )
    monkeypatch.setattr(
        futures_account, "observe_futures_positions",
        lambda *_: SimpleNamespace(
            scope_complete=True, unknown_scope=False, unknown_symbols=[], positions=(),
        ),
    )
    monkeypatch.setattr(derivatives.job_store, "futures_position_stops", lambda: {})
    monkeypatch.setattr(derivatives.job_store, "update_bracket_recovery", lambda *a, **k: None)
    monkeypatch.setattr(derivatives.job_store, "persist_futures_entry_state", lambda *a, **k: True)
    monkeypatch.setattr(derivatives.snapshot_observer, "run", lambda *a, call, **k: call())
    bind_identity(monkeypatch, VERIFIED_IDENTITY)


def _set_mode(mode, enabled):
    with mode_update_lock((mode,)):
        set_config({MODE_KEYS[mode]: enabled})


def _enable_both():
    with mode_update_lock():
        set_config({MODE_KEYS[mode]: True for mode in AutoMode})


def _order(mode):
    futures = mode is AutoMode.FUTURES
    return PlannedOrder(
        ticker="BTC-USD",
        ccxt_symbol="BTC/USDT:USDT" if futures else "BTC/USDT",
        signal="Buy", side="buy", quantity=0.005, estimated_price=50_000.0, cost=250.0,
        market="derivatives" if futures else "spot",
        stop_loss=48_000.0 if futures else None,
        take_profit=55_000.0 if futures else None,
        futures_execution_intent="enter" if futures else None,
    )


def _venue(mode):
    return FakeDerivExchange() if mode is AutoMode.FUTURES else FakeExchange()


def _executor(mode, exchange, tmp_path, *, authorization=None, live=True, config=None):
    options = {
        "exec_log_path": str(tmp_path / "audit.jsonl"),
        "exec_live": live,
        "risk_max_derivatives_exposure_pct": 100.0,
        **(config or {}),
    }
    cls = DerivativesExecutor if mode is AutoMode.FUTURES else ExchangeBridge
    executor = cls(config=options, exchange_factory=lambda: exchange, authorization=authorization)
    if mode is AutoMode.FUTURES:
        executor._place_bracket = lambda *a, **k: None
    return executor


def _execute(executor, mode):
    order = _order(mode)
    if mode is AutoMode.FUTURES:
        return executor._execute_live(order, True, VERIFIED_IDENTITY)
    return executor.execute_order(order, confirm=True, portfolio=PortfolioContext(cash=1_000.0))


def _entry_calls(exchange):
    return [call for call in exchange.calls if call[0] == "create_order"]


def _thread(action):
    errors = []

    def run():
        try:
            action()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _join(thread):
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("mode", list(AutoMode))
def test_mode_revision_invalidates_only_that_mode(mode):
    _enable_both()
    config, snapshot = capture_process_config()
    other = AutoMode.FUTURES if mode is AutoMode.SPOT else AutoMode.SPOT
    _set_mode(mode, False)
    _set_mode(mode, True)

    assert config[MODE_KEYS[mode]] is True
    assert not snapshot.allows(mode, live=True)
    assert snapshot.allows(other, live=True)
    assert capture_authorization().allows(mode, live=True)


@pytest.mark.parametrize("mode", list(AutoMode))
def test_explicit_dry_override_does_not_authorize_live(mode, tmp_path):
    _set_mode(mode, False)
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, live=False, config={MODE_KEYS[mode]: True})
    snapshot = executor._bridge._auto_authorization if mode is AutoMode.FUTURES else executor._auto_authorization
    assert snapshot.allows(mode)
    assert not snapshot.allows(mode, live=True)
    assert snapshot.allows(mode)  # Standalone explicit dry configuration remains usable.
    _set_mode(mode, False)
    assert not snapshot.allows(mode)


def test_scoped_graph_config_cannot_hide_process_disable():
    _set_mode(AutoMode.FUTURES, True)
    snapshot = capture_authorization()
    with run_config({MODE_KEYS[AutoMode.FUTURES]: True}):
        _set_mode(AutoMode.FUTURES, False)
        assert not snapshot.allows(AutoMode.FUTURES, live=True)
        assert not capture_process_config()[0][MODE_KEYS[AutoMode.FUTURES]]


@pytest.mark.parametrize("mode", list(AutoMode))
def test_stale_work_cannot_send_after_disable_and_reenable(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    _set_mode(mode, False)
    _set_mode(mode, True)

    assert executor.effective_mode() == "live"
    order = _order(mode)
    with pytest.raises(PermissionError):
        executor.execute_order(order, confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    assert _entry_calls(exchange) == []
    with pytest.raises(AutoAuthorizationError, match="stale"):
        _execute(executor, mode)
    assert _entry_calls(exchange) == []


@pytest.mark.parametrize("mode", list(AutoMode))
def test_disable_during_preflight_returns_before_preflight_ends(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    entered, release = threading.Event(), threading.Event()
    original = exchange.load_markets

    def blocked_preflight():
        entered.set()
        assert release.wait(5)
        return original()

    exchange.load_markets = blocked_preflight
    thread, errors = _thread(lambda: _execute(executor, mode))
    assert entered.wait(5)
    updater, update_errors = _thread(lambda: _set_mode(mode, False))
    _join(updater)
    assert not update_errors
    _set_mode(mode, True)
    release.set()
    _join(thread)
    assert len(errors) == 1 and isinstance(errors[0], AutoAuthorizationError)
    assert _entry_calls(exchange) == []


@pytest.mark.parametrize("mode", list(AutoMode))
def test_disable_waits_for_already_started_venue_call(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    entered, release, update_started, updated = (threading.Event() for _ in range(4))
    original = exchange.create_order

    def blocked_order(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def disable():
        update_started.set()
        _set_mode(mode, False)
        updated.set()

    exchange.create_order = blocked_order
    worker, errors = _thread(lambda: _execute(executor, mode))
    assert entered.wait(5)
    updater, update_errors = _thread(disable)
    assert update_started.wait(5)
    assert not updated.wait(0.1)
    release.set()
    _join(updater)
    _join(worker)
    assert not update_errors
    assert len(_entry_calls(exchange)) == 1
    if mode is AutoMode.FUTURES:
        assert len(errors) == 1 and "bracket resting status" in str(errors[0])
    else:
        assert errors == []
    assert updated.is_set()


@pytest.mark.parametrize("mode", list(AutoMode))
def test_managed_mode_sends_live_with_exec_live_false(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    order = _order(mode)

    assert executor.effective_mode() == "live"
    assert get_config().get("exec_live") is not True
    if mode is AutoMode.FUTURES:
        with pytest.raises(RuntimeError, match="bracket resting status"):
            executor.execute_order(order, confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    else:
        assert executor.execute_order(
            order, confirm=True, portfolio=PortfolioContext(cash=1_000.0),
        ) == "order-123"
    assert len(_entry_calls(exchange)) == 1
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(event["action"] == "buy" and event["mode"] == "live" for event in events)
    assert get_config().get("exec_live") is not True


@pytest.mark.parametrize("mode", list(AutoMode))
def test_managed_mode_requires_confirmation(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    with pytest.raises(PermissionError, match="confirm=True"):
        executor.execute_order(_order(mode), confirm=False, portfolio=PortfolioContext(cash=1_000.0))
    assert not _entry_calls(exchange)


@pytest.mark.parametrize("mode", list(AutoMode))
def test_direct_library_dry_is_independent_of_process_auto_mode(mode, tmp_path):
    _set_mode(mode, False)
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, live=False, config={MODE_KEYS[mode]: True})
    assert executor.effective_mode() == "dry"
    assert executor.execute_order(
        _order(mode), confirm=True, portfolio=PortfolioContext(cash=1_000.0),
    ) is None
    assert not _entry_calls(exchange)


@pytest.mark.parametrize("mode", list(AutoMode))
def test_direct_live_still_requires_current_process_mode(mode, tmp_path):
    _set_mode(mode, False)
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, live=True, config={MODE_KEYS[mode]: True})
    assert executor.effective_mode() == "live"
    with pytest.raises(PermissionError):
        executor.execute_order(_order(mode), confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    assert not _entry_calls(exchange)


@pytest.mark.parametrize("mode", list(AutoMode))
def test_unmanaged_live_sends_when_process_mode_is_enabled(mode, tmp_path):
    _enable_both()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, live=True)
    if mode is AutoMode.FUTURES:
        with pytest.raises(RuntimeError, match="bracket resting status"):
            executor.execute_order(_order(mode), confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    else:
        assert executor.execute_order(
            _order(mode), confirm=True, portfolio=PortfolioContext(cash=1_000.0),
        ) == "order-123"
    assert len(_entry_calls(exchange)) == 1


def test_managed_spot_daily_does_not_use_exec_live_gate(tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(AutoMode.SPOT)
    bridge = _executor(AutoMode.SPOT, exchange, tmp_path, authorization=snapshot, live=False)
    result = DailyResult(ticker="BTC-USD")
    _handle_plan(bridge, _order(AutoMode.SPOT), PortfolioContext(cash=1_000.0), result)
    assert result.executed and result.order_id == "order-123"
    assert len(_entry_calls(exchange)) == 1


@pytest.mark.parametrize("mode", list(AutoMode))
def test_managed_mode_still_checks_risk(mode, tmp_path):
    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(mode)
    executor = _executor(mode, exchange, tmp_path, authorization=snapshot, live=False)
    executor.risk_guard.check = lambda *a, **k: SimpleNamespace(
        halted=False, allowed=False, reason="test risk rejection", override_used=False,
    )
    with pytest.raises(PermissionError, match="risk guard refused"):
        executor.execute_order(_order(mode), confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    assert not _entry_calls(exchange)


def test_managed_futures_preserves_bracket_and_identity_checks(tmp_path, monkeypatch):
    from tradingagents.execution import futures_identity

    _enable_both()
    _, snapshot = capture_process_config()
    exchange = _venue(AutoMode.FUTURES)
    executor = _executor(AutoMode.FUTURES, exchange, tmp_path, authorization=snapshot, live=False)
    order = _order(AutoMode.FUTURES)
    with pytest.raises(PermissionError):
        executor.execute_order(
            order.model_copy(update={"stop_loss": None}), confirm=True,
            portfolio=PortfolioContext(cash=1_000.0),
        )
    monkeypatch.setattr(futures_identity, "read_futures_account_identity", lambda *_: None)
    with pytest.raises(PermissionError, match="identity"):
        executor.execute_order(order, confirm=True, portfolio=PortfolioContext(cash=1_000.0))
    assert not _entry_calls(exchange)


def test_managed_executor_rejects_missing_protection_task_before_manager(tmp_path, monkeypatch):
    _enable_both()
    _, snapshot = capture_process_config()
    executor = _executor(AutoMode.FUTURES, _venue(AutoMode.FUTURES), tmp_path, authorization=snapshot, live=False)
    monkeypatch.setattr(executor_module.job_store, "load_bracket_recovery", lambda _task_id: None)
    monkeypatch.setattr(
        executor_module, "BracketManager",
        lambda *_: pytest.fail("missing protection task must fail before manager construction"),
    )
    assert executor.effective_mode() == "live"
    with pytest.raises(KeyError, match="bracket:BTC/USDT:USDT:entry-1"):
        executor.recover_bracket("bracket:BTC/USDT:USDT:entry-1")


def test_futures_protection_does_not_require_entry_mode(tmp_path, monkeypatch):
    _enable_both()
    executor = _executor(
        AutoMode.FUTURES, _venue(AutoMode.FUTURES), tmp_path,
        config={"risk_derivatives_profit_protection_enabled": True},
    )
    _set_mode(AutoMode.FUTURES, False)
    task_id = "bracket:BTC/USDT:USDT:entry-1"
    task = {
        "task_id": task_id,
        "symbol": "BTC/USDT:USDT",
        "status": "monitoring",
    }
    monkeypatch.setattr(executor_module.job_store, "load_bracket_recovery", lambda _task_id: task)
    monkeypatch.setattr(executor_module, "_verified_protection_task", lambda *_args, **_kwargs: None)
    calls = []
    monkeypatch.setattr(
        executor_module, "BracketManager",
        lambda bridge: SimpleNamespace(
            reconcile=lambda current_id, **kwargs: calls.append(current_id) or "protected",
        ),
    )
    assert executor.recover_bracket(task_id) == "protected"
    assert calls == [task_id]
    _enable_both()
    bridge = _executor(AutoMode.SPOT, _venue(AutoMode.SPOT), tmp_path, live=False)
    _set_mode(AutoMode.SPOT, False)
    _set_mode(AutoMode.SPOT, True)
    result = DailyResult(ticker="BTC-USD")
    _handle_plan(bridge, _order(AutoMode.SPOT), PortfolioContext(cash=1_000.0), result)
    assert not result.executed
    assert result.reason == "awaiting approval"
