"""Configuration changes must not publish a superseded portfolio snapshot."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from server import portfolio, snapshot_coordinator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("changed", [
    {"exec_live": False},
    {"exec_exchange_id": "bybit"},
    {"exec_base_url": "https://api.binance.com"},
    {"exec_quote_currency": "USDC"},
    {"exec_log_path": "new-audit.jsonl"},
    {"risk_derivatives_round_trip_fee_rate": 0.002},
])
def test_configuration_change_during_load_discards_snapshot(monkeypatch, changed):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    snapshot_coordinator.reset_for_tests()
    config = {
        "exec_live": True,
        "exec_exchange_id": "binance",
        "exec_base_url": "https://demo-api.binance.com",
        "exec_quote_currency": "USDT",
    }
    client = SimpleNamespace(apiKey="fake-key", secret="fake-secret")
    bridge = SimpleNamespace(
        exchange_id="binance", config=config.copy(), client=lambda: client,
        effective_mode=lambda: "live",
    )
    started, release = Event(), Event()

    def load(_config, bridge=None):
        started.set()
        assert release.wait(5)
        return {"cash": 123, "positions_source": "venue"}

    monkeypatch.setattr(portfolio, "get_config", lambda: config.copy())
    monkeypatch.setattr(portfolio, "_build_bridge", lambda _config: bridge)
    monkeypatch.setattr(portfolio, "_get_portfolio", load)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(portfolio.get_portfolio)
            assert started.wait(5)
            config.update(changed)
            release.set()
            body = future.result(timeout=5)
        assert body["cash"] is None
        assert body["positions_source"] == "unavailable"
        assert body["snapshot"]["error"] == "account_identity_changed"
        if changed.get("exec_live", True):
            subsequent = portfolio.get_portfolio()
            assert subsequent["cash"] == 123
            assert subsequent["snapshot"]["cache"] == "miss"
    finally:
        release.set()
        snapshot_coordinator.reset_for_tests()


def test_credential_rotation_during_load_discards_snapshot(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    snapshot_coordinator.reset_for_tests()
    config = {"exec_live": True, "exec_exchange_id": "binance"}
    clients = [
        SimpleNamespace(apiKey="fake-key-a", secret="fake-secret-a"),
        SimpleNamespace(apiKey="fake-key-b", secret="fake-secret-b"),
    ]
    selected = [clients[0]]
    started, release = Event(), Event()

    def build(_config):
        client = selected[0]
        return SimpleNamespace(
            exchange_id="binance", config={"exec_base_url": "https://demo-api.binance.com"},
            client=lambda: client, effective_mode=lambda: "live",
        )

    def load(_config, bridge=None):
        started.set()
        assert release.wait(5)
        return {"cash": 123, "positions_source": "venue"}

    monkeypatch.setattr(portfolio, "get_config", lambda: config.copy())
    monkeypatch.setattr(portfolio, "_build_bridge", build)
    monkeypatch.setattr(portfolio, "_get_portfolio", load)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(portfolio.get_portfolio)
            assert started.wait(5)
            selected[0] = clients[1]
            release.set()
            body = future.result(timeout=5)
        assert body["cash"] is None
        assert body["positions_source"] == "unavailable"
        assert body["snapshot"]["error"] == "account_identity_changed"
    finally:
        release.set()
        snapshot_coordinator.reset_for_tests()


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("changed", ["credential", "configuration", "client"])
def test_live_read_rejects_changed_binding_for_direct_and_coordinator(monkeypatch, sync, changed):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", str(sync))
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    monkeypatch.setattr(portfolio.performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    snapshot_coordinator.reset_for_tests()
    config = {"exec_live": True, "exec_exchange_id": "binance", "exec_quote_currency": "USDT"}
    clients = [
        SimpleNamespace(apiKey="fake-key-a", secret="fake-secret-a"),
        SimpleNamespace(apiKey="fake-key-b", secret="fake-secret-b"),
    ]
    selected = [clients[0]]
    entered, resumed = Barrier(2), Barrier(2)
    used = []

    class FakeBridge:
        exchange_id = "binance"

        def __init__(self, bridge_config):
            self.config = bridge_config.copy()
            self.bound = selected[0]

        def client(self):
            return self.bound

        def effective_mode(self):
            return "live"

        def sync_portfolio(self):
            used.append(self.client())
            entered.wait(timeout=5)
            resumed.wait(timeout=5)
            if changed == "client":
                self.bound = clients[1]
            return SimpleNamespace(cash=123, currency="USDT", positions=[])

    monkeypatch.setattr(portfolio, "get_config", lambda: config.copy())
    monkeypatch.setattr(portfolio, "_build_bridge", FakeBridge)
    monkeypatch.setattr(portfolio.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(portfolio, "read_futures_snapshot", lambda bridge: {
        "status": "ok", "positions": [], "positions_read": {"observed_at": None},
    })
    monkeypatch.setattr(portfolio, "attach_position_costs", lambda *args: None)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(portfolio.get_portfolio)
            entered.wait(timeout=5)
            if changed == "credential":
                selected[0] = clients[1]
            elif changed == "configuration":
                config["exec_quote_currency"] = "USDC"
            resumed.wait(timeout=5)
            body = future.result(timeout=5)
        assert used == [clients[0]]
        assert body["cash"] is None
        assert body["positions_source"] == "unavailable"
        assert body["snapshot"]["error"] == "account_identity_changed"
        assert snapshot_coordinator._CACHE == {}
    finally:
        snapshot_coordinator.reset_for_tests()


@pytest.mark.parametrize("sync", [False, True])
def test_reverted_credentials_cannot_hit_snapshot_from_changed_read(monkeypatch, sync):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", str(sync))
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    monkeypatch.setattr(portfolio.performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    snapshot_coordinator.reset_for_tests()
    config = {"exec_live": True, "exec_exchange_id": "binance"}
    clients = [SimpleNamespace(apiKey="key-a", secret="secret-a"),
               SimpleNamespace(apiKey="key-b", secret="secret-b")]
    selected = [clients[0]]
    calls = []
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)

    class FakeBridge:
        exchange_id = "binance"

        def __init__(self, bridge_config):
            self.config = bridge_config.copy()
            self.bound = selected[0]

        def client(self):
            return self.bound

        def effective_mode(self):
            return "live"

    def load(_config, bridge=None):
        calls.append(bridge.client())
        if len(calls) == 1:
            selected[0] = clients[1]
        return {"cash": len(calls), "positions_source": "venue"}

    monkeypatch.setattr(portfolio, "get_config", lambda: config.copy())
    monkeypatch.setattr(portfolio, "_build_bridge", FakeBridge)
    monkeypatch.setattr(portfolio, "_get_portfolio", load)
    try:
        rejected = portfolio.get_portfolio()
        assert rejected["snapshot"]["error"] == "account_identity_changed"
        assert snapshot_coordinator._CACHE == {}
        selected[0] = clients[0]
        clock.now += 5
        current = portfolio.get_portfolio()
        assert current["cash"] == 2
        assert calls == [clients[0], clients[0]]
        if sync:
            assert current["snapshot"]["cache"] == "miss"
    finally:
        snapshot_coordinator.reset_for_tests()
