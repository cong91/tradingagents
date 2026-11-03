"""Control-panel runs API tests."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import runs as runs_mod
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


def _drain_sse(client: TestClient, url: str, headers: dict | None = None) -> list[dict]:
    events = []
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


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
