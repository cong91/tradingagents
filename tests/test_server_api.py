"""API tests for the control-panel server (docs/ui-api-contract.md).

Unit-scoped: the ASGI app under TestClient, no sockets, no LLM, no vendors —
every engine surface the handlers touch is pointed at tmp files or a fake
graph. The module-level ``_no_network`` shadow keeps conftest's refusal for
every real socket while whitelisting only the event loop's own self-pipe.
"""

import json
import socket
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import (
    approvals as approvals_mod,
    daily as daily_mod,
    paths as server_paths,
    portfolio as portfolio_mod,
    runs as runs_mod,
    settings as settings_mod,
)
from server.main import app
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import _ENV_OVERRIDES

pytestmark = pytest.mark.unit

# §0 CSRF: every state-changing method must send application/json — DELETE too.
_JSON = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests.

    The ASGI transport itself never opens a socket, but creating the asyncio
    portal's event loop on Windows builds a loopback self-pipe via
    ``socket.socketpair``. That internal pair is whitelisted by marking the
    sockets it returns; every other ``connect`` — a stray vendor/LLM call —
    still refuses, keeping the no-network guarantee.
    """
    if request.node.get_closest_marker("integration"):
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def marked_socketpair(*args, **kwargs):
        # Windows socketpair() performs its own loopback handshake through
        # socket.connect, so the refusal must step aside for its duration,
        # then re-arm. Only the pair it returns is whitelisted afterwards.
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        try:
            first, second = original_socketpair(*args, **kwargs)
        finally:
            socket.socket.connect = refuse_connect
            socket.socket.connect_ex = refuse_connect_ex
        selfpipe_sockets.add(first)
        selfpipe_sockets.add(second)
        return first, second

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect_ex)
    monkeypatch.setattr(socket, "socketpair", marked_socketpair)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@pytest.fixture(autouse=True)
def _server_state(tmp_path, monkeypatch):
    """Point all server state at tmp and start every test with an empty registry."""
    monkeypatch.setattr(server_paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_paths, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(server_paths, "APPROVALS_PATH", tmp_path / "approvals.jsonl")
    monkeypatch.setattr(server_paths, "SERVER_AUDIT_PATH", tmp_path / "server-audit.jsonl")
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})


@pytest.fixture()
def client(_server_state):
    with TestClient(app) as test_client:
        yield test_client


def _drain_sse(client: TestClient, url: str, headers: dict | None = None) -> list[dict]:
    events = []
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


# --- health -----------------------------------------------------------------


def test_health_reports_dry_mode_and_counts(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["exec_mode"] == "dry"  # exec_live default False, env flag blanked by conftest
    assert body["generated_at"].endswith("Z")
    assert body["active_run_id"] is None
    assert body["pending_approvals"] == 0
    assert set(body["llm_key_configured"]) == {"openai", "google", "anthropic"}


# --- settings ----------------------------------------------------------------


def test_settings_get_masks_credentials_and_lists_paths(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcd1234")
    # This machine may hold real exchange credentials; the API only ever
    # reports configured/masked, so blank them here to make the assert exact.
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()

    editable = [row for row in body["settings"] if row["editable"]]
    readonly = {row["key"] for row in body["settings"] if not row["editable"]}
    # Contract §1 prose says "24 rows" but its own list — and the file — have 25
    # (_ENV_OVERRIDES, default_config.py:10-53). The mapping is authoritative.
    assert len(editable) == len(_ENV_OVERRIDES)
    assert "llm_provider" in {row["key"] for row in editable}
    assert readonly == {"results_dir", "data_cache_dir", "memory_log_path", "exec_log_path"}

    credentials = {item["env_var"]: item for item in body["credentials"]}
    assert credentials["OPENAI_API_KEY"]["configured"] is True
    assert credentials["OPENAI_API_KEY"]["masked"].endswith("1234")
    assert "sk-test-abcd" not in response.text  # never the raw key
    assert credentials["BINANCE_API_KEY"]["configured"] is False
    assert credentials["BINANCE_API_KEY"]["masked"] is None

    assert body["gates_readonly"]["exec_live_config"] is False
    assert body["effective_exec_mode"] == "dry"


def test_settings_put_applies_in_memory_and_persists_env(client, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# keep this comment\nTRADINGAGENTS_OUTPUT_LANGUAGE=English\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_file)

    response = client.put("/api/settings", json={"settings": {"max_debate_rounds": 2}})
    assert response.status_code == 200
    assert response.json()["applied_at"]
    assert get_config()["max_debate_rounds"] == 2  # in-memory, effective for new runs

    text = env_file.read_text(encoding="utf-8")
    assert "# keep this comment" in text  # unrelated lines survive
    assert "TRADINGAGENTS_OUTPUT_LANGUAGE=English" in text
    assert "TRADINGAGENTS_MAX_DEBATE_ROUNDS=2" in text


def test_settings_put_forbids_gate_and_unknown_keys(client):
    response = client.put("/api/settings", json={"settings": {"exec_live": True}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_key"
    assert get_config()["exec_live"] is False  # the FR5 config gate never moves via API


def test_settings_put_coercion_error_names_env_var(client):
    response = client.put("/api/settings", json={"settings": {"max_debate_rounds": "abc"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "coercion_error"
    assert error["details"]["env_var"] == "TRADINGAGENTS_MAX_DEBATE_ROUNDS"


# --- CSRF guard (§0) ---------------------------------------------------------


def test_csrf_rejects_safelisted_content_type(client):
    response = client.post(
        "/api/watchlist",
        content=json.dumps({"symbol": "XRP-USD"}),
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_csrf_rejects_foreign_origin(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


def test_csrf_rejects_cross_site_fetch(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


def test_csrf_allows_control_panel_origin(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Origin": "http://localhost:3000"}
    )
    assert response.status_code == 201


# --- watchlist ----------------------------------------------------------------


def test_watchlist_add_delete_roundtrip(client):
    # Startup seeds the engine defaults (BTC-USD, ETH-USD), so assert on SOL-USD.
    added = client.post("/api/watchlist", json={"symbol": "sol-usd"})
    assert added.status_code == 201
    symbols = [item["symbol"] for item in added.json()["symbols"]]
    assert "SOL-USD" in symbols
    assert "sol-usd" not in symbols  # normalized to Yahoo form
    assert all(item["added_at"] for item in added.json()["symbols"])

    duplicate = client.post("/api/watchlist", json={"symbol": "SOL-USD"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"

    empty = client.post("/api/watchlist", json={"symbol": "   "})
    assert empty.status_code == 400

    removed = client.delete("/api/watchlist/sol-usd", headers=_JSON)
    assert removed.status_code == 200
    assert "SOL-USD" not in [item["symbol"] for item in removed.json()["symbols"]]

    again = client.delete("/api/watchlist/SOL-USD", headers=_JSON)
    assert again.status_code == 404

    # Deleting the last symbol is allowed: an empty watchlist is valid (§2).
    for item in removed.json()["symbols"]:
        response = client.delete(f"/api/watchlist/{item['symbol']}", headers=_JSON)
        assert response.status_code == 200
    assert client.get("/api/watchlist").json()["symbols"] == []


# --- runs ---------------------------------------------------------------------


def test_run_mock_completes_with_synthetic_events(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "MOCK_EVENT_DELAY", 0.0)
    # An empty results_dir forces the synthetic fixture: this machine's real
    # ~/.tradingagents/logs may hold a replayable state log for BTC-USD.
    set_config({"results_dir": str(tmp_path / "results")})
    created = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto", "mode": "mock",
    })
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "queued"
    assert body["events_url"] == f"/api/runs/{body['run_id']}/events"
    assert body["analysts"] == ["market", "social", "news"]

    events = _drain_sse(client, body["events_url"])
    types = [event["type"] for event in events]
    assert types[0] == "run_started"
    assert types[-1] == "run_completed"
    assert "trader_update" in types and "pm_decision" in types
    assert all(event["replay"] for event in events)
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert events[-1]["payload"]["signal"] == "Hold"

    detail = client.get(f"/api/runs/{body['run_id']}").json()
    assert detail["status"] == "completed"
    assert detail["signal"] == "Hold"
    assert detail["is_review"] is False
    assert detail["finished_at"]


def test_run_mock_replays_saved_state_log(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "MOCK_EVENT_DELAY", 0.0)
    today = _today()
    log_dir = tmp_path / "results" / "BTC-USD" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    state_log = log_dir / f"full_states_log_{today}.json"
    state_log.write_text(json.dumps({
        "company_of_interest": "BTC-USD", "trade_date": today,
        "market_report": "real market report", "sentiment_report": "real sentiment",
        "news_report": "real news", "fundamentals_report": "",
        "investment_debate_state": {
            "bull_history": "bull says up", "bear_history": "bear says down",
            "history": "", "current_response": "", "judge_decision": "judge calls it",
        },
        "trader_investment_decision": "trader plan", "investment_plan": "research plan",
        "risk_debate_state": {
            "aggressive_history": "aggressive view", "conservative_history": "conservative view",
            "neutral_history": "", "history": "", "judge_decision": "pm judges",
        },
        "final_trade_decision": "Final Decision:\nRating: Buy",
    }, ensure_ascii=False), encoding="utf-8")
    set_config({"results_dir": str(tmp_path / "results")})

    created = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": today, "asset_type": "crypto", "mode": "mock",
    })
    events = _drain_sse(client, created.json()["events_url"])
    types = [event["type"] for event in events]
    assert "debate_update" in types and "research_manager" in types
    assert "trader_update" in types and "risk_update" in types and "pm_decision" in types

    completed = events[-1]["payload"]
    assert completed["signal"] == "Buy"
    assert completed["report_paths"] == {"state_log": str(state_log)}
    debate_events = [event for event in events if event["type"] == "debate_update"]
    assert debate_events[0]["payload"] == {"field": "bull_history", "content": "bull says up"}


def test_run_sse_replays_from_last_event_id(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "MOCK_EVENT_DELAY", 0.0)
    set_config({"results_dir": str(tmp_path / "results")})  # synthetic, deterministic
    created = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto", "mode": "mock",
    })
    run_id = created.json()["run_id"]
    all_events = _drain_sse(client, f"/api/runs/{run_id}/events")
    cursor = all_events[1]["seq"]

    resumed = _drain_sse(client, f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)})
    assert [event["seq"] for event in resumed] == [event["seq"] for event in all_events[2:]]


def test_run_rejected_while_another_run_is_active(client):
    active = runs_mod.RunState(
        run_id="r_active", ticker="BTC-USD", trade_date=_today(),
        asset_type="crypto", mode="mock", checkpoint=None, created_at=_today(),
    )
    active.status = "running"
    runs_mod._runs[active.run_id] = active

    response = client.post("/api/runs", json={
        "ticker": "ETH-USD", "trade_date": _today(), "asset_type": "crypto",
    })
    assert response.status_code == 409
    assert response.json()["error"]["details"]["run_id"] == "r_active"


def test_run_validates_input(client):
    for bad_body in (
        {"trade_date": _today(), "asset_type": "crypto"},                      # no ticker
        {"ticker": "BTC-USD", "trade_date": "09/30/2026", "asset_type": "crypto"},  # bad date
        {"ticker": "BTC-USD", "trade_date": "2999-01-01", "asset_type": "crypto"},  # future
        {"ticker": "BTC-USD", "trade_date": _today(), "asset_type": "fx"},     # bad asset
        {"ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto", "mode": "replay"},
        {"ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto", "source_run": "r_old"},
    ):
        response = client.post("/api/runs", json=bad_body)
        assert response.status_code == 400, bad_body
        assert response.json()["error"]["code"] == "validation_error"

    missing = client.get("/api/runs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


class _FakeGraph:
    """Minimal TradingAgentsGraph stand-in: two chunks, no LLM, no network."""

    def __init__(self, tmp_path: Path):
        self._tmp = tmp_path
        self.graph = SimpleNamespace(stream=lambda state, **kwargs: iter([
            {"messages": [SimpleNamespace(
                id="m1", content="market report body",
                tool_calls=[{"name": "get_stock_data", "args": {"ticker": "BTC-USD"}},
            ])]},
            {
                "investment_debate_state": {
                    "bull_history": "bull says up", "bear_history": "",
                    "judge_decision": "judge calls it",
                },
                "trader_investment_plan": "trader plan",
                "risk_debate_state": {
                    "aggressive_history": "aggressive view", "conservative_history": "",
                    "neutral_history": "", "judge_decision": "pm judges",
                },
                "final_trade_decision": "Final Decision: Rating: Buy",
            },
        ]))
        self.propagator = SimpleNamespace(get_graph_args=lambda **kwargs: {})

    def create_run_state(self, ticker, trade_date, asset_type):
        return {"company_of_interest": ticker}

    def checkpoint_input(self, state):
        return state

    def begin_checkpoint(self, *args, **kwargs):
        return None

    def end_checkpoint(self):
        pass

    def record_decision(self, *args, **kwargs):
        pass

    def clear_checkpoint_on_success(self, *args, **kwargs):
        pass

    def process_signal(self, text):
        return "Buy"

    def save_reports(self, final_state, ticker, save_path=None):
        report = self._tmp / "complete_report.md"
        report.write_text("report", encoding="utf-8")
        return report


def test_run_live_streams_graph_chunks_as_sse(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "_build_graph", lambda config, analysts: _FakeGraph(tmp_path))
    created = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto",
    })
    assert created.status_code == 201
    assert created.json()["mode"] == "live"

    events = _drain_sse(client, created.json()["events_url"])
    types = [event["type"] for event in events]
    assert types[0] == "run_started"
    assert types[-1] == "run_completed"
    assert "tool_call" in types
    assert "debate_update" in types and "trader_update" in types
    assert "risk_update" in types and "pm_decision" in types
    assert all(not event["replay"] for event in events)

    completed = events[-1]["payload"]
    assert completed["signal"] == "Buy"
    assert completed["is_review"] is False
    assert completed["report_paths"]["complete_report"].endswith("complete_report.md")

    detail = client.get(f"/api/runs/{created.json()['run_id']}").json()
    assert detail["status"] == "completed"


# --- approvals -----------------------------------------------------------------


def _enqueue(**overrides) -> dict:
    plan = {
        "run_id": "r_test", "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT",
        "signal": "Buy", "side": "buy", "quantity": 0.0012, "price_est": 83078.0,
        "cost": 99.69,
    }
    plan.update(overrides)
    return approvals_mod.enqueue(plan)


def test_approvals_list_counts_pending(client):
    _enqueue()
    _enqueue(ticker="ETH-USD", ccxt_symbol="ETH/USDT")
    body = client.get("/api/approvals").json()
    assert body["pending_count"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["status"] == "pending"
    assert body["generated_at"].endswith("Z")

    limited = client.get("/api/approvals", params={"limit": 1}).json()
    assert len(limited["items"]) == 1


def test_approve_requires_live_gate(client):
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "gate_closed"
    assert client.get("/api/approvals").json()["pending_count"] == 1  # untouched


class _FakeExchange:
    """Deterministic venue for the approve pipeline; no network, no ccxt."""

    apiKey = "test-key"
    secret = "test-secret"

    def __init__(self, *, fail_create: bool = False):
        self.fail_create = fail_create
        self.orders: list[tuple[str, str, str, str]] = []

    def fetch_ticker(self, symbol):
        return {"last": 83078.0, "close": 83078.0}

    def load_markets(self):
        return {"BTC/USDT": {"limits": {"cost": {"min": 10.0}}}}

    def fetch_balance(self):
        return {"total": {"BTC": 0.001, "USDT": 9500.0}, "free": {"USDT": 9500.0}}

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.8f}"

    def create_order(self, symbol, type_, side, amount):
        if self.fail_create:
            raise RuntimeError("simulated exchange outage")
        self.orders.append((symbol, type_, side, amount))
        return {"id": "7570510"}


def _inject_bridge(monkeypatch, exchange: _FakeExchange) -> None:
    """Point the approve pipeline's venue seam (approvals._build_bridge) at the fake."""

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(approvals_mod, "_build_bridge", build)


def _engine_audit(tmp_path) -> Path:
    audit_file = tmp_path / "engine-audit.jsonl"
    set_config({"exec_log_path": str(audit_file)})
    return audit_file


def test_approve_dry_fills_and_resolves_item(client, tmp_path, monkeypatch):
    set_config({"exec_live": True})  # config gate open; env flag stays blanked → dry
    _engine_audit(tmp_path)
    _inject_bridge(monkeypatch, _FakeExchange())
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed"] is False
    assert body["mode"] == "dry"
    assert body["order_id"] is None
    assert body["audit_action"] == "buy"
    assert body["risk"] == {"allowed": True, "halted": False, "reason": None}

    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["executed"] is False
    assert stored["mode"] == "dry"
    assert stored["order_id"] is None

    again = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"

    trail = (tmp_path / "server-audit.jsonl").read_text(encoding="utf-8")
    assert "approval_granted" in trail
    assert "approval_executed" in trail
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert "dry-run: order not sent to exchange" in engine_log


def test_approve_live_places_order_through_bridge(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")  # second gate half: real send
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["mode"] == "live"
    assert body["order_id"] == "7570510"
    assert exchange.orders == [("BTC/USDT", "market", "buy", "0.00120000")]

    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["order_id"] == "7570510"
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "buy"' in engine_log
    assert '"confirmed": true' in engine_log
    assert "7570510" in engine_log


def test_approve_refuses_mock_item_and_stays_pending(client, monkeypatch):
    set_config({"exec_live": True})
    _inject_bridge(monkeypatch, _FakeExchange())  # must never be reached
    item = _enqueue(mock=True)
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mock_not_executable"
    assert approvals_mod._load_items()[0]["status"] == "pending"  # still rejectable
    rejected = client.post(f"/api/approvals/{item['id']}/reject", json={"reason": "demo"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_approve_refuses_non_rebuildable_item_and_stays_pending(client):
    set_config({"exec_live": True})
    item = _enqueue(side=None, quantity=None, signal="Hold")
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert approvals_mod._load_items()[0]["status"] == "pending"


def test_approve_execute_failed_when_guard_refuses(client, tmp_path, monkeypatch):
    set_config({"exec_live": True})
    _engine_audit(tmp_path)
    _inject_bridge(monkeypatch, _FakeExchange())
    item = _enqueue(quantity=100.0, cost=9999999.0)  # breaches the per-asset cap at re-check
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "risk_halted"
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "execute_failed"
    assert "risk guard refused execution" in stored["resolution"]
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "rejected_by_risk"' in engine_log


def test_approve_exchange_failure_marks_execute_failed(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    _inject_bridge(monkeypatch, _FakeExchange(fail_create=True))
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "execute_failed"
    assert approvals_mod._load_items()[0]["status"] == "execute_failed"
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "rejected_at_execute"' in engine_log


def test_approve_survives_placed_but_unaudited_order(client, tmp_path, monkeypatch):
    # bridge.py:464-469: the order exists on the venue but the audit write failed —
    # the item must resolve to executed, never to a retryable failure.
    import tradingagents.execution.bridge as bridge_mod

    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)

    def broken_append(path, event):
        raise OSError("disk full")

    monkeypatch.setattr(bridge_mod, "append_event", broken_append)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed"] is True
    assert body["order_id"] == "7570510"
    assert "WAS placed" in body["warning"]
    assert exchange.orders  # the order exists on the venue — never retry
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["order_id"] == "7570510"


def test_approve_blocked_when_risk_guard_halted_today(client, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text(
        json.dumps({"event": "halt", "timestamp": f"{_today()}T00:00:00+00:00",
                    "reason": "daily loss cap breached"}) + "\n",
        encoding="utf-8",
    )
    set_config({"exec_live": True, "exec_log_path": str(audit_file)})
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "risk_halted"
    assert error["details"]["reason"] == "daily loss cap breached"


def test_reject_records_reason_and_never_takes_the_gate(client, tmp_path):
    item = _enqueue()  # exec_live stays False: reject must not care
    response = client.post(f"/api/approvals/{item['id']}/reject", json={"reason": "too expensive"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["resolution"] == "too expensive"
    assert body["resolved_at"]

    trail = (tmp_path / "server-audit.jsonl").read_text(encoding="utf-8")
    assert "approval_denied" in trail

    again = client.post(f"/api/approvals/{item['id']}/reject", json={})
    assert again.status_code == 409


def test_approval_queue_survives_a_restart(client, tmp_path):
    item = _enqueue()
    reloaded = approvals_mod._load_items()  # what a fresh server process would read
    assert [loaded["id"] for loaded in reloaded] == [item["id"]]
    assert reloaded[0]["status"] == "pending"


# --- audit ----------------------------------------------------------------------


def test_audit_reads_engine_log_with_filters_and_halt(client, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    today = _today()
    audit_file.write_text("\n".join([
        json.dumps({"timestamp": f"{today}T04:02:00.509077+00:00", "ticker": "BTC-USD",
                    "ccxt_symbol": "BTC/USDT", "signal": "Buy", "action": "buy",
                    "qty": 0.0012, "price_est": 83078.0, "mode": "dry", "confirmed": True,
                    "order_id": None, "phase": "execute", "market": "spot", "leverage": 1.0,
                    "reason": "dry fill"}),
        json.dumps({"event": "halt", "timestamp": f"{today}T05:00:00+00:00",
                    "reason": "daily loss cap breached"}),
        "{oops: not json",
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/audit").json()
    assert body["total_lines"] == 2
    assert body["skipped_lines"] == 1
    assert body["halted_today"] is True
    assert body["halt_reason"] == "daily loss cap breached"
    assert body["last_halt"].startswith(today)
    assert body["last_halt_reset"] is None
    assert [item["action"] for item in body["items"] if "action" in item] == ["buy"]  # newest first
    assert body["items"][0]["_line_no"] == 2  # the halt line is the newest event

    filtered = client.get("/api/audit", params={"action": "buy", "phase": "execute"}).json()
    assert filtered["total_matching"] == 1
    assert filtered["items"][0]["ticker"] == "BTC-USD"


def test_audit_missing_log_is_empty_not_error(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    body = client.get("/api/audit").json()
    assert body["items"] == []
    assert body["total_lines"] == 0
    assert body["halted_today"] is False


# --- history & backtest ------------------------------------------------------------

_SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"


def _memory_log_text() -> str:
    entries = [
        "[2026-09-29 | BTC-USD | Buy | pending]\n\nDECISION:\nRating: Buy\n",
        "[2026-09-20 | BTC-USD | Underweight | -1.2% | -0.5% | 5d | resolved:2026-09-27]"
        "\n\nDECISION:\nRating: Underweight\n\nREFLECTION:\nwrong direction\n",
        "[2026-09-28 | ETH-USD | Hold | pending]\n\nDECISION:\nRating: Hold\n",
    ]
    return _SEPARATOR.join(entries) + _SEPARATOR


def test_history_parses_decision_log_and_filters(client, tmp_path):
    log = tmp_path / "trading_memory.md"
    log.write_text(_memory_log_text(), encoding="utf-8")
    set_config({"memory_log_path": str(log), "results_dir": str(tmp_path / "results")})

    body = client.get("/api/history").json()
    assert body["total"] == 3
    assert body["by_ticker"] == {"BTC-USD": 2, "ETH-USD": 1}
    newest = body["items"][0]
    assert (newest["date"], newest["ticker"], newest["rating"]) == ("2026-09-29", "BTC-USD", "Buy")
    assert newest["pending"] is True
    assert newest["decision_excerpt"].startswith("Rating: Buy")
    assert set(newest["run_artifacts"]) == {"state_log", "reports_dir", "saved_report"}

    pending = client.get("/api/history", params={"pending_only": "true"}).json()
    assert pending["total"] == 2
    per_ticker = client.get("/api/history", params={"ticker": "ETH-USD"}).json()
    assert per_ticker["total"] == 1


def test_history_warns_about_unparseable_blocks(client, tmp_path):
    log = tmp_path / "trading_memory.md"
    log.write_text(
        "[2026-09-29 | BTC-USD | Buy | pending]\n\nDECISION:\nRating: Buy\n"
        + _SEPARATOR + "garbage without a tag line" + _SEPARATOR,
        encoding="utf-8",
    )
    set_config({"memory_log_path": str(log)})
    body = client.get("/api/history").json()
    assert body["total"] == 1
    assert "1 log entries could not be parsed" in body["warning"]


def test_backtest_lists_runs_and_serves_detail(client, tmp_path):
    results = tmp_path / "results"
    run_dir = results / "backtest" / "20260928_101500"
    run_dir.mkdir(parents=True)
    (run_dir / "trading_memory.md").write_text(
        "[2026-09-20 | BTC-USD | Buy | +1.0% | +0.5% | 5d]\n\nDECISION:\nRating: Buy\n"
        + _SEPARATOR,
        encoding="utf-8",
    )
    set_config({"results_dir": str(results)})

    listing = client.get("/api/backtest").json()
    assert [run["run_id"] for run in listing["runs"]] == ["20260928_101500"]
    summary = listing["runs"][0]
    assert summary["entries"] == 1 and summary["resolved"] == 1
    assert summary["by_rating"]["Buy"]["hit_rate"] == 1.0

    detail = client.get("/api/backtest", params={"run_id": "20260928_101500"}).json()
    assert len(detail["entries"]) == 1
    assert detail["entries"][0]["alpha"] == "+0.5%"

    missing = client.get("/api/backtest", params={"run_id": "nope"})
    assert missing.status_code == 404


def test_backtest_empty_results_dir_is_empty_list(client, tmp_path):
    set_config({"results_dir": str(tmp_path / "results")})
    body = client.get("/api/backtest").json()
    assert body["runs"] == []


# --- daily jobs (§5) ------------------------------------------------------------


def _state_log(tmp_path: Path, rating: str = "Buy") -> Path:
    """A replayable full_states_log for BTC-USD dated today (runs.py machinery)."""
    today = _today()
    log_dir = tmp_path / "results" / "BTC-USD" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    state_log = log_dir / f"full_states_log_{today}.json"
    state_log.write_text(json.dumps({
        "company_of_interest": "BTC-USD", "trade_date": today,
        "market_report": "m", "sentiment_report": "s", "news_report": "n",
        "fundamentals_report": "",
        "investment_debate_state": {
            "bull_history": "b", "bear_history": "r",
            "history": "", "current_response": "", "judge_decision": "j",
        },
        "trader_investment_decision": "t", "investment_plan": "p",
        "risk_debate_state": {
            "aggressive_history": "a", "conservative_history": "c",
            "neutral_history": "n2", "history": "", "judge_decision": "pj",
        },
        "final_trade_decision": f"Final Decision:\nRating: {rating}",
    }, ensure_ascii=False), encoding="utf-8")
    return state_log


def _await_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/daily/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"daily job {job_id} did not finish within {timeout}s")


def test_daily_mock_replays_signal_and_enqueues_plan(client, tmp_path):
    state_log = _state_log(tmp_path, rating="Buy")
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})

    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    assert created.status_code == 202
    body = created.json()
    assert body["status"] == "queued"
    assert body["mode"] == "mock"
    assert body["tickers"] == ["BTC-USD"]
    assert body["detail_url"] == f"/api/daily/{body['job_id']}"

    job = _await_job(client, body["job_id"])
    assert job["status"] == "completed"
    assert job["finished_at"]
    (result,) = job["results"]
    assert result["ticker"] == "BTC-USD"
    assert result["signal"] == "Buy"
    assert result["mock_source"] == f"replay:{state_log}"
    assert result["reason"] == "awaiting approval"
    assert result["error"] is None

    plan = result["plan"]
    assert plan["side"] == "buy"
    assert plan["ccxt_symbol"] == "BTC/USDT"
    assert plan["price_est"] == daily_mod.MOCK_PRICE  # stub venue fixture, no network
    assert plan["cost"] == pytest.approx(daily_mod.MOCK_CASH * 0.25)
    assert plan["quantity"] == pytest.approx(daily_mod.MOCK_CASH * 0.25 / daily_mod.MOCK_PRICE)
    assert plan["market"] == "spot"
    assert plan["mode_at_plan"] == "dry"

    # per-coin enqueue with the full item, including the job back-reference
    items = client.get("/api/approvals").json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["daily_job_id"] == body["job_id"]
    assert item["ticker"] == "BTC-USD"
    assert item["side"] == "buy"
    assert item["mock"] is True
    assert item["status"] == "pending"
    assert result["approval_id"] == item["id"]

    # the job never executes: plan + awaiting-approval lines only, no execute line
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "buy"' in engine_log and '"phase": "plan"' in engine_log
    assert '"action": "awaiting_approval"' in engine_log
    assert '"phase": "execute"' not in engine_log


def test_daily_mock_synthetic_hold_enqueues_nothing(client, tmp_path):
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})

    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job = _await_job(client, created.json()["job_id"])
    (result,) = job["results"]
    assert result["signal"] == "Hold"
    assert result["mock_source"] == "synthetic"
    assert result["plan"] is None
    assert result["approval_id"] is None
    assert result["reason"] == "Hold: no trade"
    assert client.get("/api/approvals").json()["pending_count"] == 0


def test_daily_defaults_to_watchlist_and_validates_input(client, tmp_path):
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    client.delete("/api/watchlist/BTC-USD", headers=_JSON)
    client.delete("/api/watchlist/ETH-USD", headers=_JSON)
    client.post("/api/watchlist", json={"symbol": "sol-usd"})

    created = client.post("/api/daily", json={"mode": "mock"})  # no tickers → the watchlist
    assert created.status_code == 202
    assert created.json()["tickers"] == ["SOL-USD"]
    job = _await_job(client, created.json()["job_id"])
    (result,) = job["results"]
    assert result["signal"] == "Hold"  # synthetic fixture for SOL-USD

    for bad_body in (
        {"mode": "replay"},
        {"tickers": "BTC-USD"},            # not a list
        {"tickers": [42]},
        {"tickers": ["   ", ""]},          # empty after normalization
    ):
        response = client.post("/api/daily", json=bad_body)
        assert response.status_code == 400, bad_body
        assert response.json()["error"]["code"] == "validation_error"

    missing = client.get("/api/daily/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_daily_normalizes_tickers_at_the_boundary(client, tmp_path):
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    created = client.post("/api/daily", json={"tickers": [" BTC-USD ", "BTC-USD", ""],
                                             "mode": "mock"})
    assert created.status_code == 202
    assert created.json()["tickers"] == ["BTC-USD"]  # strip, dedupe keep order, drop empties
    _await_job(client, created.json()["job_id"])


def test_daily_and_runs_share_one_worker_lock(client):
    active_run = runs_mod.RunState(
        run_id="r_active", ticker="BTC-USD", trade_date=_today(),
        asset_type="crypto", mode="mock", checkpoint=None, created_at=_today(),
    )
    active_run.status = "running"
    runs_mod._runs["r_active"] = active_run
    response = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["run_id"] == "r_active"

    runs_mod._runs.clear()
    active_job = daily_mod.DailyJobState(
        job_id="d_active", mode="mock", tickers=["BTC-USD"], created_at=_today(),
    )
    active_job.status = "running"
    daily_mod._jobs["d_active"] = active_job

    run_while_busy = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto",
    })
    assert run_while_busy.status_code == 409
    assert run_while_busy.json()["error"]["details"]["job_id"] == "d_active"

    job_while_busy = client.post("/api/daily", json={"tickers": ["ETH-USD"], "mode": "mock"})
    assert job_while_busy.status_code == 409
    assert job_while_busy.json()["error"]["details"]["job_id"] == "d_active"


def test_daily_mock_plan_cannot_be_approved(client, tmp_path):
    _state_log(tmp_path, rating="Buy")
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl"),
                "exec_live": True})  # even with the gate armed
    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job = _await_job(client, created.json()["job_id"])
    approval_id = job["results"][0]["approval_id"]

    response = client.post(f"/api/approvals/{approval_id}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mock_not_executable"
    assert client.get("/api/approvals").json()["items"][0]["status"] == "pending"

    rejected = client.post(f"/api/approvals/{approval_id}/reject", json={"reason": "demo"})
    assert rejected.status_code == 200


# --- portfolio (§6) ----------------------------------------------------------------


def _trade(when: str, action: str, qty: float, price: float, *,
           phase: str = "execute", confirmed: bool = True) -> dict:
    return {"timestamp": when, "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT",
            "signal": "Buy", "action": action, "qty": qty, "price_est": price,
            "mode": "dry", "confirmed": confirmed, "order_id": None,
            "phase": phase, "market": "spot", "leverage": 1.0}


def test_portfolio_dry_mode_reconstructs_positions_from_audit(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.0012, 82000.0)),
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.0008, 85000.0)),
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})  # exec_live stays False → dry, no ccxt

    body = client.get("/api/portfolio").json()
    assert body["generated_at"].endswith("Z")
    assert body["currency"] == "USDT"
    assert body["cash"] is None
    (position,) = body["positions"]
    assert position["ticker"] == "BTC-USD"
    assert position["quantity"] == pytest.approx(0.002)
    assert position["average_price"] == pytest.approx((0.0012 * 82000 + 0.0008 * 85000) / 0.002)
    assert position["basis_source"] == "fifo"
    assert position["marked_price"] is None
    assert position["market_value"] is None
    assert position["unrealized_pnl"] is None
    assert body["equity_est"] == 0.0  # unmarked positions count 0 (risk.py:381-403)
    assert body["realized_pnl"]["total"] == 0
    assert body["realized_pnl"]["closed_trades"] == 0
    assert body["halted_today"] is False
    assert body["warnings"]  # honest about the reconstruction
    assert body["stale_after"] == 30


def test_portfolio_dry_empty_audit_is_valid_empty_state(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    body = client.get("/api/portfolio").json()
    assert body["cash"] is None
    assert body["positions"] == []
    assert body["equity_est"] is None
    assert body["realized_pnl"] == {
        "total": 0, "today": 0, "by_ticker": {}, "closed_trades": 0,
        "unknown_basis_closes": 0, "consecutive_losses": 0,
    }
    assert body["halted_today"] is False
    assert body["warnings"] == []


def test_portfolio_realized_pnl_is_fifo_replay_of_the_audit_log(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.001, 80000.0)),
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.001, 84000.0)),
        json.dumps(_trade(f"{today}T06:00:00+00:00", "sell", 0.0012, 86000.0)),
        # plan lines never count twice (risk.py:271-281)
        json.dumps(_trade(f"{today}T07:00:00+00:00", "sell", 0.0008, 90000.0, phase="plan")),
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/portfolio").json()
    expected = (86000 - 80000) * 0.001 + (86000 - 84000) * 0.0002  # FIFO: oldest lots first
    assert body["realized_pnl"]["total"] == pytest.approx(expected)
    assert body["realized_pnl"]["today"] == pytest.approx(expected)
    assert body["realized_pnl"]["by_ticker"] == {"BTC-USD": pytest.approx(expected)}
    assert body["realized_pnl"]["closed_trades"] == 1
    assert body["realized_pnl"]["unknown_basis_closes"] == 0

    (position,) = body["positions"]  # the leftover 0.0008 lot @ 84000 stays open
    assert position["quantity"] == pytest.approx(0.0008)
    assert position["average_price"] == pytest.approx(84000.0)


def test_portfolio_counts_unknown_basis_and_loss_streak(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "sell", 0.001, 80000.0)),  # no open lot
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.001, 90000.0)),
        json.dumps(_trade(f"{today}T06:00:00+00:00", "sell", 0.001, 85000.0)),  # -5 close
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/portfolio").json()
    assert body["realized_pnl"]["unknown_basis_closes"] == 1
    assert body["realized_pnl"]["closed_trades"] == 1
    assert body["realized_pnl"]["total"] == pytest.approx(-5.0)
    assert body["realized_pnl"]["consecutive_losses"] == 1
    assert body["positions"] == []


def test_portfolio_live_mode_reads_venue_and_marks_positions(client, tmp_path, monkeypatch):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text(
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.001, 80000.0)) + "\n",
        encoding="utf-8",
    )
    set_config({"exec_live": True, "exec_log_path": str(audit_file)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")  # second gate half

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_FakeExchange)  # price 83078, cash 9500

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["currency"] == "USDT"
    (position,) = body["positions"]
    assert position["ticker"] == "BTC-USD"
    assert position["quantity"] == pytest.approx(0.001)
    assert position["average_price"] == pytest.approx(80000.0)
    assert position["basis_source"] == "fifo"
    assert position["marked_price"] == pytest.approx(83078.0)
    assert position["market_value"] == pytest.approx(83.078)
    assert position["unrealized_pnl"] == pytest.approx((83078.0 - 80000.0) * 0.001)
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    assert body["warnings"] == []


def test_portfolio_live_position_without_basis_or_mark_is_honest(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    exchange = _FakeExchange()
    exchange.fetch_ticker = lambda symbol: {}  # venue answers but cannot mark

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    body = client.get("/api/portfolio").json()
    (position,) = body["positions"]
    assert position["average_price"] is None  # no log → unknown basis (risk.py:15-17)
    assert position["basis_source"] is None
    assert position["marked_price"] is None
    assert position["unrealized_pnl"] is None
    assert body["equity_est"] == 9500.0  # unmarked counts 0, cash still counts
    assert any("cannot be marked" in warning for warning in body["warnings"])


def test_portfolio_live_venue_failure_is_503(client, tmp_path, monkeypatch):
    set_config({"exec_live": True})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _DeadVenue:
        def fetch_balance(self):
            raise RuntimeError("venue down")

        def fetch_ticker(self, symbol):
            raise RuntimeError("venue down")

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_DeadVenue)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "exchange_unreachable"


def test_portfolio_unreadable_audit_log_is_503(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path)})  # a directory: OSError on read
    response = client.get("/api/portfolio")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_log_unreadable"
