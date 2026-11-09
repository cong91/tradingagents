"""Offline coverage for daily and approval mode-generation authorization."""

import time
from threading import Event
from types import SimpleNamespace

import pytest

from server import approvals, daily
from server.auto_modes import MODE_KEYS, AutoMode
from tests.server_api_exchange import _FakeExchange
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.dataflows.config import set_config
from tradingagents.execution.auto_authorization import (
    AutoAuthorizationError,
    capture_process_config,
    mode_update_lock,
)
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


def _set_mode(mode, enabled):
    with mode_update_lock((mode,)):
        set_config({MODE_KEYS[mode]: enabled})


def _cycle_spot():
    _set_mode(AutoMode.SPOT, False)
    _set_mode(AutoMode.SPOT, True)


def _job(*, mode="live", auto=False):
    return daily.DailyJobState(
        job_id="d_token", mode=mode, tickers=[] if auto else ["BTC-USD"],
        created_at="2026-11-09T00:00:00Z", auto_top_n=1 if auto else None,
    )


def _enqueue(**overrides):
    plan = {
        "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT", "signal": "Buy",
        "side": "buy", "quantity": 0.0012, "price_est": 83078.0,
        "cost": 0.0012 * 83078.0,
    }
    plan.update(overrides)
    return approvals.enqueue(plan)


def _wait_for_job(client, job_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get(f"/api/daily/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.05)
    pytest.fail(f"daily job {job_id} did not finish")


@pytest.mark.parametrize("auto", [False, True])
def test_daily_captures_generation_before_worker_start(client, monkeypatch, auto):
    _set_mode(AutoMode.SPOT, True)
    entered, release = Event(), Event()
    name = "_execute_auto" if auto else "_execute"
    worker = getattr(daily, name)

    def paused_worker(job, config, authorization):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release daily worker")
        worker(job, config, authorization)

    monkeypatch.setattr(daily, name, paused_worker)
    monkeypatch.setattr(daily, "_screen_candidates", lambda *_: pytest.fail("stale job screened"))
    monkeypatch.setattr(daily, "_build_bridge", lambda *_: pytest.fail("stale job built a bridge"))
    request = {"mode": "live", "auto": auto}
    if not auto:
        request["tickers"] = ["BTC-USD"]
    created = client.post("/api/daily", json=request)
    try:
        assert created.status_code == 202
        assert entered.wait(10)
        _cycle_spot()
    finally:
        release.set()
    job = _wait_for_job(client, created.json()["job_id"])
    assert job["status"] == "failed"
    assert "authorization is stale" in job["error"]
    assert job["results"] == []


def test_revocation_during_graph_cancels_without_retry_or_planning(monkeypatch):
    _set_mode(AutoMode.SPOT, True)
    config, authorization = capture_process_config()
    graph_calls = []

    class Graph:
        def __init__(self, **kwargs):
            graph_calls.append("build")

        def propagate_with_telemetry(self, *args, check_cancelled, **kwargs):
            graph_calls.append("propagate")
            _cycle_spot()
            check_cancelled()
            pytest.fail("cancelled graph continued")

    monkeypatch.setattr("tradingagents.graph.trading_graph.TradingAgentsGraph", Graph)
    bridge = SimpleNamespace(sync_portfolio=lambda: pytest.fail("stale graph reached planning"))
    with pytest.raises(AutoAuthorizationError, match="stale"):
        daily._process_coin(_job(), bridge, "BTC-USD", config, authorization)
    assert graph_calls == ["build", "propagate"]


@pytest.mark.parametrize("stage", ["balance", "plan"])
def test_revocation_during_daily_planning_prevents_enqueue(monkeypatch, stage):
    _set_mode(AutoMode.SPOT, True)
    config, authorization = capture_process_config()
    calls = []
    monkeypatch.setattr(daily, "_analyze", lambda *args: ("Buy", None))
    monkeypatch.setattr(daily, "_attach_bracket", lambda _job, plan, _coin: plan)
    monkeypatch.setattr(daily.approvals_mod, "enqueue", lambda *_: pytest.fail("stale plan queued"))

    class Bridge:
        def sync_portfolio(self):
            calls.append("balance")
            if stage == "balance":
                _cycle_spot()
            return PortfolioContext(cash=1000.0)

        def plan_order(self, *args, **kwargs):
            calls.append("plan")
            _cycle_spot()
            return PlannedOrder(
                ticker="BTC-USD", ccxt_symbol="BTC/USDT", signal="Buy",
                side="buy", quantity=0.001, estimated_price=50000, cost=50,
            )

    with pytest.raises(AutoAuthorizationError, match="stale"):
        daily._process_coin(_job(), Bridge(), "BTC-USD", config, authorization)
    assert calls == (["balance"] if stage == "balance" else ["balance", "plan"])


def test_mock_job_remains_dry_and_synthetic_plans_cannot_execute(client, monkeypatch, tmp_path):
    _set_mode(AutoMode.SPOT, True)
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "audit.jsonl")})
    monkeypatch.setattr(daily, "_analyze", lambda *args: ("Buy", "synthetic"))
    created = client.post("/api/daily", json={"mode": "mock", "tickers": ["BTC-USD"]})
    assert created.status_code == 202
    job = _wait_for_job(client, created.json()["job_id"])
    assert job["status"] == "completed"
    result = job["results"][0]
    assert result["plan"]["mode_at_plan"] == "dry"
    item = approvals._load_items()[0]
    assert item["mock"] is True
    refused = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "mock_not_executable"
    assert approvals._load_items()[0]["status"] == "pending"


def test_approval_rechecks_generation_before_claim(client, monkeypatch):
    _set_mode(AutoMode.SPOT, True)
    item = _enqueue()

    def halt_state():
        _cycle_spot()
        return False, None

    monkeypatch.setattr(approvals.audit_log, "halt_state", halt_state)
    monkeypatch.setattr(approvals, "_build_bridge", lambda *_: pytest.fail("stale approval built a bridge"))
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "mode_closed"
    assert approvals._load_items()[0]["status"] == "pending"


@pytest.mark.parametrize("stage", ["balance", "venue_preflight"])
def test_approval_releases_claim_when_generation_changes(client, monkeypatch, tmp_path, stage):
    _set_mode(AutoMode.SPOT, True)
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "audit.jsonl")})

    class Venue(_FakeExchange):
        def fetch_balance(self, params=None):
            result = super().fetch_balance(params)
            if stage == "balance":
                _cycle_spot()
            return result

        def load_markets(self):
            result = super().load_markets()
            if stage == "venue_preflight":
                _cycle_spot()
            return result

    venue = Venue()
    tokens = []

    def build(config, authorization):
        tokens.append(authorization)
        return ExchangeBridge(config=config, exchange_factory=lambda: venue, authorization=authorization)

    monkeypatch.setattr(approvals, "_build_bridge", build)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "mode_closed"
    stored = approvals._load_items()[0]
    assert stored["status"] == "pending"
    assert stored["executed"] is None
    assert stored["order_id"] is None
    assert tokens and not tokens[0].allows(AutoMode.SPOT, live=True)
    assert venue.orders == []


def test_approval_with_current_token_executes_through_fake_bridge(client, monkeypatch, tmp_path):
    _set_mode(AutoMode.SPOT, True)
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "audit.jsonl")})
    venue = _FakeExchange()
    tokens = []

    def build(config, authorization):
        tokens.append(authorization)
        return ExchangeBridge(config=config, exchange_factory=lambda: venue, authorization=authorization)

    monkeypatch.setattr(approvals, "_build_bridge", build)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})

    assert response.status_code == 200
    assert response.json()["mode"] == "live"
    assert response.json()["executed"] is True
    assert tokens[0].allows(AutoMode.SPOT, live=True)
    assert len(venue.orders) == 1
    assert approvals._load_items()[0]["status"] == "executed"


def test_approval_refuses_derivatives_without_constructing_bridge(client, monkeypatch):
    _set_mode(AutoMode.SPOT, True)
    monkeypatch.setattr(approvals, "_build_bridge", lambda *_: pytest.fail("unsupported market built a bridge"))
    item = _enqueue(market="derivatives")
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unsupported_market"
    assert approvals._load_items()[0]["status"] == "pending"
