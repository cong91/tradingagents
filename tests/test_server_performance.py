"""Tests for the audit-based performance endpoint, without a venue or database."""

import json
import socket
import weakref
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from curl_cffi import requests as curl_requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import performance as performance_mod, postgres
from server.contract import install_error_handlers
from tradingagents.dataflows.config import set_config
from tradingagents.execution.bridge import ExchangeBridge

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Allow the event loop's self-pipe while refusing external connections."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()

    def refuse(*args, **kwargs):
        raise OSError(f"test tried to reach the network: {args[1:] or kwargs}")

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        return refuse(self, address)

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        return refuse(self, address)

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
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(curl_requests.Session, "request", refuse)
    monkeypatch.setattr(curl_requests.AsyncSession, "request", refuse)


@pytest.fixture(autouse=True)
def _no_database_or_venue(monkeypatch):
    def refuse_database(*args, **kwargs):
        pytest.fail("performance must not connect to a database")

    def refuse_venue(*args, **kwargs):
        pytest.fail("performance must not construct an exchange bridge")

    monkeypatch.setattr(postgres, "connect", refuse_database)
    monkeypatch.setattr(psycopg, "connect", refuse_database)
    monkeypatch.setattr(ExchangeBridge, "__init__", refuse_venue)


@pytest.fixture()
def client():
    """Mount the router locally without importing the full app's lifespan."""
    local_app = FastAPI()
    install_error_handlers(local_app)
    local_app.include_router(performance_mod.router)
    with TestClient(local_app) as test_client:
        yield test_client


def _execute_event(
    ticker: str,
    action: str,
    qty: float | None,
    price: float | None,
    ts: str,
    mode: str = "dry",
    **extra,
) -> dict:
    return {
        "timestamp": ts,
        "ticker": ticker,
        "action": action,
        "qty": qty,
        "price_est": price,
        "mode": mode,
        "phase": "execute",
        "order_id": f"order-{ts}",
        **extra,
    }


def _write_audit(tmp_path: Path, events: list[dict]) -> Path:
    audit_file = tmp_path / "engine-audit.jsonl"
    audit_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    set_config({"exec_log_path": str(audit_file)})
    return audit_file


def _assert_empty_state(body: dict) -> None:
    assert set(body) == {
        "generated_at", "skipped_lines", "summary", "open_positions", "brackets", "recent_orders",
    }
    datetime.strptime(body["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert body["skipped_lines"] == 0
    assert body["summary"] is None
    assert body["open_positions"] is None
    assert body["brackets"] == {"placed": 0, "failed": 0}
    assert body["recent_orders"] == []


def test_compute_performance_empty_audit():
    assert performance_mod.compute_performance([]) == {
        "summary": None,
        "open_positions": None,
        "brackets": {"placed": 0, "failed": 0},
        "recent_orders": [],
    }


def test_performance_returns_audit_fields_without_inferred_results(client, tmp_path):
    _write_audit(tmp_path, [
        {"timestamp": "2026-10-01T00:30:00Z", "ticker": "BTC-USD", "action": "buy",
         "qty": 0.001, "price_est": 50000.0, "mode": "dry", "phase": "plan"},
        _execute_event("BTC-USD", "buy", 0.001, 50000.0, "2026-10-01T01:00:00Z"),
        _execute_event("BTC-USD", "sell", 0.001, 55000.0, "2026-10-01T02:00:00Z", mode="live"),
        _execute_event(
            "UNI-USD", "sell", 38, 7.047, "2026-10-01T03:00:00Z", mode="live",
            market="derivatives", leverage=3.0, cost=10.0, ccxt_symbol="UNI/USDT:USDT",
        ),
        _execute_event(
            "UNI/USDT:USDT", "time_stop_closed", 38, 7.057, "2026-10-01T04:00:00Z",
            mode="live", market="derivatives", ccxt_symbol="UNI/USDT:USDT",
            order_id="time-stop-1", reason="maximum holding period reached",
        ),
        _execute_event("BTC-USD", "bracket_placed", 0.002, 50000.0, "2026-10-01T05:00:00Z"),
        _execute_event("BTC-USD", "bracket_failed", 0.002, 50000.0, "2026-10-01T06:00:00Z"),
        _execute_event("BTC-USD", "rejected_by_risk", None, 50000.0, "2026-10-01T07:00:00Z"),
    ])

    response = client.get("/api/performance")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "generated_at", "skipped_lines", "summary", "open_positions", "brackets", "recent_orders",
    }
    datetime.strptime(body["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert body["skipped_lines"] == 0
    assert body["summary"] is None
    assert body["open_positions"] is None
    assert body["brackets"] == {"placed": 1, "failed": 1}

    recent = body["recent_orders"]
    assert [row["action"] for row in recent] == [
        "rejected_by_risk", "bracket_failed", "bracket_placed", "time_stop_closed",
        "sell", "sell", "buy",
    ]
    assert all(set(row) == {
        "timestamp", "ticker", "ccxt_symbol", "action", "qty", "price_est", "market",
        "leverage", "cost", "mode", "order_id",
    } for row in recent)
    assert recent[0]["qty"] is None
    assert recent[0]["market"] == "spot"
    assert recent[3] == {
        "timestamp": "2026-10-01T04:00:00Z",
        "ticker": "UNI/USDT:USDT",
        "ccxt_symbol": "UNI/USDT:USDT",
        "action": "time_stop_closed",
        "qty": 38,
        "price_est": 7.057,
        "market": "derivatives",
        "leverage": None,
        "cost": None,
        "mode": "live",
        "order_id": "time-stop-1",
    }
    assert recent[4]["ccxt_symbol"] == "UNI/USDT:USDT"
    assert recent[4]["market"] == "derivatives"
    assert recent[4]["leverage"] == 3.0
    assert recent[4]["cost"] == 10.0
    assert [recent[5]["mode"], recent[6]["mode"]] == ["live", "dry"]
    assert recent[-1]["timestamp"] == "2026-10-01T01:00:00Z"


@pytest.mark.parametrize("buy_mode,sell_mode", [
    ("dry", "dry"),
    ("live", "live"),
    ("dry", "live"),
    ("live", "dry"),
])
def test_dry_and_live_audit_orders_never_become_verified_results(buy_mode, sell_mode):
    events = [
        _execute_event("BTC-USD", "buy", 1, 100, "2026-10-01T01:00:00Z", mode=buy_mode),
        _execute_event("BTC-USD", "sell", 1, 110, "2026-10-01T02:00:00Z", mode=sell_mode),
        _execute_event("ETH-USD", "buy", 1, 200, "2026-10-01T03:00:00Z", mode=buy_mode),
    ]

    result = performance_mod.compute_performance(events)
    assert result["summary"] is None
    assert result["open_positions"] is None
    assert [(row["action"], row["mode"]) for row in result["recent_orders"]] == [
        ("buy", buy_mode), ("sell", sell_mode), ("buy", buy_mode),
    ]


def test_recent_order_missing_optional_fields_has_explicit_defaults():
    result = performance_mod.compute_performance([{"action": "buy", "phase": "execute"}])
    assert result["recent_orders"] == [{
        "timestamp": None,
        "ticker": None,
        "ccxt_symbol": None,
        "action": "buy",
        "qty": None,
        "price_est": None,
        "market": "spot",
        "leverage": None,
        "cost": None,
        "mode": None,
        "order_id": None,
    }]


def test_recent_orders_use_append_order_and_cap_after_filtering():
    events = [
        _execute_event("BTC-USD", "bracket_placed", 1, 100, "2026-10-01T01:00:00Z"),
        _execute_event("BTC-USD", "bracket_failed", 1, 100, "2026-10-01T01:01:00Z"),
        *[
            _execute_event(
                "BTC-USD", "buy", 0.001, 100.0 + i, f"2026-10-01T00:{59 - i:02d}:00Z",
                order_id=f"order-{i}",
            )
            for i in range(60)
        ],
        {"action": "sell", "phase": "plan", "order_id": "plan"},
        {"action": "", "phase": "execute", "order_id": "no-action"},
        {"event": "halt"},
    ]

    result = performance_mod.compute_performance(events)
    assert result["summary"] is None
    assert result["open_positions"] is None
    assert result["brackets"] == {"placed": 1, "failed": 1}
    assert [row["order_id"] for row in result["recent_orders"]] == [
        f"order-{i}" for i in range(59, 9, -1)
    ]
    assert result["recent_orders"][0]["timestamp"] == "2026-10-01T00:00:00Z"
    assert result["recent_orders"][-1]["timestamp"] == "2026-10-01T00:49:00Z"


def test_performance_missing_log_is_empty_state(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    response = client.get("/api/performance")
    assert response.status_code == 200
    _assert_empty_state(response.json())


@pytest.mark.parametrize("content", ["", " \n\t\n"], ids=["empty", "whitespace"])
def test_performance_blank_log_is_empty_state(client, tmp_path, content):
    audit_file = tmp_path / "engine-audit.jsonl"
    audit_file.write_text(content, encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})
    response = client.get("/api/performance")
    assert response.status_code == 200
    _assert_empty_state(response.json())


@pytest.mark.parametrize("unreadable", ["directory", "invalid-utf8"])
def test_performance_unreadable_log_is_503(client, tmp_path, unreadable):
    audit_file = tmp_path
    if unreadable == "invalid-utf8":
        audit_file = tmp_path / "engine-audit.jsonl"
        audit_file.write_bytes(b"\xff")
    set_config({"exec_log_path": str(audit_file)})
    response = client.get("/api/performance")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_log_unreadable"


def test_performance_counts_skipped_lines_without_losing_valid_events(client, tmp_path):
    audit_file = tmp_path / "engine-audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_execute_event("BTC-USD", "buy", 1, 100, "2026-10-01T01:00:00Z")),
        "",
        "  \t ",
        "{invalid json",
        "null",
        "[]",
        "42",
        json.dumps({"event": "halt"}),
        json.dumps(_execute_event("BTC-USD", "bracket_failed", 1, 100, "2026-10-01T02:00:00Z")),
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    response = client.get("/api/performance")
    assert response.status_code == 200
    body = response.json()
    assert body["skipped_lines"] == 4
    assert body["summary"] is None
    assert body["open_positions"] is None
    assert body["brackets"] == {"placed": 0, "failed": 1}
    assert [row["action"] for row in body["recent_orders"]] == ["bracket_failed", "buy"]


@pytest.mark.parametrize("field", ["qty", "price_est", "leverage", "cost"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "positive-infinity", "negative-infinity"])
def test_performance_sanitizes_nonfinite_numeric_fields(client, tmp_path, field, bad_value):
    event = _execute_event(
        "BTC-USD", "buy", 1.0, 100.0, "2026-10-01T01:00:00Z", mode="live",
        market="derivatives", leverage=3.0, cost=40.0,
    )
    event[field] = bad_value
    _write_audit(tmp_path, [event])

    response = client.get("/api/performance")
    assert response.status_code == 200
    body = response.json()
    assert body["skipped_lines"] == 0
    order = body["recent_orders"][0]
    assert order[field] is None
    for other, value in {"qty": 1.0, "price_est": 100.0, "leverage": 3.0, "cost": 40.0}.items():
        if other != field:
            assert order[other] == value


@pytest.mark.parametrize("field", [
    "timestamp", "ticker", "ccxt_symbol", "action", "market", "mode", "order_id",
])
def test_performance_stays_json_serializable_with_nonfinite_audit_fields(client, tmp_path, field):
    event = _execute_event("BTC-USD", "buy", 1, 100, "2026-10-01T01:00:00Z")
    event[field] = float("nan")
    _write_audit(tmp_path, [event])

    response = client.get("/api/performance")
    assert response.status_code == 200
    json.dumps(response.json(), allow_nan=False)
