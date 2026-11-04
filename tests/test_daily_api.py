"""Control-panel daily-job API tests."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import approvals as approvals_mod, daily as daily_mod, runs as runs_mod
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit

# §0 CSRF: every state-changing method must send application/json — DELETE too.
_JSON = {"Content-Type": "application/json"}


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


def test_daily_auto_mock_screens_then_replays_pipeline(client, tmp_path):
    """Auto mode (§5 rev 4): mock screens deterministically, then the pipeline
    runs on the screened candidates — one click, no watchlist, no network."""
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})

    created = client.post("/api/daily", json={"auto": True, "mode": "mock", "top_n": 3})
    assert created.status_code == 202
    assert created.json()["tickers"] == []  # decided by the screen inside the worker

    job = _await_job(client, created.json()["job_id"])
    assert job["status"] == "completed"
    screening = job["screening"]
    assert screening["mock"] is True
    assert screening["universe_count"] == 3
    tickers = [candidate["ticker"] for candidate in screening["candidates"]]
    assert tickers == job["tickers"] == ["BTC-USD", "ETH-USD", "NVDA"]

    # the pipeline ran on the screened candidates in rank order, stock advisory
    assert [result["asset_type"] for result in job["results"]] == ["crypto", "crypto", "stock"]
    btc, _eth, nvda = job["results"]
    assert btc["signal"] == "Hold"  # synthetic fixture, no state log → Hold
    assert nvda["asset_type"] == "stock"
    assert nvda["plan"] is None
    assert nvda["approval_id"] is None
    assert "advisory only" in nvda["reason"]
    assert client.get("/api/approvals").json()["pending_count"] == 0


def test_daily_auto_validates_body_and_never_takes_tickers(client):
    set_config({"results_dir": "results", "exec_log_path": "engine-audit.jsonl"})
    for bad_body in (
        {"auto": "yes"},          # not a boolean
        {"auto": True, "top_n": 0},
        {"auto": True, "top_n": 21},
        {"auto": True, "top_n": "3"},
        {"auto": True, "mode": "mock", "tickers": ["BTC-USD"]},  # screen picks, not you
    ):
        response = client.post("/api/daily", json=bad_body)
        assert response.status_code == 400, bad_body
        assert response.json()["error"]["code"] == "validation_error"


def test_daily_auto_off_set_crypto_is_advisory(client, tmp_path, monkeypatch):
    """A crypto-universe ticker outside the closed executable set (QNT-USD)
    keeps the crypto analysts but cannot plan: its result is advisory, never a
    bridge ValueError."""
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    monkeypatch.setattr(daily_mod, "MOCK_AUTO_CANDIDATES", (
        {"ticker": "QNT-USD", "asset_type": "crypto", "score": 3.0,
         "last_close": 294.5, "momentum_pct": 12.0, "trend_pct": 4.0,
         "volume_ratio": 1.1, "reasons": ["+12.0% trong 20 phiên"]},
    ))

    created = client.post("/api/daily", json={"auto": True, "mode": "mock"})
    assert created.status_code == 202
    job = _await_job(client, created.json()["job_id"])
    (result,) = job["results"]
    assert result["asset_type"] == "crypto"
    assert result["plan"] is None
    assert result["approval_id"] is None
    assert "outside the executable crypto set" in result["reason"]
    assert client.get("/api/approvals").json()["pending_count"] == 0


def test_daily_mock_buy_plan_carries_bracket_and_approval_keeps_it(client, tmp_path):
    """A Buy plan gets protective exits attached (mock: fixed demo distances),
    and the approval item + rebuilt plan both carry them so approve → execute
    can place the exchange-side bracket."""
    _state_log(tmp_path, rating="Buy")
    set_config({"results_dir": str(tmp_path / "results"),
                "exec_log_path": str(tmp_path / "engine-audit.jsonl")})

    created = client.post("/api/daily", json={"tickers": ["BTC-USD"], "mode": "mock"})
    job = _await_job(client, created.json()["job_id"])
    (result,) = job["results"]
    plan = result["plan"]
    assert plan["side"] == "buy"
    price = plan["price_est"]
    assert plan["stop_loss"] == pytest.approx(price * 0.96)
    assert plan["take_profit"] == pytest.approx(price * 1.08)

    items = client.get("/api/approvals").json()["items"]
    (item,) = items
    assert item["stop_loss"] == pytest.approx(price * 0.96)
    assert item["take_profit"] == pytest.approx(price * 1.08)

    rebuilt = approvals_mod._rebuild_plan(item)
    assert rebuilt.stop_loss == pytest.approx(price * 0.96)
    assert rebuilt.take_profit == pytest.approx(price * 1.08)


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
