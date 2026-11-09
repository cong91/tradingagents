"""Offline cancellation tests for the standalone daily runner."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from server.auto_modes import AutoMode
from tradingagents.dataflows.config import set_config
from tradingagents.execution.auto_authorization import mode_update_lock
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.execution.daily import run_daily
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


def _toggle_spot():
    with mode_update_lock((AutoMode.SPOT,)):
        set_config({"auto_spot_enabled": False})
    with mode_update_lock((AutoMode.SPOT,)):
        set_config({"auto_spot_enabled": True})


def _config(tmp_path):
    return {
        "exec_log_path": str(tmp_path / "audit.jsonl"),
        "exec_live": False,
        "auto_spot_enabled": True,
    }


@pytest.fixture
def boundaries(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ExchangeBridge, "sync_portfolio",
        lambda self: calls.append("sync") or PortfolioContext(cash=1_000.0),
    )
    monkeypatch.setattr(
        ExchangeBridge, "plan_order",
        lambda self, ticker, signal, portfolio: calls.append("plan") or PlannedOrder(
            ticker=ticker, ccxt_symbol="BTC/USDT", signal=signal, side="buy",
            quantity=0.005, estimated_price=50_000.0, cost=250.0,
        ),
    )
    monkeypatch.setattr(
        ExchangeBridge, "execute_order",
        lambda self, order, confirm, portfolio: calls.append("execute"),
    )
    return calls


def _set_graph(monkeypatch, graph):
    monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", graph)


def test_explicit_dry_config_runs_without_process_mode(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            boundaries.append("construct")

        def propagate(self, ticker, today, asset_type):
            boundaries.append("analysis")
            return {}, "Buy"

    _set_graph(monkeypatch, Graph)
    result = run_daily(["BTC-USD"], config=_config(tmp_path))[0]
    assert result.executed
    assert boundaries == ["construct", "analysis", "sync", "plan", "execute"]


def test_disabled_spot_never_constructs_graph(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            pytest.fail("disabled mode must not construct graph")

    _set_graph(monkeypatch, Graph)
    result = run_daily(["BTC-USD"], config={**_config(tmp_path), "auto_spot_enabled": False})[0]
    assert result.reason == "auto spot is disabled"
    assert not boundaries


def test_disable_during_graph_construction_stops_analysis(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            boundaries.append("construct")
            _toggle_spot()

        def propagate(self, *args, **kwargs):
            pytest.fail("stale mode must not start analysis")

    _set_graph(monkeypatch, Graph)
    results = run_daily(["BTC-USD", "ETH-USD"], config=_config(tmp_path))
    assert boundaries == ["construct"]
    assert all("authorization is stale" in result.reason for result in results)
    assert all(not result.executed and result.plan is None for result in results)


def test_disable_after_analysis_prevents_sync_and_later_coins(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            boundaries.append("construct")

        def propagate(self, ticker, today, asset_type):
            boundaries.append("analysis")
            _toggle_spot()
            return {}, "Buy"

    _set_graph(monkeypatch, Graph)
    results = run_daily(["BTC-USD", "ETH-USD"], config=_config(tmp_path))
    assert boundaries == ["construct", "analysis"]
    assert all("authorization is stale" in result.reason for result in results)


def test_disable_during_failed_analysis_does_not_retry(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            boundaries.append("construct")

        def propagate(self, ticker, today, asset_type):
            boundaries.append("analysis")
            _toggle_spot()
            raise RuntimeError("provider error")

    _set_graph(monkeypatch, Graph)
    monkeypatch.setattr("tradingagents.execution.daily.time.sleep", lambda _: pytest.fail("must not sleep"))
    result = run_daily(["BTC-USD"], config=_config(tmp_path))[0]
    assert boundaries == ["construct", "analysis"]
    assert "authorization is stale" in result.reason


def test_disable_during_retry_wait_prevents_new_attempt(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            boundaries.append("construct")

        def propagate(self, ticker, today, asset_type):
            boundaries.append("analysis")
            raise RuntimeError("provider error")

    _set_graph(monkeypatch, Graph)
    monkeypatch.setattr("tradingagents.execution.daily.time.sleep", lambda _: _toggle_spot())
    result = run_daily(["BTC-USD"], config=_config(tmp_path))[0]
    assert boundaries == ["construct", "analysis"]
    assert "authorization is stale" in result.reason


def test_disable_during_sync_prevents_plan(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, today, asset_type):
            return {}, "Buy"

    def sync(self):
        boundaries.append("sync")
        _toggle_spot()
        return PortfolioContext(cash=1_000.0)

    _set_graph(monkeypatch, Graph)
    monkeypatch.setattr(ExchangeBridge, "sync_portfolio", sync)
    result = run_daily(["BTC-USD"], config=_config(tmp_path))[0]
    assert boundaries == ["sync"]
    assert result.plan is None and not result.executed
    assert "authorization is stale" in result.reason


def test_disable_during_planning_prevents_execution(tmp_path, monkeypatch, boundaries):
    class Graph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, today, asset_type):
            return {}, "Buy"

    def plan(self, ticker, signal, portfolio):
        boundaries.append("plan")
        _toggle_spot()
        return SimpleNamespace(side="buy", market="spot")

    _set_graph(monkeypatch, Graph)
    monkeypatch.setattr(ExchangeBridge, "plan_order", plan)
    result = run_daily(["BTC-USD"], config=_config(tmp_path))[0]
    assert boundaries == ["sync", "plan"]
    assert not result.executed
    assert "authorization is stale" in result.reason


def test_graph_callback_stops_at_next_boundary_after_disable(tmp_path, monkeypatch, boundaries):
    entered, resume = threading.Event(), threading.Event()

    class Graph:
        def __init__(self, **kwargs):
            pass

        def propagate_with_telemetry(self, ticker, today, *, asset_type, telemetry, check_cancelled):
            check_cancelled()
            boundaries.append("first node")
            entered.set()
            assert resume.wait(5)
            check_cancelled()
            boundaries.append("second node")
            return {}, "Buy"

    _set_graph(monkeypatch, Graph)
    outcomes, errors = [], []

    def run():
        try:
            outcomes.extend(run_daily(["BTC-USD"], config=_config(tmp_path)))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        _toggle_spot()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert boundaries == ["first node"]
    assert "authorization is stale" in outcomes[0].reason
