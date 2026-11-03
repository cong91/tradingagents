"""Portfolio cache behavior when coalesced display reads meet cooldown."""

from concurrent.futures import Future
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from server import performance_history_cache, portfolio, snapshot_coordinator

pytestmark = pytest.mark.unit


def test_coalesced_cooldown_response_is_not_cached_by_later_waiter(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)
    snapshot_coordinator.reset_for_tests()
    client = SimpleNamespace(apiKey="fake-key", secret="fake-secret")
    bridge = SimpleNamespace(
        exchange_id="binance",
        config={"exec_base_url": "https://demo-api.binance.com"},
        client=lambda: client,
        effective_mode=lambda: "live",
    )
    monkeypatch.setattr(portfolio, "get_config", lambda: {"exec_live": True})
    monkeypatch.setattr(portfolio, "_build_bridge", lambda _config: bridge)
    loader_started = Event()
    release_loader = Event()
    second_waiting = Event()
    second_returned = Event()
    calls = []
    results = {}
    errors = []

    def load(_config, bridge=None):
        calls.append(1)
        if len(calls) == 1:
            loader_started.set()
            assert release_loader.wait(5)
            performance_history_cache.start_cooldown()
            return {"value": "cooldown"}
        return {"value": "fresh"}

    original_result = Future.result

    def controlled_result(future, timeout=None):
        if current_thread().name == "first":
            assert second_returned.wait(5)
        elif current_thread().name == "second":
            second_waiting.set()
        return original_result(future, timeout)

    monkeypatch.setattr(portfolio, "_get_portfolio", load)
    monkeypatch.setattr(Future, "result", controlled_result)

    def request(name):
        try:
            results[name] = portfolio.get_portfolio()
        except Exception as exc:
            errors.append(exc)
        finally:
            if name == "second":
                second_returned.set()

    first = Thread(target=request, args=("first",), name="first")
    second = Thread(target=request, args=("second",), name="second")
    try:
        first.start()
        assert loader_started.wait(5)
        second.start()
        assert second_waiting.wait(5)
        release_loader.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []
        assert calls == [1]
        assert all(body["value"] == "cooldown" for body in results.values())
        assert all("_snapshot_no_cache" not in body for body in results.values())
        assert snapshot_coordinator._CACHE == {}

        monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
        clock.now = 106.0
        fresh = portfolio.get_portfolio()
        assert fresh["value"] == "fresh"
        assert fresh["snapshot"]["cache"] == "miss"
        assert calls == [1, 1]
    finally:
        release_loader.set()
        second_returned.set()
        if first.ident is not None:
            first.join(5)
        if second.ident is not None:
            second.join(5)
        snapshot_coordinator.reset_for_tests()
