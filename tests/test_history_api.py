"""Control-panel decision-history API tests."""

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


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
