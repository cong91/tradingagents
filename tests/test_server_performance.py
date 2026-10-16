"""Tests for the performance endpoint (server/performance.py).

Unit-scoped like tests/test_server_api.py: the ASGI app under TestClient,
the engine audit log pointed at tmp JSONL files, marks injected through the
price_fetcher seam — no network, no LLM, no vendors.
"""

import json
import socket
import weakref
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import paths as server_paths, performance as performance_mod
from server.contract import install_error_handlers
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests (same shadow
    as tests/test_server_api.py: the event loop's Windows self-pipe must be
    whitelisted; every other connect still refuses)."""
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


@pytest.fixture(autouse=True)
def _server_state(tmp_path, monkeypatch):
    """Point the server's file state at tmp (TestClient's lifespan seeds the
    watchlist, so the real server/data/ must stay untouched)."""
    from tradingagents.default_config import DEFAULT_CONFIG

    saved_watchlist = DEFAULT_CONFIG.get("exec_watchlist")
    monkeypatch.setattr(server_paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_paths, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(server_paths, "APPROVALS_PATH", tmp_path / "approvals.jsonl")
    monkeypatch.setattr(server_paths, "SERVER_AUDIT_PATH", tmp_path / "server-audit.jsonl")
    yield
    DEFAULT_CONFIG["exec_watchlist"] = saved_watchlist


@pytest.fixture()
def client(_server_state):
    """TestClient over a local composition of the performance router.

    Registering the router in server/main.py is a separate integration step
    owned elsewhere (the registration line ships with this module), so the
    route tests mount the router exactly the way main.py does — same error
    handlers, same envelope — instead of depending on that edit.
    """
    local_app = FastAPI()
    install_error_handlers(local_app)
    local_app.include_router(performance_mod.router)
    with TestClient(local_app) as test_client:
        yield test_client


def _fill(ticker: str, action: str, qty: float | None, price: float | None, ts: str,
          mode: str = "dry", **extra) -> dict:
    """One execute-phase audit line in the bridge's shape (bridge.py:647-668)."""
    return {"timestamp": ts, "ticker": ticker, "action": action, "qty": qty,
            "price_est": price, "mode": mode, "phase": "execute",
            "order_id": f"order-{ts}", **extra}


def _write_audit(tmp_path: Path, events: list[dict]) -> Path:
    audit_file = tmp_path / "engine-audit.jsonl"
    audit_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    set_config({"exec_log_path": str(audit_file)})
    return audit_file


def _assert_empty_state(body: dict) -> None:
    assert body["summary"] == {
        "closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
        "avg_win_pct": 0.0, "avg_loss_pct": 0.0, "expectancy_pct": 0.0,
    }
    assert body["open_positions"] == []
    assert body["brackets"] == {"placed": 0, "failed": 0}
    assert body["recent_orders"] == []
    assert body["generated_at"].endswith("Z")


def test_performance_summary_positions_brackets_and_recent_orders(
    client, tmp_path, monkeypatch
):
    _write_audit(tmp_path, [
        # Plan-phase line: never a fill, never in recent_orders.
        {"timestamp": "2026-10-01T00:30:00Z", "ticker": "BTC-USD", "action": "buy",
         "qty": 0.001, "price_est": 50000.0, "mode": "dry", "phase": "plan"},
        _fill("BTC-USD", "buy", 0.001, 50000.0, "2026-10-01T01:00:00Z"),
        _fill("BTC-USD", "sell", 0.001, 55000.0, "2026-10-01T02:00:00Z"),  # +10%
        _fill("BTC-USD", "buy", 0.002, 50000.0, "2026-10-01T03:00:00Z"),  # stays open
        _fill("ETH-USD", "buy", 0.1, 2000.0, "2026-10-01T04:00:00Z"),
        _fill("ETH-USD", "sell", 0.1, 1900.0, "2026-10-01T05:00:00Z"),  # -5%
        _fill("BTC-USD", "bracket_placed", 0.002, 50000.0, "2026-10-01T06:00:00Z",
              order_id="bracket-1"),
        _fill("BTC-USD", "bracket_failed", 0.002, 50000.0, "2026-10-01T06:05:00Z"),
        _fill("BTC-USD", "rejected_by_risk", None, 50000.0, "2026-10-01T07:00:00Z"),
    ])

    def fake_fetch(ticker: str) -> float | None:
        return 52500.0 if ticker == "BTC-USD" else None

    monkeypatch.setattr(performance_mod, "_default_price_fetcher", fake_fetch)
    response = client.get("/api/performance")
    assert response.status_code == 200
    body = response.json()

    summary = body["summary"]
    assert summary["closed_trades"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate_pct"] == pytest.approx(50.0)
    assert summary["avg_win_pct"] == pytest.approx(10.0)
    assert summary["avg_loss_pct"] == pytest.approx(-5.0)
    assert summary["expectancy_pct"] == pytest.approx(2.5)

    assert body["open_positions"] == [{
        "ticker": "BTC-USD",
        "qty": 0.002,
        "entry_price": 50000.0,
        "current_price": 52500.0,
        "unrealized_pct": pytest.approx(5.0),
        "mode": "dry",
        "entry_at": "2026-10-01T03:00:00Z",
    }]

    assert body["brackets"] == {"placed": 1, "failed": 1}

    recent = body["recent_orders"]
    assert len(recent) == 8  # every execute line, plan line excluded
    assert set(recent[0].keys()) == {
        "timestamp", "ticker", "action", "qty", "price_est", "mode", "order_id",
    }
    assert recent[0]["action"] == "rejected_by_risk"  # newest first
    assert recent[0]["qty"] is None
    assert recent[-1]["action"] == "buy"
    assert recent[-1]["timestamp"] == "2026-10-01T01:00:00Z"
    assert not any(row["timestamp"] == "2026-10-01T00:30:00Z" for row in recent)


def test_performance_missing_log_is_all_zero_empty_state(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    response = client.get("/api/performance")
    assert response.status_code == 200
    _assert_empty_state(response.json())


def test_performance_blank_log_is_all_zero_empty_state(client, tmp_path):
    audit_file = tmp_path / "engine-audit.jsonl"
    audit_file.write_text("", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})
    response = client.get("/api/performance")
    assert response.status_code == 200
    _assert_empty_state(response.json())


def test_performance_unreadable_log_is_503(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path)})  # a directory: OSError on read
    response = client.get("/api/performance")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_log_unreadable"


def test_fifo_closes_oldest_lot_first():
    events = [
        _fill("BTC-USD", "buy", 0.1, 100.0, "2026-10-01T01:00:00Z"),
        _fill("BTC-USD", "buy", 0.1, 200.0, "2026-10-01T02:00:00Z"),
        _fill("BTC-USD", "sell", 0.1, 150.0, "2026-10-01T03:00:00Z"),
    ]
    result = performance_mod.compute_performance(events, price_fetcher=lambda ticker: None)
    # 100 -> 150 closes (+50%), not 200 -> 150 (-25%): FIFO, not LIFO.
    assert result["summary"]["closed_trades"] == 1
    assert result["summary"]["wins"] == 1
    assert result["summary"]["avg_win_pct"] == pytest.approx(50.0)
    assert result["open_positions"] == [{
        "ticker": "BTC-USD",
        "qty": 0.1,
        "entry_price": 200.0,
        "current_price": None,
        "unrealized_pct": None,
        "mode": "dry",
        "entry_at": "2026-10-01T02:00:00Z",
    }]


def test_partial_sell_leaves_remaining_lot_open():
    events = [
        _fill("BTC-USD", "buy", 0.002, 50000.0, "2026-10-01T01:00:00Z"),
        _fill("BTC-USD", "sell", 0.001, 60000.0, "2026-10-01T02:00:00Z"),
    ]
    result = performance_mod.compute_performance(events, price_fetcher=lambda ticker: 48000.0)
    assert result["summary"]["closed_trades"] == 1
    assert result["summary"]["avg_win_pct"] == pytest.approx(20.0)
    assert len(result["open_positions"]) == 1
    row = result["open_positions"][0]
    assert row["qty"] == pytest.approx(0.001)
    assert row["entry_price"] == 50000.0
    assert row["unrealized_pct"] == pytest.approx(-4.0)  # marked 48000 vs entry 50000


def test_sell_without_open_lot_is_dropped():
    events = [_fill("BTC-USD", "sell", 0.1, 150.0, "2026-10-01T01:00:00Z")]
    result = performance_mod.compute_performance(events, price_fetcher=lambda ticker: None)
    assert result["summary"]["closed_trades"] == 0
    assert result["summary"]["losses"] == 0
    assert result["open_positions"] == []
    assert result["recent_orders"][0]["action"] == "sell"  # still visible in the execute tail


def test_derivatives_short_round_trip_and_open_mark_are_short_aware():
    events = [
        _fill("BTC-USD", "sell", 0.1, 100.0, "2026-10-01T01:00:00Z", market="derivatives"),
        _fill("BTC-USD", "buy", 0.1, 80.0, "2026-10-01T02:00:00Z", market="derivatives"),
        _fill("ETH-USD", "sell", 0.2, 200.0, "2026-10-01T03:00:00Z", market="derivatives"),
    ]
    result = performance_mod.compute_performance(events, price_fetcher=lambda ticker: 180.0)

    assert result["summary"]["closed_trades"] == 1
    assert result["summary"]["avg_win_pct"] == pytest.approx(20.0)
    assert result["open_positions"] == [{
        "ticker": "ETH-USD",
        "qty": -0.2,
        "entry_price": 200.0,
        "current_price": 180.0,
        "unrealized_pct": pytest.approx(10.0),
        "mode": "dry",
        "entry_at": "2026-10-01T03:00:00Z",
    }]


    events = [
        _fill("BTC-USD", "buy", 0.001, 100.0 + i, f"2026-10-01T{600 + i}:00:00Z")
        for i in range(60)
    ]
    result = performance_mod.compute_performance(events, price_fetcher=lambda ticker: None)
    assert len(result["recent_orders"]) == 50
    assert result["recent_orders"][0]["timestamp"] == "2026-10-01T659:00:00Z"
    assert result["recent_orders"][-1]["timestamp"] == "2026-10-01T610:00:00Z"
    assert result["summary"]["closed_trades"] == 0  # buys only: lots stay open
