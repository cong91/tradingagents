"""Control-panel approvals API tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from server import approvals as approvals_mod, autoloop, control_store, main as server_main
from tests.server_api_exchange import _FakeExchange
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_mode_scheduler(monkeypatch):
    monkeypatch.setattr(autoloop, "ensure_started", lambda: None)
    monkeypatch.setattr(autoloop, "_wakeup", Event())


# --- approvals -----------------------------------------------------------------


def _enqueue(**overrides) -> dict:
    plan = {
        "run_id": "r_test", "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT",
        "signal": "Buy", "side": "buy", "quantity": 0.0012, "price_est": 83078.0,
        # RiskGuard's cost/quantity consistency check is exact (rel_tol=1e-9),
        # so the fixture notional must equal quantity * price_est, not a 2-dp
        # rounding of it.
        "cost": 0.0012 * 83078.0,
    }
    plan.update(overrides)
    return approvals_mod.enqueue(plan)


def test_approvals_list_counts_pending(client):
    _enqueue()
    _enqueue(ticker="ETH-USD", ccxt_symbol="ETH/USDT")
    body = client.get("/api/approvals").json()
    assert body["pending_count"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["status"] == "pending"
    assert body["generated_at"].endswith("Z")

    limited = client.get("/api/approvals", params={"limit": 1}).json()
    assert len(limited["items"]) == 1


def test_approve_requires_spot_mode_even_with_live_enabled(client, tmp_path, monkeypatch):
    set_config({"auto_spot_enabled": False, "exec_live": True})
    _engine_audit(tmp_path)
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "mode_closed"
    assert client.get("/api/approvals").json()["pending_count"] == 1  # untouched
    assert exchange.orders == []


def _inject_bridge(monkeypatch, exchange: _FakeExchange) -> None:
    """Point the approve pipeline's venue seam (approvals._build_bridge) at the fake."""

    def build(config, authorization):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(
            config=config, exchange_factory=lambda: exchange, authorization=authorization,
        )

    monkeypatch.setattr(approvals_mod, "_build_bridge", build)


def _engine_audit(tmp_path) -> Path:
    audit_file = tmp_path / "engine-audit.jsonl"
    set_config({"exec_log_path": str(audit_file)})
    return audit_file


@pytest.mark.parametrize("other_enabled", [False, True])
def test_approve_spot_requires_spot_mode(client, tmp_path, monkeypatch, other_enabled):
    set_config({
        "exec_live": True,
        "auto_spot_enabled": False,
        "auto_futures_enabled": other_enabled,
    })
    _engine_audit(tmp_path)
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue(market="spot")

    response = client.post(f"/api/approvals/{item['id']}/approve", json={})

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "mode_closed"
    assert client.get("/api/approvals").json()["pending_count"] == 1
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "pending"
    assert stored["resolved_at"] is None
    assert exchange.orders == []
    assert client.post(f"/api/approvals/{item['id']}/reject", json={}).status_code == 200


@pytest.mark.parametrize("spot_enabled", [False, True])
@pytest.mark.parametrize("futures_enabled", [False, True])
@pytest.mark.parametrize("live_enabled", [False, True])
def test_approve_derivatives_is_unsupported_before_claim(
    client, tmp_path, monkeypatch, spot_enabled, futures_enabled, live_enabled,
):
    set_config({
        "exec_live": live_enabled,
        "auto_spot_enabled": spot_enabled,
        "auto_futures_enabled": futures_enabled,
    })
    _engine_audit(tmp_path)
    bridge_attempts = []

    def unexpected_bridge(config, authorization):
        bridge_attempts.append((config, authorization))
        raise AssertionError("derivatives approvals must not construct a bridge")

    monkeypatch.setattr(approvals_mod, "_build_bridge", unexpected_bridge)
    item = _enqueue(market="derivatives")

    response = client.post(f"/api/approvals/{item['id']}/approve", json={})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "unsupported_market"
    assert "derivatives" in error["message"].lower()
    assert "spot" in error["message"].lower()
    assert bridge_attempts == []
    assert client.get("/api/approvals").json()["pending_count"] == 1
    assert approvals_mod._load_items()[0] == item
    assert client.post(f"/api/approvals/{item['id']}/reject", json={}).status_code == 200


def test_approve_releases_claim_when_spot_mode_disabled_mid_request(client, tmp_path, monkeypatch):
    key, other_key = "auto_spot_enabled", "auto_futures_enabled"
    enabled = client.put(
        "/api/operator-config",
        json={"settings": {key: True, other_key: True}, "confirm": True, "reason": "start"},
    )
    assert enabled.status_code == 200
    set_config({"exec_live": True})
    _engine_audit(tmp_path)
    sync_started = Event()
    resume_sync = Event()

    class _PausedVenue(_FakeExchange):
        def fetch_balance(self, params=None):
            sync_started.set()
            if not resume_sync.wait(timeout=15):
                raise TimeoutError("portfolio sync was not released")
            return super().fetch_balance(params)

    exchange = _PausedVenue()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue(market="spot")

    # A separate portal lets the synchronous portfolio read stay paused while
    # another operator request is served. No second lifespan is started.
    with closing(TestClient(server_main.app)) as operator_client, ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            client.post, f"/api/approvals/{item['id']}/approve", json={},
        )
        try:
            assert sync_started.wait(timeout=15), "approval did not reach portfolio sync"
            assert approvals_mod._load_items()[0]["status"] == "approved"
            disabled = operator_client.put(
                "/api/operator-config",
                json={"settings": {key: False}, "confirm": True, "reason": "stop market"},
            )
            assert disabled.status_code == 200
            assert control_store.load_operator_settings()[key] is False
            assert control_store.load_operator_settings()[other_key] is True
        finally:
            resume_sync.set()
        response = pending.result(timeout=15)

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "mode_closed"
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "pending"
    assert "disabled before execution" in stored["resolution"]
    assert stored["executed"] is None
    assert stored["order_id"] is None
    assert exchange.orders == []
    assert any(
        event["action"] == "approval_execute_deferred" and event["approval_id"] == item["id"]
        for event in control_store.load_server_audit()
    )
    assert client.post(f"/api/approvals/{item['id']}/reject", json={}).status_code == 200


def test_approve_managed_live_ignores_legacy_gate_change(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "auto_spot_enabled": True})
    audit_file = _engine_audit(tmp_path)

    class _PermissionClosedVenue(_FakeExchange):
        def fetch_balance(self, params=None):
            balance = super().fetch_balance(params)
            set_config({"exec_live": False})
            return balance

    exchange = _PermissionClosedVenue()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed"] is True
    assert body["mode"] == "live"
    assert body["order_id"] == "7570510"
    assert body["audit_action"] == "buy"
    assert body["risk"] == {"allowed": True, "halted": False, "reason": None}

    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["executed"] is True
    assert stored["mode"] == "live"
    assert stored["order_id"] == "7570510"

    again = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"

    trail = "\n".join(json.dumps(e) for e in control_store.load_server_audit())
    assert "approval_granted" in trail
    assert "approval_executed" in trail
    assert exchange.orders == [("BTC/USDT", "market", "buy", "0.00120000")]
    engine_log = audit_file.read_text(encoding="utf-8")
    assert '"action": "buy"' in engine_log
    assert '"confirmed": true' in engine_log


def test_approve_live_places_order_through_bridge(client, tmp_path, monkeypatch):
    set_config({"exec_live": False, "auto_spot_enabled": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["executed"] is True
    assert body["mode"] == "live"
    assert body["order_id"] == "7570510"
    assert exchange.orders == [("BTC/USDT", "market", "buy", "0.00120000")]

    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["order_id"] == "7570510"
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "buy"' in engine_log
    assert '"confirmed": true' in engine_log
    assert "7570510" in engine_log


def test_approve_refuses_mock_item_and_stays_pending(client, monkeypatch):
    set_config({"exec_live": True, "auto_spot_enabled": True})
    _inject_bridge(monkeypatch, _FakeExchange())  # must never be reached
    item = _enqueue(mock=True)
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mock_not_executable"
    assert approvals_mod._load_items()[0]["status"] == "pending"  # still rejectable
    rejected = client.post(f"/api/approvals/{item['id']}/reject", json={"reason": "demo"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_approve_refuses_non_rebuildable_item_and_stays_pending(client):
    set_config({"exec_live": True, "auto_spot_enabled": True})
    item = _enqueue(side=None, quantity=None, signal="Hold")
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert approvals_mod._load_items()[0]["status"] == "pending"


def test_approve_execute_failed_when_guard_refuses(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "auto_spot_enabled": True})
    _engine_audit(tmp_path)
    _inject_bridge(monkeypatch, _FakeExchange())
    item = _enqueue(quantity=100.0, cost=9999999.0)  # breaches the per-asset cap at re-check
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "risk_halted"
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "execute_failed"
    assert "risk guard refused execution" in stored["resolution"]
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "rejected_by_risk"' in engine_log


def test_approve_venue_portfolio_failure_stays_pending(client, monkeypatch):
    set_config({"exec_live": True, "auto_spot_enabled": True})

    class _DeadVenue(_FakeExchange):
        def fetch_balance(self):
            raise RuntimeError("exchangeInfo 502 Bad Gateway")

    _inject_bridge(monkeypatch, _DeadVenue())
    item = _enqueue()

    response = client.post(f"/api/approvals/{item['id']}/approve", json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "exchange_unreachable"
    assert approvals_mod._load_items()[0]["status"] == "pending"


def test_approve_exchange_failure_marks_execute_failed(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "auto_spot_enabled": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    _inject_bridge(monkeypatch, _FakeExchange(fail_create=True))
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "execute_failed"
    assert approvals_mod._load_items()[0]["status"] == "execute_failed"
    engine_log = (tmp_path / "engine-audit.jsonl").read_text(encoding="utf-8")
    assert '"action": "rejected_at_execute"' in engine_log


def test_approve_survives_placed_but_unaudited_order(client, tmp_path, monkeypatch):
    # bridge.py:464-469: the order exists on the venue but the audit write failed —
    # the item must resolve to executed, never to a retryable failure.
    import tradingagents.execution.bridge as bridge_mod

    set_config({"exec_live": True, "auto_spot_enabled": True, "exec_log_path": str(tmp_path / "engine-audit.jsonl")})
    exchange = _FakeExchange()
    _inject_bridge(monkeypatch, exchange)

    def broken_append(path, event):
        raise OSError("disk full")

    monkeypatch.setattr(bridge_mod, "append_event", broken_append)
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "executed"
    assert body["executed"] is True
    assert body["order_id"] == "7570510"
    assert "WAS placed" in body["warning"]
    assert exchange.orders  # the order exists on the venue — never retry
    stored = approvals_mod._load_items()[0]
    assert stored["status"] == "executed"
    assert stored["order_id"] == "7570510"


def test_approve_blocked_when_risk_guard_halted_today(client, tmp_path):
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text(
        json.dumps({"event": "halt", "timestamp": f"{_today()}T00:00:00+00:00",
                    "reason": "daily loss cap breached"}) + "\n",
        encoding="utf-8",
    )
    set_config({"exec_live": True, "auto_spot_enabled": True, "exec_log_path": str(audit_file)})
    item = _enqueue()
    response = client.post(f"/api/approvals/{item['id']}/approve", json={})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "risk_halted"
    assert error["details"]["reason"] == "daily loss cap breached"


def test_reject_records_reason_and_never_takes_the_gate(client, tmp_path):
    item = _enqueue()  # exec_live stays False: reject must not care
    response = client.post(f"/api/approvals/{item['id']}/reject", json={"reason": "too expensive"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["resolution"] == "too expensive"
    assert body["resolved_at"]

    trail = "\n".join(json.dumps(e) for e in control_store.load_server_audit())
    assert "approval_denied" in trail

    again = client.post(f"/api/approvals/{item['id']}/reject", json={})
    assert again.status_code == 409


def test_approval_queue_survives_a_restart(client, tmp_path):
    item = _enqueue()
    reloaded = approvals_mod._load_items()  # what a fresh server process would read
    assert [loaded["id"] for loaded in reloaded] == [item["id"]]
    assert reloaded[0]["status"] == "pending"
