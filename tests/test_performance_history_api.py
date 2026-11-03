"""Isolated contract tests for the read-only futures fills endpoint."""

import copy
import importlib
import os
import socket
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace

import ccxt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import performance_history, performance_history_cache
from server.contract import ApiError, install_error_handlers
from tradingagents.execution.exchange_client import credential_env_names

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 12, 30, 0, 123000, tzinfo=UTC)
WEEK_MS = 7 * 24 * 60 * 60 * 1000
VENUE_KEY = "binance|https://demo-api.binance.com|False"
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


class FakeExchange:
    def __init__(self):
        self.timeout = None
        self.mutation_calls = []
        self.apiKey = None
        self.secret = None

    def create_order(self, *args, **kwargs):
        self.mutation_calls.append("create_order")
        raise AssertionError("history must not create orders")

    def cancel_order(self, *args, **kwargs):
        self.mutation_calls.append("cancel_order")
        raise AssertionError("history must not cancel orders")


class FakeBridge:
    def __init__(self, config, *, mode="live"):
        self.config = copy.deepcopy(config)
        self.exchange_id = str(config["exec_exchange_id"]).lower()
        self.mode = mode
        self.exchange = FakeExchange()
        key_name, secret_name = credential_env_names(self.exchange_id, config.get("exec_base_url"))
        self.exchange.apiKey = os.environ.get(key_name)
        self.exchange.secret = os.environ.get(secret_name)
        self.client_calls = 0

    def effective_mode(self):
        return self.mode

    def client(self):
        self.client_calls += 1
        return self.exchange

    def symbol_for(self, *_args):
        raise AssertionError("history must not guess symbols")

    def futures_symbol_for(self, *_args, **_kwargs):
        raise AssertionError("history must not resolve active symbols")


def event(symbol=BTC, *, timestamp="2026-10-31T00:00:00Z", **changes):
    row = {
        "timestamp": timestamp,
        "phase": "execute",
        "mode": "live",
        "market": "derivatives",
        "action": "buy",
        "ticker": "IGNORED-USD",
        "ccxt_symbol": symbol,
        "venue_key": VENUE_KEY,
    }
    row.update(changes)
    return row


def fill(symbol=BTC):
    return {
        "trade_id": "42",
        "order_id": "7",
        "symbol": symbol,
        "side": "buy",
        "timestamp": "2026-11-01T00:00:00Z",
        "contracts": "0.000000000123456789",
        "price": "12345.00000001",
        "fee": None,
        "fee_currency": None,
        "realized_pnl": "-0.00000001",
        "realized_pnl_currency": "USDT",
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Allow only the event loop's local socket pair on Windows."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets = weakref.WeakSet()

    def refuse_connect(sock, address):
        if sock in selfpipe_sockets:
            return original_connect(sock, address)
        raise AssertionError("unexpected network connection")

    def refuse_connect_ex(sock, address):
        if sock in selfpipe_sockets:
            return original_connect_ex(sock, address)
        raise AssertionError("unexpected network connection")

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
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: pytest.fail("unexpected DNS"))


@pytest.fixture
def setup_api(monkeypatch):
    state = SimpleNamespace(
        config={
            "exec_live": True,
            "exec_exchange_id": "binance",
            "exec_base_url": "https://demo-api.binance.com",
            "exec_sandbox": False,
        },
        events=[event()],
        skipped=0,
        bridge=None,
        calls=[],
        result=None,
        monotonic=100.0,
        now=NOW,
    )

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now if tz is not None else state.now.replace(tzinfo=None)

    monkeypatch.setattr(performance_history, "datetime", FrozenDatetime)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: state.monotonic)
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(performance_history_cache, "_HISTORY_CACHE", {})
    monkeypatch.setattr(performance_history_cache, "HISTORY_LOCK", threading.Lock())
    credential_identity = f"isolated-{id(state)}"
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", credential_identity)
    monkeypatch.setenv("BINANCE_DEMO_SECRET", f"{credential_identity}-secret")
    state.api_key = credential_identity
    state.secret = f"{credential_identity}-secret"
    monkeypatch.setattr(performance_history, "get_config", lambda: state.config)
    monkeypatch.setattr(performance_history.audit_log, "read_events", lambda: (state.events, state.skipped))

    def build_bridge(config):
        state.bridge = FakeBridge(config)
        return state.bridge

    monkeypatch.setattr(performance_history, "_build_bridge", build_bridge)
    reader = ModuleType("server.futures_trade_history")

    def read_history(bridge, symbols, since_ms, until_ms, **kwargs):
        assert bridge.exchange.timeout == 5000
        state.calls.append((bridge, list(symbols), since_ms, until_ms, kwargs))
        if state.result is not None:
            return copy.deepcopy(state.result)
        return {
            "status": "ok",
            "source": "venue_fills",
            "venue_profile": "demo",
            "start_at": (NOW - timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            "end_at": NOW.isoformat().replace("+00:00", "Z"),
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
            "coverage": "requested_symbols_only; starting_basis_unknown; venue_retention_unknown",
            "warnings": [],
            "fills": [fill(symbols[0])],
        }

    reader.read_futures_trade_history = read_history
    monkeypatch.setitem(sys.modules, "server.futures_trade_history", reader)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(performance_history.router)
    with TestClient(app) as client:
        yield client, state, reader


def test_exact_audit_symbols_are_sorted_deduplicated_and_queried_read_only(setup_api):
    client, state, _ = setup_api
    state.events = [
        event(ETH, timestamp="2026-10-30T00:00:00Z"),
        event(BTC, timestamp="2026-10-31T00:00:00Z"),
        event(ETH, timestamp="2026-11-01T02:30:00+02:00", action="bracket_placed"),
        event("SPOT/USDT", market="spot"),
        event("DRY/USDT:USDT", mode="dry"),
        event("PLAN/USDT:USDT", phase="plan"),
        event("REJECTED/USDT:USDT", action="rejected_at_execute"),
        event("OTHER/USDT:USDT", venue_key="binance|https://api.binance.com|False"),
    ]
    before = copy.deepcopy((state.config, state.events))

    response = client.get("/api/performance/fills")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "generated_at", "status", "source", "venue_profile", "start_at", "end_at",
        "observed_at", "coverage", "warnings", "fills",
    }
    assert body["generated_at"] == NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert body["status"] == "partial"
    assert body["source"] == "venue_fills"
    assert body["venue_profile"] == "demo"
    assert body["fills"] == [fill(ETH)]
    assert "Chỉ các mã trong nhật ký được chọn" in body["coverage"]
    assert "chưa xác minh đầy đủ tài khoản" in body["coverage"]
    assert f"{ETH}, {BTC}" in body["coverage"]
    assert "thời gian lưu trữ" in body["coverage"].lower()
    assert "tài khoản sàn đang chọn" in body["coverage"].lower()
    assert "người dùng" in body["coverage"].lower()
    assert "hệ thống khác" in body["coverage"].lower()
    assert "không xác minh" in body["coverage"].lower() and "bot" in body["coverage"].lower()
    bridge, symbols, since, until, options = state.calls[0]
    assert symbols == [ETH, BTC]
    assert until == int(NOW.timestamp() * 1000)
    assert since == until - WEEK_MS
    assert options == {"max_pages_per_symbol": 3, "max_total_seconds": 15}
    assert bridge.client_calls >= 1
    assert bridge.exchange.mutation_calls == []
    assert (state.config, state.events) == before


def test_cache_preserves_response_until_expiration_then_refreshes(setup_api):
    client, state, _ = setup_api
    first = client.get("/api/performance/fills").json()
    assert first["status"] == "partial"
    assert first["fills"] == [fill()]
    assert len(state.calls) == 1

    state.result = copy.deepcopy(first)
    state.result["fills"][0]["trade_id"] = "99"
    state.monotonic += 239.999
    state.now += timedelta(seconds=239)
    assert client.get("/api/performance/fills").json() == first
    assert state.bridge.client_calls >= 1  # inspect the actual client's credentials
    assert len(state.calls) == 1

    state.monotonic = 340.0
    state.now = NOW + timedelta(seconds=240)
    refreshed = client.get("/api/performance/fills").json()
    assert refreshed["status"] == "partial"
    assert refreshed["fills"][0]["trade_id"] == "99"
    assert refreshed["generated_at"] == "2026-11-02T12:34:00.123Z"
    assert len(state.calls) == 2
    assert client.get("/api/performance/fills").json() == refreshed
    assert len(state.calls) == 2


@pytest.mark.parametrize("credential", ["BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET"])
def test_cache_is_isolated_when_either_credential_rotates(setup_api, monkeypatch, credential):
    client, state, _ = setup_api
    first = client.get("/api/performance/fills").json()
    assert first["fills"] == [fill()]
    state.result = copy.deepcopy(first)
    state.result["fills"][0]["trade_id"] = "99"
    state.now += timedelta(seconds=1)
    monkeypatch.setenv(credential, "rotated-credential")

    rotated = client.get("/api/performance/fills").json()
    assert rotated["fills"][0]["trade_id"] == "99"
    assert rotated["generated_at"] != first["generated_at"]
    assert len(state.calls) == 2
    assert client.get("/api/performance/fills").json() == rotated
    assert len(state.calls) == 2


def test_client_credentials_must_match_cache_identity_after_client_rotation(setup_api, monkeypatch):
    client, state, _ = setup_api
    builds = []

    def build(config):
        bridge = FakeBridge(config)
        bridge.exchange.apiKey = state.api_key if not builds else "different-client-key"
        bridge.exchange.secret = state.secret
        builds.append(bridge)
        return bridge

    monkeypatch.setattr(performance_history, "_build_bridge", build)
    first = client.get("/api/performance/fills").json()
    assert first["fills"] == [fill()]
    state.result = copy.deepcopy(first)
    state.result["fills"][0]["trade_id"] = "99"
    second = client.get("/api/performance/fills").json()
    assert second["fills"] != first["fills"]
    assert len(state.calls) == 1  # mismatched client is refused before a venue read
    assert second["status"] == "unavailable"


def test_rotation_after_key_creation_prevents_wrong_cache_write(setup_api, monkeypatch):
    client, state, _ = setup_api
    original = performance_history_cache.client_cache_key
    calls = []

    def rotate(config, bridge, exchange, symbols):
        key = original(config, bridge, exchange, symbols)
        calls.append(key)
        if len(calls) == 1:
            monkeypatch.setenv("BINANCE_DEMO_API_KEY", "rotated-between-keys")
            exchange.apiKey = "rotated-between-keys"
        return key

    monkeypatch.setattr(performance_history_cache, "client_cache_key", rotate)
    result = client.get("/api/performance/fills").json()
    assert result["status"] == "unavailable"
    assert result["fills"] == []
    assert state.calls == []
    assert not performance_history_cache._HISTORY_CACHE


def test_rotation_while_cache_is_being_read_prevents_cache_hit(setup_api, monkeypatch):
    client, state, _ = setup_api
    assert client.get("/api/performance/fills").json()["fills"] == [fill()]
    original = performance_history_cache.cache_response

    def rotate(key):
        cached = original(key)
        if cached is not None:
            monkeypatch.setenv("BINANCE_DEMO_API_KEY", "rotated-at-cache-hit")
        return cached

    monkeypatch.setattr(performance_history_cache, "cache_response", rotate)
    result = client.get("/api/performance/fills").json()
    assert result["fills"] == []
    assert result["status"] == "unavailable"
    assert len(state.calls) == 1


@pytest.mark.parametrize("rotation", ["env", "config", "client", "replacement", "gate"])
def test_rotation_during_read_never_stores_result_under_old_identity(setup_api, monkeypatch, rotation):
    client, state, reader = setup_api

    def rotate(bridge, symbols, since_ms, until_ms, **kwargs):
        if rotation == "env":
            monkeypatch.setenv("BINANCE_DEMO_API_KEY", "rotated-during-read")
        elif rotation == "config":
            state.config["exec_base_url"] = "https://api.binance.com"
        elif rotation == "client":
            bridge.exchange.apiKey = "rotated-during-read"
        elif rotation == "replacement":
            bridge.exchange = FakeExchange()
        else:
            bridge.mode = "dry"
        return {
            "status": "partial", "source": "venue_fills", "venue_profile": "demo",
            "observed_at": NOW.isoformat(), "warnings": [], "fills": [fill()],
        }

    reader.read_futures_trade_history = rotate
    result = client.get("/api/performance/fills").json()
    assert result["status"] == "unavailable"
    assert result["fills"] == []
    assert not performance_history_cache._HISTORY_CACHE


def test_cache_is_isolated_by_selected_symbols_and_venue(setup_api, monkeypatch):
    client, state, _ = setup_api
    btc = client.get("/api/performance/fills").json()
    assert btc["fills"] == [fill(BTC)]
    state.events = [event(ETH)]
    eth = client.get("/api/performance/fills").json()
    assert eth["fills"] == [fill(ETH)]
    assert [call[1] for call in state.calls] == [[BTC], [ETH]]

    state.config["exec_base_url"] = "https://api.binance.com"
    state.events = [event(ETH, venue_key="binance|https://api.binance.com|False")]
    monkeypatch.setenv("BINANCE_API_KEY", state.api_key)
    monkeypatch.setenv("BINANCE_SECRET", state.secret)
    state.result = copy.deepcopy(eth)
    state.result["venue_profile"] = "production"
    state.result["fills"][0]["trade_id"] = "99"
    production = client.get("/api/performance/fills").json()
    assert production["venue_profile"] == "production"
    assert production["fills"][0]["trade_id"] == "99"
    assert [call[1] for call in state.calls] == [[BTC], [ETH], [ETH]]
    assert client.get("/api/performance/fills").json() == production
    assert len(state.calls) == 3


def test_cache_does_not_expose_credentials(setup_api):
    client, state, _ = setup_api
    first = client.get("/api/performance/fills")
    cached = client.get("/api/performance/fills")
    assert first.status_code == cached.status_code == 200
    assert cached.json() == first.json()
    assert performance_history_cache._HISTORY_CACHE
    for credential in (state.api_key, state.secret):
        assert credential not in first.text
        assert credential not in cached.text
        assert credential not in repr(performance_history_cache._HISTORY_CACHE)
    assert len(state.calls) == 1


def test_cached_fills_include_new_audit_warnings(setup_api):
    client, state, _ = setup_api
    first = client.get("/api/performance/fills").json()
    assert not any("nhật ký không hợp lệ" in warning.lower() for warning in first["warnings"])

    state.skipped = 1
    cached = client.get("/api/performance/fills").json()
    assert cached["fills"] == first["fills"]
    assert any("nhật ký không hợp lệ" in warning.lower() for warning in cached["warnings"])

    state.skipped = 0
    assert client.get("/api/performance/fills").json() == first
    assert len(state.calls) == 1


def test_cached_fills_replace_old_audit_warnings_and_coverage(setup_api):
    client, state, _ = setup_api
    state.skipped = 1
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        "warnings": ["fetch_failed"], "fills": [fill()],
    }
    first = client.get("/api/performance/fills").json()
    assert any("nhật ký không hợp lệ" in warning.lower() for warning in first["warnings"])

    state.skipped = 0
    state.events[0].pop("venue_key")
    cached = client.get("/api/performance/fills").json()
    assert not any("nhật ký không hợp lệ" in warning.lower() for warning in cached["warnings"])
    assert any("chưa xác minh" in warning.lower() for warning in cached["warnings"])
    assert any("không thể đọc khớp lệnh" in warning.lower() for warning in cached["warnings"])
    assert "chưa được xác minh" in cached["coverage"].lower()
    assert cached["fills"] == first["fills"]
    assert len(state.calls) == 1


@pytest.mark.parametrize("action", [{}, [], 7])
def test_malformed_audit_action_is_ignored_without_losing_valid_symbols(setup_api, action):
    client, state, _ = setup_api
    state.events = [event(ETH, action=action), event(BTC)]
    response = client.get("/api/performance/fills")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["fills"] == [fill(BTC)]
    assert any("bỏ qua" in warning.lower() and "nhật ký" in warning.lower()
               for warning in body["warnings"])
    assert state.calls[0][1] == [BTC]
    assert len(state.calls) == 1


def test_time_stop_close_discovers_symbol_only_not_a_fill(setup_api):
    client, state, _ = setup_api
    state.events = [
        event(BTC, action="time_stop_postponed"),
        event(ETH, action="time_stop_closed", timestamp="2026-11-01T00:00:00Z"),
    ]
    state.result = {
        "status": "ok", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": [], "fills": [],
    }
    body = client.get("/api/performance/fills").json()
    assert state.calls[0][1] == [ETH]
    assert body["fills"] == []
    assert state.calls[0][0].exchange.mutation_calls == []


def test_unverified_audit_venue_is_explicit_and_partial(setup_api):
    client, state, _ = setup_api
    state.events = [event()]
    del state.events[0]["venue_key"]
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert body["fills"] == [fill()]
    assert any("chưa xác minh" in warning.lower() and "sàn" in warning.lower()
               for warning in body["warnings"])
    assert "chưa xác minh" in body["coverage"].lower()
    assert state.calls[0][1] == [BTC]


def test_missing_or_invalid_exact_symbols_and_corrupt_lines_are_partial(setup_api):
    client, state, _ = setup_api
    state.skipped = 2
    state.events = [
        event(None, ticker="BTC-USD"),
        event(" BTC/USDT:USDT "),
        event("X/USDT"),
        event(ETH, action="bracket_failed"),
    ]
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert any("thiếu" in warning.lower() and "mã" in warning.lower()
               for warning in body["warnings"])
    assert any("bỏ qua" in warning.lower() and "nhật ký" in warning.lower()
               for warning in body["warnings"])
    assert state.calls[0][1] == [ETH]


def test_invalid_event_timestamp_is_last_and_marks_selection_partial(setup_api):
    client, state, _ = setup_api
    state.events = [event(BTC, timestamp="not-a-time"), event(ETH)]
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert any("thời gian" in warning.lower() for warning in body["warnings"])
    assert state.calls[0][1] == [ETH, BTC]


def test_symbol_limit_is_twelve_newest_unique_symbols(setup_api):
    client, state, _ = setup_api
    symbols = [f"COIN{i}/USDT:USDT" for i in range(13)]
    state.events = [
        event(symbol, timestamp=f"2026-10-{day + 1:02d}T00:00:00Z")
        for day, symbol in enumerate(symbols)
    ]
    state.events.append(event(symbols[-1], timestamp="2026-11-01T00:00:00Z"))
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert any("12" in warning for warning in body["warnings"])
    assert state.calls[0][1] == list(reversed(symbols))[:12]
    assert len(state.calls[0][1]) == 12


def test_verified_profile_symbols_take_priority_over_unverified_legacy_symbols(setup_api):
    client, state, _ = setup_api
    legacy_events = []
    for index in range(12):
        legacy = event(
            f"OLD{index}/USDT:USDT",
            timestamp=f"2026-11-01T00:{index:02d}:00Z",
        )
        legacy.pop("venue_key")
        legacy_events.append(legacy)
    verified = event(BTC, timestamp="2026-10-27T00:00:00Z")
    state.events = [*legacy_events, verified]

    body = client.get("/api/performance/fills").json()

    assert body["status"] == "partial"
    assert state.calls[0][1][0] == BTC
    assert len(state.calls[0][1]) == 12
    assert any("12" in warning for warning in body["warnings"])
    assert any("chưa xác minh" in warning.lower() for warning in body["warnings"])


def test_no_eligible_symbols_is_unavailable_not_verified_zero(setup_api):
    client, state, _ = setup_api
    state.events = [event(None, ticker="BTC-USD"), event(BTC, mode="dry")]
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "unavailable"
    assert body["source"] == "unavailable"
    assert body["fills"] == []
    assert body["observed_at"] is None
    assert any("không tìm thấy" in warning.lower() for warning in body["warnings"])
    assert body["start_at"] == (NOW - timedelta(days=7)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert body["end_at"] == NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert state.calls == []
    assert state.bridge is None or state.bridge.client_calls == 0


@pytest.mark.parametrize("closed_gate", [False, None, 1, "true"])
def test_closed_config_gate_never_builds_or_calls_venue(setup_api, closed_gate):
    client, state, _ = setup_api
    state.config["exec_live"] = closed_gate
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "unavailable"
    assert body["source"] == "unavailable"
    assert body["fills"] == []
    assert state.calls == []
    assert state.bridge is None


def test_closed_effective_gate_never_calls_venue(setup_api, monkeypatch):
    client, state, _ = setup_api
    bridge = FakeBridge(state.config, mode="dry")
    monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "unavailable"
    assert body["fills"] == []
    assert bridge.client_calls == 0
    assert state.calls == []


def test_non_binance_is_unsupported_without_fetch(setup_api):
    client, state, _ = setup_api
    state.config["exec_exchange_id"] = "bybit"
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "unsupported"
    assert body["source"] == "unavailable"
    assert body["fills"] == []
    assert state.calls == []
    assert state.bridge is None or state.bridge.client_calls == 0


def test_unreadable_audit_is_sanitized_503(setup_api, monkeypatch):
    client, state, _ = setup_api

    def unreadable():
        raise ApiError(503, "audit_log_unreadable", "secret-api-key /secret/path")

    monkeypatch.setattr(performance_history.audit_log, "read_events", unreadable)
    response = client.get("/api/performance/fills")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_log_unreadable"
    assert "secret" not in response.text
    assert state.calls == []
    assert state.bridge is None


@pytest.mark.parametrize("failure_at", ["config", "audit", "bridge", "mode", "client", "reader"])
def test_unexpected_failures_return_sanitized_error_and_no_fills(setup_api, monkeypatch, failure_at):
    client, state, reader = setup_api

    def fail(*_args, **_kwargs):
        raise RuntimeError("API_KEY=secret-key https://secret.invalid")

    if failure_at == "config":
        monkeypatch.setattr(performance_history, "get_config", fail)
    elif failure_at == "audit":
        monkeypatch.setattr(performance_history.audit_log, "read_events", fail)
    elif failure_at == "bridge":
        monkeypatch.setattr(performance_history, "_build_bridge", fail)
    elif failure_at in {"mode", "client"}:
        bridge = FakeBridge(state.config)
        monkeypatch.setattr(bridge, "effective_mode" if failure_at == "mode" else "client", fail)
        monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    else:
        monkeypatch.setattr(reader, "read_futures_trade_history", fail)
    response = client.get("/api/performance/fills")
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["source"] == "unavailable"
    assert response.json()["fills"] == []
    assert "secret" not in response.text
    assert "API_KEY" not in response.text


def test_reader_warnings_are_sanitized_and_partial(setup_api):
    client, state, _ = setup_api
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00Z", "end_at": "2026-11-02T12:30:00Z",
        "observed_at": "2026-11-02T12:30:01Z", "coverage": "API_KEY=secret",
        "warnings": [
            "fetch_failed: BTC/USDT:USDT [2026-10-26T12:30:00.123Z, 2026-11-02T12:30:00.123Z)",
            "fetch_failed: secret-key", "secret-key",
        ], "fills": [fill()],
    }
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert any("BTC/USDT:USDT" in warning and "2026-10-26T12:30:00.123Z" in warning
               for warning in body["warnings"])
    assert all("fetch_failed" not in warning for warning in body["warnings"])
    assert "secret" not in str(body)
    assert body["fills"] == [fill()]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("rate_limited", "giới hạn yêu cầu"),
        ("normalized_history_unverified", "lịch sử chuẩn hóa"),
        ("portfolio_margin_unsupported", "portfolio margin"),
    ],
)
def test_new_reader_warnings_are_localized_with_safe_context(setup_api, code, expected):
    client, state, _ = setup_api
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:01Z", "coverage": "unused",
        "warnings": [
            f"{code}: {BTC} [2026-10-26T12:30:00.123Z, 2026-11-02T12:30:00.123Z)",
            f"{code}: secret-key", "unrecognized: secret-key",
        ], "fills": [fill()],
    }
    response = client.get("/api/performance/fills")
    body = response.json()
    assert body["status"] == "partial"
    assert body["fills"] == [fill()]
    assert any(expected in warning.lower() for warning in body["warnings"])
    assert any(BTC in warning and "2026-10-26T12:30:00.123Z" in warning
               for warning in body["warnings"])
    assert code not in response.text
    assert "secret" not in response.text


def test_rate_limit_cooldown_serves_fresh_cache_then_blocks_until_900_seconds(setup_api):
    client, state, _ = setup_api
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": [f"rate_limited: {BTC} [2026-10-26T12:30:00.123Z, 2026-11-02T12:30:00.123Z)"],
        "fills": [fill()],
    }
    first = client.get("/api/performance/fills").json()
    assert first["status"] == "partial"
    assert first["fills"] == [fill()]
    assert any("giới hạn yêu cầu" in warning.lower() for warning in first["warnings"])
    assert len(state.calls) == 1

    state.monotonic = 339.999
    state.now = NOW + timedelta(seconds=299)
    cached = client.get("/api/performance/fills").json()
    assert cached == first
    assert state.bridge.client_calls >= 1  # identity checks do not read the venue
    assert len(state.calls) == 1

    state.monotonic = 400.0
    blocked = client.get("/api/performance/fills").json()
    assert blocked["status"] == "unavailable"
    assert blocked["source"] == "unavailable"
    assert blocked["observed_at"] is None
    assert blocked["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in blocked["warnings"])
    assert state.bridge.client_calls == 0
    assert len(state.calls) == 1

    state.monotonic = 999.999
    blocked = client.get("/api/performance/fills").json()
    assert blocked["status"] == "unavailable"
    assert blocked["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in blocked["warnings"])
    assert len(state.calls) == 1
    state.monotonic = 1000.0
    state.now = NOW + timedelta(seconds=900)
    state.result = None
    resumed = client.get("/api/performance/fills").json()
    assert resumed["status"] == "partial"
    assert resumed["fills"] == [fill()]
    assert resumed["generated_at"] != first["generated_at"]
    assert len(state.calls) == 2
    assert client.get("/api/performance/fills").json() == resumed
    assert len(state.calls) == 2


@pytest.mark.parametrize("warning", [
    "normalized_history_unverified", "portfolio_margin_unsupported", "rate_limit_disabled",
    "rate_limited_extra", "fetch_failed: rate_limited",
])
def test_other_warnings_do_not_start_rate_limit_cooldown(setup_api, warning):
    client, state, _ = setup_api
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": [warning], "fills": [],
    }
    assert client.get("/api/performance/fills").json()["status"] == "partial"
    state.monotonic += 301.0
    second = client.get("/api/performance/fills").json()
    assert second["status"] == "partial"
    assert second["source"] == "venue_fills"
    assert len(state.calls) == 2


def test_retry_after_longer_than_900_seconds_extends_cooldown(setup_api):
    client, state, _ = setup_api
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": ["rate_limited"], "fills": [fill()], "retry_after_seconds": 1200,
    }
    first = client.get("/api/performance/fills").json()
    assert first["fills"] == [fill()]
    assert any("giới hạn yêu cầu" in warning.lower() for warning in first["warnings"])
    assert len(state.calls) == 1

    state.monotonic = 1000.0
    blocked = client.get("/api/performance/fills").json()
    assert blocked["status"] == "unavailable"
    assert blocked["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in blocked["warnings"])
    assert state.bridge.client_calls == 0
    assert len(state.calls) == 1
    state.monotonic = 1299.999
    assert client.get("/api/performance/fills").json()["status"] == "unavailable"
    assert len(state.calls) == 1

    state.monotonic = 1300.0
    state.result = None
    resumed = client.get("/api/performance/fills").json()
    assert resumed["status"] == "partial"
    assert resumed["fills"] == [fill()]
    assert len(state.calls) == 2


def test_rate_limit_unavailable_result_starts_cooldown(setup_api, monkeypatch):
    client, state, _ = setup_api
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)
    state.result = {
        "status": "unavailable", "source": "unavailable", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": ["rate_limited"], "fills": [],
    }
    assert client.get("/api/performance/fills").json()["status"] == "unavailable"
    blocked = client.get("/api/performance/fills").json()
    assert blocked["status"] == "unavailable"
    assert blocked["observed_at"] is None
    assert blocked["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in blocked["warnings"])
    assert len(state.calls) == 1


def test_rate_limit_cooldown_blocks_concurrent_requests(setup_api, monkeypatch):
    client, state, reader = setup_api
    clock = SimpleNamespace(now=500.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: clock.now)
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:00.123Z", "coverage": "unused",
        "warnings": ["rate_limited"], "fills": [],
    }
    entered = threading.Event()
    release = threading.Event()
    original_reader = reader.read_futures_trade_history

    def blocking_reader(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(reader, "read_futures_trade_history", blocking_reader)
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = executor.submit(client.get, "/api/performance/fills")
        try:
            assert entered.wait(timeout=5)
            busy = [executor.submit(client.get, "/api/performance/fills") for _ in range(2)]
            for request in busy:
                response = request.result(timeout=5)
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "history_busy"
        finally:
            release.set()
        first_body = first.result(timeout=5).json()
        assert first_body["status"] == "partial"
        cached = client.get("/api/performance/fills").json()
        assert cached == first_body
        assert any("giới hạn yêu cầu" in warning.lower() for warning in cached["warnings"])
        clock.now = 801.0
        for _ in range(3):
            body = client.get("/api/performance/fills").json()
            assert body["status"] == "unavailable"
            assert body["fills"] == []
            assert any("giới hạn yêu cầu" in warning.lower() for warning in body["warnings"])
    assert len(state.calls) == 1


def test_submillisecond_now_matches_reader_warning_boundaries(setup_api, monkeypatch):
    client, state, _ = setup_api

    class PreciseDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(microsecond=123456)

    monkeypatch.setattr(performance_history, "datetime", PreciseDatetime)
    state.result = {
        "status": "partial", "source": "venue_fills", "venue_profile": "demo",
        "start_at": "2026-10-26T12:30:00.123Z", "end_at": "2026-11-02T12:30:00.123Z",
        "observed_at": "2026-11-02T12:30:01Z", "coverage": "unused",
        "warnings": [
            "fetch_failed: BTC/USDT:USDT [2026-10-26T12:30:00.123Z, 2026-11-02T12:30:00.123Z)"
        ],
        "fills": [],
    }
    body = client.get("/api/performance/fills").json()
    assert body["start_at"] == "2026-10-26T12:30:00.123Z"
    assert body["end_at"] == "2026-11-02T12:30:00.123Z"
    assert any("BTC/USDT:USDT" in warning and "2026-10-26T12:30:00.123Z" in warning
               for warning in body["warnings"])


def test_busy_lock_returns_structured_503(setup_api):
    client, state, _ = setup_api
    assert performance_history_cache.HISTORY_LOCK.acquire(blocking=False)
    try:
        response = client.get("/api/performance/fills")
    finally:
        performance_history_cache.HISTORY_LOCK.release()
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "history_busy"
    assert response.json()["error"]["message"]
    assert "fills" not in response.json()
    assert state.calls == []
    assert state.bridge is None or state.bridge.client_calls == 0


def test_actual_reader_contract_with_fake_exchange(setup_api, monkeypatch):
    client, state, _ = setup_api
    monkeypatch.delitem(sys.modules, "server.futures_trade_history")
    reader = importlib.import_module("server.futures_trade_history")
    assert callable(reader.read_futures_trade_history)
    bridge = FakeBridge(state.config)
    exchange = bridge.exchange
    exchange.id = "binance"
    exchange.enableRateLimit = True
    exchange.urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}
    calls = []

    def load_markets():
        assert exchange.timeout == 5000
        return {BTC: {"id": "BTCUSDT", "symbol": BTC, "contract": True, "linear": True, "swap": True}}

    def fetch_my_trades(symbol, since, limit, params):
        calls.append((symbol, since, limit, params))
        return [{
            "id": "42", "order": "7", "symbol": BTC, "side": "buy",
            "timestamp": int((NOW - timedelta(days=1)).timestamp() * 1000),
            "info": {"id": 42, "symbol": "BTCUSDT", "side": "BUY",
                     "qty": "0.000000000123456789", "price": "12345.00000001",
                     "realizedPnl": "-0.00000001", "marginAsset": "USDT"},
        }]

    exchange.load_markets = load_markets
    exchange.fetch_my_trades = fetch_my_trades
    monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    body = client.get("/api/performance/fills").json()
    assert body["status"] == "partial"
    assert body["source"] == "venue_fills"
    assert body["venue_profile"] == "demo"
    expected = fill()
    expected["timestamp"] = (NOW - timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert body["fills"] == [expected]
    assert len(calls) == 1
    assert calls[0][0] == BTC
    assert calls[0][1] == int(NOW.timestamp() * 1000) - WEEK_MS
    assert calls[0][3]["until"] == int(NOW.timestamp() * 1000) - 1
    assert exchange.mutation_calls == []


def test_actual_reader_rate_limit_triggers_route_cooldown(setup_api, monkeypatch):
    client, state, _ = setup_api
    monkeypatch.delitem(sys.modules, "server.futures_trade_history")
    reader = importlib.import_module("server.futures_trade_history")
    assert callable(reader.read_futures_trade_history)
    bridge = FakeBridge(state.config)
    exchange = bridge.exchange
    exchange.id = "binance"
    exchange.enableRateLimit = True
    exchange.urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}
    calls = []

    def load_markets():
        assert exchange.timeout == 5000
        return {BTC: {"id": "BTCUSDT", "symbol": BTC, "contract": True, "linear": True, "swap": True}}

    def fetch_my_trades(symbol, since, limit, params):
        calls.append((symbol, since, limit, params))
        raise ccxt.RateLimitExceeded("API_KEY=secret-key")

    exchange.load_markets = load_markets
    exchange.fetch_my_trades = fetch_my_trades
    monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)

    first = client.get("/api/performance/fills")
    assert first.status_code == 200
    assert first.json()["status"] == "unavailable"
    assert first.json()["source"] == "unavailable"
    assert first.json()["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in first.json()["warnings"])
    assert "secret" not in first.text
    assert len(calls) == 1

    second = client.get("/api/performance/fills")
    assert second.status_code == 200
    assert second.json()["status"] == "unavailable"
    assert second.json()["source"] == "unavailable"
    assert second.json()["observed_at"] is None
    assert second.json()["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in second.json()["warnings"])
    assert len(calls) == 1
    assert bridge.client_calls >= 2  # route and reader inspect the same client
    assert exchange.mutation_calls == []


def test_client_rate_limit_triggers_route_cooldown_without_reader(setup_api, monkeypatch):
    client, state, _ = setup_api
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: clock.now)

    class RateLimitedClientBridge(FakeBridge):
        def client(self):
            self.client_calls += 1
            error = ccxt.RateLimitExceeded("API_KEY=secret-key")
            error.response = SimpleNamespace(headers={"Retry-After": "1200"})
            raise error

    bridge = RateLimitedClientBridge(state.config)
    monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    first = client.get("/api/performance/fills")
    second = client.get("/api/performance/fills")
    first_body = first.json()
    second_body = second.json()

    assert first.status_code == 200
    assert first_body["status"] == "unavailable"
    assert first_body["source"] == "unavailable"
    assert first_body["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in first_body["warnings"])
    assert "secret" not in first.text
    assert second.status_code == 200
    assert second_body["status"] == "unavailable"
    assert second_body["source"] == "unavailable"
    assert second_body["observed_at"] is None
    assert second_body["fills"] == []
    assert any("giới hạn yêu cầu" in warning.lower() for warning in second_body["warnings"])
    assert bridge.client_calls == 1
    assert state.calls == []
    clock.now = 1000.0
    assert client.get("/api/performance/fills").json()["status"] == "unavailable"
    assert bridge.client_calls == 1
    clock.now = 1299.999
    assert client.get("/api/performance/fills").json()["status"] == "unavailable"
    assert bridge.client_calls == 1
    clock.now = 1300.0
    assert client.get("/api/performance/fills").json()["status"] == "unavailable"
    assert bridge.client_calls == 2
    assert state.calls == []


def test_actual_reader_all_failed_fills_are_unavailable(setup_api, monkeypatch):
    client, state, _ = setup_api
    monkeypatch.delitem(sys.modules, "server.futures_trade_history")
    reader = importlib.import_module("server.futures_trade_history")
    assert callable(reader.read_futures_trade_history)
    bridge = FakeBridge(state.config)
    exchange = bridge.exchange
    exchange.id = "binance"
    exchange.enableRateLimit = True
    exchange.urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}
    calls = []
    exchange.load_markets = lambda: {
        BTC: {"id": "BTCUSDT", "symbol": BTC, "contract": True, "linear": True, "swap": True},
    }

    def fetch_my_trades(symbol, since, limit, params):
        calls.append(symbol)
        raise ccxt.NetworkError("API_KEY=secret-key")

    exchange.fetch_my_trades = fetch_my_trades
    monkeypatch.setattr(performance_history, "_build_bridge", lambda _config: bridge)
    response = client.get("/api/performance/fills")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["source"] == "unavailable"
    assert response.json()["fills"] == []
    assert "secret" not in response.text
    assert calls == [BTC]
    assert exchange.mutation_calls == []


def test_concurrent_refresh_fetches_once_then_reuses_cache(setup_api, monkeypatch):
    client, state, reader = setup_api
    initial = client.get("/api/performance/fills").json()
    assert initial["status"] == "partial"
    assert len(state.calls) == 1
    state.monotonic += 301.0
    state.now += timedelta(seconds=301)
    entered = threading.Event()
    release = threading.Event()
    original_reader = reader.read_futures_trade_history

    def blocking_reader(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(reader, "read_futures_trade_history", blocking_reader)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.get, "/api/performance/fills")
        try:
            assert entered.wait(timeout=5)
            second = client.get("/api/performance/fills")
            assert second.status_code == 503
            assert second.json()["error"]["code"] == "history_busy"
        finally:
            release.set()
        refreshed = first.result(timeout=5).json()
    assert refreshed["status"] == "partial"
    assert refreshed["fills"] == initial["fills"]
    assert refreshed["generated_at"] != initial["generated_at"]
    assert len(state.calls) == 2
    assert client.get("/api/performance/fills").json() == refreshed
    assert len(state.calls) == 2
    state.monotonic += 301.0
    assert client.get("/api/performance/fills").json()["status"] == "partial"
    assert len(state.calls) == 3
