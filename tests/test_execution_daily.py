"""Unit tests for the daily runner (tradingagents/execution/daily.py).

``TradingAgentsGraph`` is mocked wholesale (the real one would spend LLM
quota and touch vendors); the bridge's two network reads (balance sync and
price fetch) are stubbed at class level so no socket opens. The audit log
lives in tmp_path and the risk guard's clock is frozen.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tradingagents.execution.daily as daily_module
import tradingagents.execution.risk as risk_module
from tests.test_execution_bridge import FakeExchange
from tradingagents.dataflows.config import set_config
from tradingagents.default_config import build_default_config
from tradingagents.execution.daily import DailyResult, _handle_plan, run_daily
from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

FROZEN_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


class FakeGraph:
    """Stand-in for TradingAgentsGraph: records construction and propagation."""

    signals: dict[str, str] = {}
    constructed: list = []
    propagated: list = []
    error_on: str | None = None

    def __init__(self, config=None, **kwargs):
        self.config = config
        self.selected_analysts = kwargs.get("selected_analysts")
        FakeGraph.constructed.append(self)

    def propagate(self, coin, trade_date, asset_type="stock", portfolio=None):
        FakeGraph.propagated.append((coin, trade_date, asset_type))
        if self.error_on is not None and coin == self.error_on:
            raise RuntimeError("vendor outage")
        return (object(), FakeGraph.signals.get(coin, "Hold"))


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Scrub env, freeze both clocks, stub the bridge's network reads."""
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)
    monkeypatch.setattr(risk_module, "_utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(daily_module, "_today_iso", lambda: "2026-09-28")
    from tradingagents.execution.bridge import ExchangeBridge

    monkeypatch.setattr(
        ExchangeBridge, "sync_portfolio",
        lambda self: PortfolioContext(cash=1_000.0),
    )
    monkeypatch.setattr(ExchangeBridge, "_fetch_price", lambda self, symbol: 50_000.0)
    # plan_order's network path builds the venue client for load_markets;
    # hand it an inert stand-in so no real ccxt client is ever constructed.
    monkeypatch.setattr(
        ExchangeBridge, "_exchange_or_raise",
        lambda self: SimpleNamespace(load_markets=lambda: {}),
    )
    FakeGraph.signals = {"BTC-USD": "Buy", "ETH-USD": "Sell", "SOL-USD": "Hold"}
    FakeGraph.constructed = []
    FakeGraph.propagated = []
    FakeGraph.error_on = None
    monkeypatch.setattr(daily_module, "_PROPAGATE_RETRY_WAIT", 0.0)  # no real backoff in tests
    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.TradingAgentsGraph", FakeGraph
    )
    yield


def test_graph_runs_the_crypto_analyst_set_without_fundamentals(tmp_path):
    # The daily runner is crypto-only: the fundamentals analyst must be
    # dropped exactly like the CLI's filter_analysts_for_asset_type does
    # (Yahoo has no quoteSummary fundamentals for crypto symbols, so running
    # it only burns LLM calls on an empty report).
    run_daily(["BTC-USD"], config=make_config(tmp_path))
    graph = FakeGraph.constructed[0]
    assert graph.selected_analysts == ("market", "social", "news")


def make_config(tmp_path, **extra):
    merged = {
        "exec_log_path": str(tmp_path / "audit.jsonl"),
        "auto_spot_enabled": True,
    }
    merged.update(extra)
    return merged


def read_audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("auto_futures_enabled", [False, True])
def test_auto_spot_disabled_skips_graph_and_llm(tmp_path, auto_futures_enabled):
    results = run_daily(
        ["BTC-USD"],
        config=make_config(
            tmp_path,
            auto_spot_enabled=False,
            auto_futures_enabled=auto_futures_enabled,
        ),
    )

    assert len(results) == 1
    assert results[0].reason == "auto spot is disabled"
    assert results[0].signal is None
    assert results[0].plan is None
    assert results[0].executed is False
    assert FakeGraph.constructed == []
    assert FakeGraph.propagated == []
    assert read_audit(tmp_path) == []


def test_legacy_auto_confirm_env_does_not_enable_auto_spot(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    config = build_default_config()
    config["exec_log_path"] = str(tmp_path / "audit.jsonl")

    results = run_daily(["BTC-USD"], config=config)

    assert config["exec_auto_confirm"] is False
    assert config.get("auto_spot_enabled") is not True
    assert results[0].executed is False
    assert results[0].reason == "auto spot is disabled"
    assert FakeGraph.constructed == []
    assert FakeGraph.propagated == []


def test_legacy_auto_confirm_config_does_not_enable_auto_spot(tmp_path):
    results = run_daily(
        ["BTC-USD"],
        config=make_config(tmp_path, auto_spot_enabled=False, exec_auto_confirm=True),
    )

    assert results[0].executed is False
    assert results[0].reason == "auto spot is disabled"
    assert FakeGraph.constructed == []
    assert FakeGraph.propagated == []


def test_auto_spot_dry_executes_from_mode_switch(tmp_path):
    results = run_daily(
        ["BTC-USD"],
        config=make_config(
            tmp_path,
            auto_spot_enabled=True,
            auto_futures_enabled=False,
            exec_auto_confirm=False,
        ),
    )
    result = results[0]
    assert result.signal == "Buy"
    assert result.plan.side == "buy"
    assert result.executed is True
    assert result.order_id is None
    events = read_audit(tmp_path)
    assert events[-1]["action"] == "buy"
    assert events[-1]["phase"] == "execute"
    assert events[-1]["mode"] == "dry"
    assert events[-1]["confirmed"] is True


def test_halt_blocks_execution(tmp_path, monkeypatch):
    from tradingagents.execution.audit import append_event

    config = make_config(tmp_path, auto_spot_enabled=True)
    append_event(
        config["exec_log_path"],
        {"event": "halt", "timestamp": "2026-09-28T10:00:00+00:00", "reason": "streak"},
    )
    results = run_daily(["BTC-USD"], config=config)
    result = results[0]
    assert result.executed is False
    assert result.plan is None  # plan_order refused before returning anything
    assert "halt" in result.reason
    assert read_audit(tmp_path)[-1]["action"] == "rejected_by_risk"


def test_live_auto_spot_requires_configured_credentials(tmp_path):
    # With Auto Spot and live execution enabled, reach the venue credential check.
    set_config({"auto_spot_enabled": True})
    results = run_daily(
        ["BTC-USD"],
        config=make_config(tmp_path, auto_spot_enabled=True, exec_live=True),
    )
    result = results[0]
    assert result.executed is False
    assert "BINANCE_DEMO_API_KEY" in result.reason
    assert read_audit(tmp_path)[-1]["action"] == "execute_denied"


def test_live_auto_spot_override_cannot_enable_closed_process_mode(tmp_path):
    set_config({"auto_spot_enabled": False})

    results = run_daily(
        ["BTC-USD"],
        config=make_config(tmp_path, auto_spot_enabled=True, exec_live=True),
    )

    assert results[0].reason == "auto spot is disabled"
    assert results[0].signal is None
    assert results[0].plan is None
    assert results[0].executed is False
    assert FakeGraph.constructed == []
    assert FakeGraph.propagated == []
    assert read_audit(tmp_path) == []


def test_live_auto_spot_executes_when_all_gates_open(tmp_path, monkeypatch):
    from tradingagents.execution.bridge import ExchangeBridge

    exchange = FakeExchange()
    set_config({"auto_spot_enabled": True})
    monkeypatch.setattr(ExchangeBridge, "_exchange_or_raise", lambda self: exchange)
    results = run_daily(
        ["BTC-USD"],
        config=make_config(tmp_path, auto_spot_enabled=True, exec_live=True),
    )
    assert results[0].executed is True
    assert results[0].order_id == "order-123"
    assert ("create_order", "BTC/USDT", "market", "buy", "0.005000", None) in exchange.calls
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "buy"
    assert event["phase"] == "execute"
    assert event["mode"] == "live"
    assert event["confirmed"] is True


@pytest.mark.parametrize("auto_futures_enabled", [False, True])
def test_futures_plan_follows_auto_futures_not_auto_spot(tmp_path, auto_futures_enabled):
    config = make_config(
        tmp_path,
        auto_spot_enabled=not auto_futures_enabled,
        auto_futures_enabled=auto_futures_enabled,
        exec_auto_confirm=False,
        risk_max_derivatives_exposure_pct=100.0,
    )
    executor = DerivativesExecutor(config=config)
    plan = PlannedOrder(
        ticker="BTC-USD",
        ccxt_symbol="BTC/USDT:USDT",
        signal="Buy",
        side="buy",
        quantity=0.005,
        estimated_price=50_000.0,
        cost=250.0,
        market="derivatives",
        stop_loss=48_000.0,
        take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    result = DailyResult(ticker="BTC-USD", signal="Buy", plan=plan)

    _handle_plan(executor, plan, PortfolioContext(cash=1_000.0), result)

    event = read_audit(tmp_path)[-1]
    assert result.order_id is None
    if auto_futures_enabled:
        assert result.executed is True
        assert event["action"] == "buy"
        assert event["phase"] == "execute"
        assert event["mode"] == "dry"
        assert event["confirmed"] is True
    else:
        assert result.executed is False
        assert result.reason == "awaiting approval"
        assert event["action"] == "awaiting_approval"
        assert "Auto Futures is disabled" in event["reason"]


def test_coin_failure_is_isolated(tmp_path):
    FakeGraph.error_on = "ETH-USD"
    results = run_daily(["BTC-USD", "ETH-USD"], config=make_config(tmp_path))
    assert [r.ticker for r in results] == ["BTC-USD", "ETH-USD"]
    assert results[0].plan is not None  # first coin went through its policy
    assert results[1].plan is None
    assert "vendor outage" in results[1].reason
    # a fresh graph per propagate ATTEMPT: BTC 1, ETH initial + 2 retries
    assert len(FakeGraph.constructed) == 4


def test_propagate_runs_the_crypto_pipeline_with_utc_today(tmp_path):
    run_daily(["BTC-USD"], config=make_config(tmp_path))
    assert FakeGraph.propagated == [("BTC-USD", "2026-09-28", "crypto")]


def test_watchlist_defaults_to_config(tmp_path):
    results = run_daily(config=make_config(tmp_path))
    assert [r.ticker for r in results] == ["BTC-USD", "ETH-USD"]  # exec_watchlist


def test_watchlist_string_argument_wraps_and_does_not_split(tmp_path):
    # A bare string is one coin, never list()-split into characters.
    results = run_daily("BTC-USD", config=make_config(tmp_path))
    assert [r.ticker for r in results] == ["BTC-USD"]


def test_watchlist_config_string_wraps_and_does_not_split(tmp_path):
    results = run_daily(config=make_config(tmp_path, exec_watchlist="BTC-USD"))
    assert [r.ticker for r in results] == ["BTC-USD"]


def test_watchlist_entries_stripped_empties_dropped_deduped_in_order(tmp_path):
    results = run_daily(
        ["  BTC-USD  ", "", "BTC-USD", "ETH-USD", "   "],
        config=make_config(tmp_path),
    )
    assert [r.ticker for r in results] == ["BTC-USD", "ETH-USD"]


def test_hold_coin_records_reason_without_execution(tmp_path):
    results = run_daily(["SOL-USD"], config=make_config(tmp_path, auto_spot_enabled=True))
    assert results[0].signal == "Hold"
    assert results[0].executed is False
    assert results[0].plan.side is None
    assert "Hold" in results[0].reason


def test_result_shape():
    result = DailyResult(ticker="BTC-USD")
    assert result.executed is False
    assert result.order_id is None
    assert result.signal is None
    assert result.plan is None
