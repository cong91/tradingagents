"""Isolated tests for process-local performance history state."""

import hashlib
import hmac
import threading
from collections import UserDict
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from server import performance_history_cache as history_cache

pytestmark = pytest.mark.unit

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(history_cache, "monotonic", lambda: clock.now)
    monkeypatch.setattr(history_cache, "_HISTORY_CACHE", {})
    monkeypatch.setattr(history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(history_cache, "HISTORY_LOCK", threading.Lock())
    return clock


def test_cache_deep_copies_on_store_and_retrieval_until_exact_expiry(clock):
    key = ("binance", "demo", "identity", (BTC,))
    response = {"source": "venue_fills", "status": "ok", "fills": [{"trade_id": "42"}]}
    history_cache.store_cache(key, response)
    response["fills"][0]["trade_id"] = "mutated input"

    clock.now = 339.999
    cached = history_cache.cache_response(key)
    assert cached["fills"][0]["trade_id"] == "42"
    cached["fills"][0]["trade_id"] = "mutated output"
    assert history_cache.cache_response(key)["fills"][0]["trade_id"] == "42"

    clock.now = 340.0
    assert history_cache.cache_response(key) is None
    assert history_cache._HISTORY_CACHE == {}


def test_cache_replaces_single_entry_only_for_cacheable_responses(clock):
    first = ("binance", "demo", "one", (BTC,))
    second = ("binance", "demo", "two", (ETH,))
    valid = {"source": "venue_fills", "status": "partial", "fills": []}
    history_cache.store_cache(first, valid)

    for invalid in (
        {"source": "unavailable", "status": "partial"},
        {"source": "venue_fills", "status": "error"},
    ):
        history_cache.store_cache(second, invalid)
    history_cache.store_cache(None, valid)
    assert history_cache.cache_response(first) == valid
    assert history_cache.cache_response(None) is None

    history_cache.store_cache(second, valid)
    assert history_cache.cache_response(first) is None
    assert history_cache.cache_response(second) == valid
    assert len(history_cache._HISTORY_CACHE) == 1


def test_identity_and_key_follow_demo_credentials_profile_and_symbols(clock, monkeypatch):
    config = {"exec_exchange_id": "binance", "exec_base_url": ""}
    bridge = SimpleNamespace(exchange_id="binance")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "production-key")
    monkeypatch.setenv("BINANCE_SECRET", "production-secret")

    key = history_cache.cache_key(config, bridge, [BTC, ETH])
    expected = hmac.new(
        history_cache._CACHE_SECRET,
        b"BINANCE\x00https://demo-api.binance.com\x00demo-key\x00demo-secret",
        hashlib.sha256,
    ).hexdigest()
    assert key == ("binance", "https://demo-api.binance.com", expected, (BTC, ETH))
    assert key != history_cache.cache_key(config, bridge, [ETH, BTC])
    assert "demo-key" not in repr(key) and "demo-secret" not in repr(key)

    monkeypatch.setenv("BINANCE_DEMO_SECRET", "rotated")
    assert history_cache.cache_key(config, bridge, [BTC, ETH]) != key
    config["exec_base_url"] = "https://api.binance.com"
    production = history_cache.cache_key(config, bridge, [BTC, ETH])
    assert production[1] == "https://api.binance.com"
    assert production[2] != key[2]

    monkeypatch.delenv("BINANCE_SECRET")
    assert history_cache.credential_identity(config) is None
    assert history_cache.cache_key(config, bridge, [BTC]) is None
    assert history_cache.credential_identity({}) is None


@pytest.mark.parametrize("retry_after", [None, False, "1200", -1, float("nan"), float("inf"), 300])
def test_cooldown_uses_minimum_for_invalid_or_short_retry_after(clock, retry_after):
    history_cache.start_cooldown({"retry_after_seconds": retry_after})
    clock.now = 999.999
    assert history_cache.is_rate_limited()
    clock.now = 1000.0
    assert not history_cache.is_rate_limited()


def test_cooldown_honors_long_retry_after(clock):
    history_cache.start_cooldown({"retry_after_seconds": 1200})
    clock.now = 1299.999
    assert history_cache.is_rate_limited()
    clock.now = 1300.0
    assert not history_cache.is_rate_limited()


def test_exception_retry_after_combines_headers_dates_and_ban(clock, monkeypatch):
    wall_now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(history_cache, "wall_time", lambda: wall_now.timestamp())
    ban_ms = int((wall_now + timedelta(seconds=1200)).timestamp() * 1000)
    exc = RuntimeError(f"IP banned until {ban_ms}")
    exc.headers = {"retry-after": "500"}
    exc.response = SimpleNamespace(headers={
        "Retry-After": format_datetime(wall_now + timedelta(seconds=1500), usegmt=True),
    })
    client = SimpleNamespace(last_response_headers={"RETRY-AFTER": "1300"})
    assert history_cache.retry_after_from_exception(exc, client) == 1500


def test_exception_retry_after_ignores_invalid_headers(clock, monkeypatch):
    monkeypatch.setattr(history_cache, "wall_time", lambda: 100.0)
    for value in (None, True, -1, "nan", "not-a-date", "Wed, 21 Oct 2015 07:28:00"):
        exc = RuntimeError("safe error")
        exc.headers = {"Retry-After": value}
        assert history_cache.retry_after_from_exception(exc) is None
    exc = RuntimeError("IP banned until 1000000000000ignored")
    assert history_cache.retry_after_from_exception(exc) is None


def test_mapping_headers_and_http_status_rate_limits(clock):
    for status in (418, 429):
        exc = RuntimeError("redacted")
        exc.response = SimpleNamespace(status_code=status, headers=UserDict({"Retry-After": "1200"}))
        assert history_cache.is_rate_limit_error(exc)
        assert history_cache.retry_after_from_exception(exc) == 1200
    mapped = RuntimeError("redacted")
    mapped.response = UserDict({"status": 429, "headers": UserDict({"Retry-After": "1250"})})
    assert history_cache.is_rate_limit_error(mapped)
    assert history_cache.retry_after_from_exception(mapped) == 1250
    assert not history_cache.is_rate_limit_error(RuntimeError("HTTP 503"))
    history_cache.start_cooldown({"retry_after_seconds": 1800})
    clock.now += 1
    history_cache.start_cooldown({"retry_after_seconds": 20})
    assert history_cache._RATE_LIMIT_UNTIL == 1900
