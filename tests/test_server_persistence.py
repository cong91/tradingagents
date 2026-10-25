"""Persistence tests: the API's own state must outlive the process.

The daily job registry and the run registry used to be process-global dicts,
which produced the bug these tests pin down — a job that is running is
invisible, and once the process is gone the record is too, so the worker lock
derived from that status can never be released.

Each test drops the in-memory registry to simulate a restart and asserts the
API still answers from ``control.db``. Unit-scoped like the rest of the server
suite: tmp data directory, no sockets, no LLM, no vendors.
"""

import json
import socket
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    daily as daily_mod,
    job_store,
    run_store,
    runs as runs_mod,
)
from server.main import app
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit

_JSON = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests (same shadow as
    tests/test_server_api.py: the event loop's Windows self-pipe is the only
    whitelisted socket; every other connect still refuses)."""
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
def _server_state(tmp_path, monkeypatch, postgres_schema):
    """Point all server state — files and database — at tmp."""
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})
    # Each test gets a fresh file, so the once-per-path schema guard must reset
    # with it — otherwise the second test would skip CREATE TABLE on a new file.


@pytest.fixture()
def client(_server_state):
    with TestClient(app) as test_client:
        yield test_client


def _drain_sse(client: TestClient, url: str, headers: dict | None = None) -> list[dict]:
    events = []
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def _await_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/daily/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled", "interrupted"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"daily job {job_id} did not finish within {timeout}s")


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


def _plant_running_job(job_id: str = "d_running") -> None:
    """A job this process is working on, planted in both registries."""
    job = daily_mod.DailyJobState(job_id=job_id, mode="mock", tickers=["BTC-USD"],
                                  created_at=datetime.now(timezone.utc).isoformat())
    job.status = "running"
    daily_mod._jobs[job_id] = job
    daily_mod._persist(job)


# --- the bug: a running job nobody can see --------------------------------


def test_daily_list_reports_the_active_job(client):
    """The UI's running banner reads this field — without it the operator only
    learns a job exists when the next one is refused with a 409."""
    _plant_running_job()
    body = client.get("/api/daily").json()
    assert body["active_job_id"] == "d_running"
    assert [item["job_id"] for item in body["items"]] == ["d_running"]
    assert body["items"][0]["status"] == "running"


def test_cancelling_a_job_releases_the_worker_lock(client):
    _plant_running_job()
    refused = client.post("/api/daily", json={"tickers": ["ETH-USD"], "mode": "mock"})
    assert refused.status_code == 409  # the phantom this feature exists to expose

    cancelled = client.post("/api/daily/d_running/cancel", headers=_JSON)
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["finished_at"]

    assert client.get("/api/daily").json()["active_job_id"] is None
    started = client.post("/api/daily", json={"tickers": ["ETH-USD"], "mode": "mock"})
    assert started.status_code == 202
    _await_job(client, started.json()["job_id"])


def test_cancel_refuses_a_job_that_is_not_running(client):
    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job_id = created.json()["job_id"]
    _await_job(client, job_id)
    response = client.post(f"/api/daily/{job_id}/cancel", headers=_JSON)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["status"] == "completed"

    unknown = client.post("/api/daily/d_nope/cancel", headers=_JSON)
    assert unknown.status_code == 404


def test_orphaned_job_is_reconciled_at_startup(client):
    """A crash leaves a job at ``running`` with no thread behind it. Without
    reconciliation that row answers 409 to every future job, forever."""
    _plant_running_job("d_orphan")
    daily_mod._jobs.clear()  # the process died; the record did not

    assert daily_mod.reconcile() == ["d_orphan"]
    stored = client.get("/api/daily/d_orphan").json()
    assert stored["status"] == "interrupted"
    assert stored["finished_at"]
    assert client.get("/api/daily").json()["active_job_id"] is None

    started = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    assert started.status_code == 202
    _await_job(client, started.json()["job_id"])


# --- the record the operator reads ----------------------------------------


def test_job_and_its_agent_output_survive_a_restart(client, tmp_path):
    _state_log(tmp_path, rating="Buy")
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job_id = created.json()["job_id"]
    job = _await_job(client, job_id)
    assert job["results"][0]["signal"] == "Buy"

    daily_mod._jobs.clear()  # restart: only the database is left

    restored = client.get(f"/api/daily/{job_id}")
    assert restored.status_code == 200
    body = restored.json()
    assert body["status"] == "completed"
    assert body["mode"] == "mock"
    assert body["tickers"] == ["BTC-USD"]
    result = body["results"][0]
    assert result["ticker"] == "BTC-USD"
    assert result["signal"] == "Buy"
    assert result["plan"]["side"] == "buy"  # the plan the agent produced, in full
    assert result["approval_id"]
    assert result["mock_source"].startswith("replay:")


def test_finish_persists_terminal_row_before_exposing_it(monkeypatch):
    """The API reads the live job first: flipping its status terminal before
    the database write lets a poll see "completed" while the restart record
    still says "running"."""
    job = daily_mod.DailyJobState(
        job_id="d_order", mode="mock", tickers=["BTC-USD"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    job.status = "running"
    observed: list[tuple[str, str]] = []
    real_persist = job_store.persist

    def spy(payload):
        observed.append((payload["status"], job.status))
        real_persist(payload)

    monkeypatch.setattr(daily_mod.job_store, "persist", spy)

    daily_mod._finish(job)

    assert observed == [("completed", "running")]
    assert job.status == "completed"
    assert job_store.load("d_order")["status"] == "completed"


def test_job_progress_heartbeat_and_current_ticker_survive_restart(client):
    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job_id = created.json()["job_id"]
    body = _await_job(client, job_id)

    assert body["last_progress_at"]
    assert body["current_ticker"] is None
    assert body["last_progress_at"] >= body["created_at"]

    daily_mod._jobs.clear()
    restored = client.get(f"/api/daily/{job_id}").json()
    assert restored["last_progress_at"] == body["last_progress_at"]
    assert restored["current_ticker"] is None


def test_running_job_persists_current_ticker_and_heartbeat(client):
    job = daily_mod.DailyJobState(
        job_id="d_progress",
        mode="mock",
        tickers=["BTC-USD"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    job.status = "running"
    daily_mod._jobs[job.job_id] = job
    daily_mod._touch_progress(job, current_ticker="BTC-USD")

    body = client.get(f"/api/daily/{job.job_id}").json()
    assert body["current_ticker"] == "BTC-USD"
    assert body["last_progress_at"]


def test_running_job_heartbeat_refreshes_during_slow_coin(client, monkeypatch):
    job = daily_mod.DailyJobState(
        job_id="d_slow_progress",
        mode="mock",
        tickers=["BTC-USD"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    job.status = "running"
    daily_mod._jobs[job.job_id] = job
    monkeypatch.setattr(daily_mod, "_PROGRESS_HEARTBEAT_SECONDS", 0.01)
    started = time.monotonic()

    def slow_process(*_args):
        time.sleep(0.04)
        return {"ticker": "BTC-USD", "asset_type": "crypto", "signal": "Hold"}

    monkeypatch.setattr(daily_mod, "_process_coin", slow_process)
    daily_mod._run_coin_loop(job, object(), {})

    assert time.monotonic() - started >= 0.04
    stored = client.get(f"/api/daily/{job.job_id}").json()
    assert stored["current_ticker"] is None
    assert stored["last_progress_at"] > job.created_at


def test_job_history_lists_newest_first_with_counts(client):
    for ticker in ("BTC-USD", "ETH-USD"):
        created = client.post("/api/daily", json={"tickers": [ticker], "mode": "mock"})
        _await_job(client, created.json()["job_id"])
    body = client.get("/api/daily?page_size=10").json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert all(item["results_count"] == 1 for item in body["items"])
    assert all(item["mode"] == "mock" for item in body["items"])
    # Newest first: the second job's timestamp is >= the first's.
    assert body["items"][0]["created_at"] >= body["items"][1]["created_at"]

    filtered = client.get("/api/daily?status=failed").json()
    assert filtered["total"] == 0
    assert client.get("/api/daily?status=nonsense").status_code == 400


def test_auto_job_screening_survives_a_restart(client):
    created = client.post("/api/daily", json={"auto": True, "mode": "mock", "top_n": 3})
    job_id = created.json()["job_id"]
    _await_job(client, job_id)
    daily_mod._jobs.clear()

    body = client.get(f"/api/daily/{job_id}").json()
    assert body["auto"] is True
    assert body["top_n"] == 3
    assert len(body["screening"]["candidates"]) == 3
    assert body["asset_types"]["NVDA"] == "stock"


# --- runs: the agent event stream -----------------------------------------


def test_run_events_replay_from_the_database_after_a_restart(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "MOCK_EVENT_DELAY", 0.0)
    set_config({"results_dir": str(tmp_path / "results")})
    created = client.post("/api/runs", json={
        "ticker": "BTC-USD", "trade_date": _today(), "asset_type": "crypto", "mode": "mock",
    })
    run_id = created.json()["run_id"]
    live_types = [event["type"] for event in _drain_sse(client, f"/api/runs/{run_id}/events")]
    assert live_types[0] == "run_started" and live_types[-1] == "run_completed"

    runs_mod._runs.clear()  # restart: the SSE buffer is gone, the log is not

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["signal"] == "Hold"

    replayed = _drain_sse(client, f"/api/runs/{run_id}/events")
    assert [event["type"] for event in replayed] == live_types
    assert replayed[0]["payload"]["analysts"] == ["market", "social", "news"]


def test_run_list_reports_the_active_run(client, monkeypatch, tmp_path):
    monkeypatch.setattr(runs_mod, "MOCK_EVENT_DELAY", 0.0)
    set_config({"results_dir": str(tmp_path / "results")})
    run = runs_mod.RunState(
        run_id="r_active", ticker="BTC-USD", trade_date=_today(),
        asset_type="crypto", mode="mock", checkpoint=None, created_at=_today(),
    )
    run.status = "running"
    runs_mod._runs["r_active"] = run
    runs_mod._persist(run)

    body = client.get("/api/runs").json()
    assert body["active_run_id"] == "r_active"
    assert [item["run_id"] for item in body["items"]] == ["r_active"]


def test_orphaned_run_is_reconciled_at_startup(client):
    run = runs_mod.RunState(
        run_id="r_orphan", ticker="BTC-USD", trade_date=_today(),
        asset_type="crypto", mode="mock", checkpoint=None, created_at=_today(),
    )
    run.status = "running"
    run_store.persist_run({
        "run_id": run.run_id, "ticker": run.ticker, "trade_date": run.trade_date,
        "asset_type": run.asset_type, "mode": run.mode, "status": run.status,
        "signal": None, "is_review": False, "error": None, "report_paths": None,
        "analysts": list(run.analysts), "created_at": run.created_at, "finished_at": None,
    })

    run_store.append_event("r_orphan", {
        "seq": 1,
        "ts": f"{_today()}T00:00:01+00:00",
        "type": "message",
        "role": "analyst",
        "stage": "analyst",
        "agent": "Market Analyst",
        "replay": False,
        "payload": {"content": "partial evidence"},
    })

    assert runs_mod.reconcile() == ["r_orphan"]
    detail = client.get("/api/runs/r_orphan").json()
    assert detail["status"] == "interrupted"
    assert detail["error"]["code"] == "interrupted"
    assert client.get("/api/runs").json()["active_run_id"] is None
    events = _drain_sse(client, "/api/runs/r_orphan/events")
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["type"] == "message"
    assert events[-1]["type"] == "run_interrupted"
    assert events[-1]["payload"]["error"]["code"] == "interrupted"
    resumed = _drain_sse(
        client,
        "/api/runs/r_orphan/events",
        headers={"Last-Event-ID": "1"},
    )
    assert [event["seq"] for event in resumed] == [2]
    assert run_store.load_events("r_orphan", after_seq=1)[0]["type"] == "run_interrupted"
    assert runs_mod.reconcile() == []
    assert [event["seq"] for event in run_store.load_events("r_orphan")] == [1, 2]


def test_job_results_are_replaced_not_duplicated(client, tmp_path):
    """The whole job is rewritten on every state change — the history must not
    accumulate one result row per write."""
    _state_log(tmp_path, rating="Buy")
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job_id = created.json()["job_id"]
    _await_job(client, job_id)

    stored = job_store.load(job_id)
    for _ in range(3):
        job_store.persist(stored)  # the same job written again, unchanged

    assert len(job_store.load(job_id)["results"]) == 1
    assert client.get(f"/api/daily/{job_id}").json()["results"][0]["approval_id"]
