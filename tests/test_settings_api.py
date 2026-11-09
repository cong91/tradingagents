"""Control-panel settings API tests."""

from threading import Event

import pytest

from server import autoloop, control_store, main as server_main
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import _ENV_OVERRIDES

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_mode_scheduler(monkeypatch):
    monkeypatch.setattr(autoloop, "ensure_started", lambda: None)
    monkeypatch.setattr(autoloop, "_wakeup", Event())


# --- settings ----------------------------------------------------------------


def test_settings_get_masks_credentials_and_lists_paths(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcd1234")
    # This machine may hold real exchange credentials; the API only ever
    # reports configured/masked, so blank them here to make the assert exact.
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_SECRET", raising=False)
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()

    editable = [row for row in body["settings"] if row["editable"]]
    readonly = {row["key"] for row in body["settings"] if not row["editable"]}
    # Contract §1 prose says "24 rows" but its own list — and the file — have 25
    # (_ENV_OVERRIDES, default_config.py:10-53). The mapping is authoritative.
    assert len(editable) == len(_ENV_OVERRIDES) - 1
    assert "llm_provider" in {row["key"] for row in editable}
    assert readonly == {
        "results_dir", "data_cache_dir", "memory_log_path", "exec_log_path", "exec_base_url",
    }

    credentials = {item["env_var"]: item for item in body["credentials"]}
    assert credentials["OPENAI_API_KEY"]["configured"] is True
    assert credentials["OPENAI_API_KEY"]["masked"].endswith("1234")
    assert "sk-test-abcd" not in response.text  # never the raw key
    assert credentials["BINANCE_DEMO_API_KEY"]["configured"] is False
    assert credentials["BINANCE_DEMO_API_KEY"]["masked"] is None

    assert body["gates_readonly"] == {
        "auto_spot": False,
        "auto_futures": False,
        "any_auto_enabled": False,
    }
    assert body["effective_exec_mode"] == "dry"


def test_settings_put_persists_to_postgres_and_reapplies_at_boot(client):
    response = client.put("/api/settings", json={"settings": {"max_debate_rounds": 2}})
    assert response.status_code == 200
    assert response.json()["applied_at"]
    assert get_config()["max_debate_rounds"] == 2  # in-memory, effective for new runs
    assert control_store.load_engine_settings() == {"max_debate_rounds": 2}

    # A fresh process re-applies the stored value at boot (the same call the
    # lifespan makes) — the env layer alone no longer carries UI saves.
    set_config({"max_debate_rounds": 1})
    server_main._apply_engine_settings()
    assert get_config()["max_debate_rounds"] == 2


def test_boot_engine_settings_sit_between_env_and_operator_overrides(monkeypatch):
    prior = get_config()
    try:
        monkeypatch.setattr(
            control_store,
            "load_engine_settings",
            lambda: {
                "max_debate_rounds": 3,
                "risk_max_derivatives_leverage": 2,
                # A gate key and a read-only key must never enter the config
                # through stored engine settings.
                "exec_live": True,
                "exec_base_url": "https://evil.example",
            },
        )
        monkeypatch.setattr(
            control_store,
            "load_operator_settings",
            lambda: {"risk_max_derivatives_leverage": 3},
        )
        monkeypatch.setattr(
            server_main.auto_modes,
            "ensure_modes_migrated",
            lambda: {"auto_spot_enabled": False, "auto_futures_enabled": False},
        )
        server_main._apply_engine_settings()
        server_main._apply_operator_config()
        config = get_config()
        assert config["max_debate_rounds"] == 3
        assert config["risk_max_derivatives_leverage"] == 3  # operator override wins
        assert config["exec_live"] is False
        assert config["exec_base_url"] == prior["exec_base_url"]
    finally:
        set_config({
            key: prior[key]
            for key in ("max_debate_rounds", "risk_max_derivatives_leverage")
        })


def test_settings_put_forbids_gate_and_unknown_keys(client):
    response = client.put("/api/settings", json={"settings": {"exec_live": True}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_key"
    assert get_config()["exec_live"] is False  # the FR5 config gate never moves via API


def test_settings_credentials_follow_binance_venue_url(client, monkeypatch):
    set_config({"exec_base_url": "https://api.binance.com"})
    monkeypatch.setenv("BINANCE_API_KEY", "prod-key-1234")
    monkeypatch.setenv("BINANCE_SECRET", "prod-secret-5678")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key-9999")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret-0000")

    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    credential_vars = {item["env_var"] for item in body["credentials"]}
    assert "BINANCE_API_KEY" in credential_vars
    assert "BINANCE_SECRET" in credential_vars
    assert "BINANCE_DEMO_API_KEY" not in credential_vars
    assert "prod-key-1234" not in response.text
    assert "demo-key-9999" not in response.text


def test_settings_put_forbids_venue_url_switch(client):
    response = client.put(
        "/api/settings", json={"settings": {"exec_base_url": "https://api.binance.com"}}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_key"


def test_settings_put_coercion_error_names_env_var(client):
    response = client.put("/api/settings", json={"settings": {"max_debate_rounds": "abc"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "coercion_error"
    assert error["details"]["env_var"] == "TRADINGAGENTS_MAX_DEBATE_ROUNDS"


@pytest.mark.parametrize(
    "key,other_key,market,other_market",
    [
        ("auto_spot_enabled", "auto_futures_enabled", "spot", "futures"),
        ("auto_futures_enabled", "auto_spot_enabled", "futures", "spot"),
    ],
)
def test_operator_partial_mode_update_preserves_other_mode(
    client, key, other_key, market, other_market,
):
    enabled = client.put(
        "/api/operator-config",
        json={"settings": {other_key: True}, "confirm": True, "reason": "enable other market"},
    )
    assert enabled.status_code == 200

    for value in (True, False):
        response = client.put(
            "/api/operator-config",
            json={"settings": {key: value}, "confirm": True, "reason": "change one market"},
        )
        assert response.status_code == 200
        assert response.json()["modes"] == {market: value, other_market: True}
        stored = control_store.load_operator_settings()
        assert stored[key] is value
        assert stored[other_key] is True
        assert get_config()[key] is value
        assert get_config()[other_key] is True
        gates = client.get("/api/settings").json()["gates_readonly"]
        assert gates[f"auto_{market}"] is value
        assert gates[f"auto_{other_market}"] is True
        assert gates["any_auto_enabled"] is True


def test_operator_dual_mode_update_persists_reloads_and_audits(client):
    updates = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    response = client.put(
        "/api/operator-config",
        json={"settings": updates, "confirm": True, "reason": "enable both", "actor": "operator"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["modes"] == {"spot": True, "futures": True}
    assert body["gates"] == {
        "auto_spot": True, "auto_futures": True, "any_auto_enabled": True,
    }
    assert body["effective_exec_mode"] == "dry"
    stored = control_store.load_operator_settings()
    assert all(stored[key] is value for key, value in updates.items())
    rows = {row["key"]: row for row in body["settings"]}
    for key in updates:
        assert rows[key]["value"] is True
        assert rows[key]["default"] is False
        assert rows[key]["stored_value"] is True
        assert rows[key]["updated_by"] == "operator"

    events = [e for e in control_store.load_server_audit() if e["action"] == "operator_config_updated"]
    event = events[-1]
    assert event["changes"] == {key: {"from": False, "to": True} for key in updates}
    assert event["gate_changes"] == sorted(updates)
    assert event["reason"] == "enable both"
    assert event["actor"] == "operator"

    set_config(dict.fromkeys(updates, False))
    server_main._apply_operator_config()
    assert all(get_config()[key] is True for key in updates)


@pytest.mark.parametrize(
    "settings,extra,status,code",
    [
        ({"auto_spot_enabled": True}, {"reason": "enable"}, 412, "gate_confirmation_required"),
        ({"auto_futures_enabled": True}, {"confirm": False, "reason": "enable"}, 412, "gate_confirmation_required"),
        ({"auto_spot_enabled": True, "auto_futures_enabled": True}, {"confirm": True}, 422, "reason_required"),
        ({"auto_spot_enabled": True, "auto_futures_enabled": True}, {"confirm": True, "reason": "  "}, 422, "reason_required"),
    ],
)
def test_operator_mode_update_requires_confirmation_and_reason(client, settings, extra, status, code):
    before = control_store.load_operator_settings()
    response = client.put("/api/operator-config", json={"settings": settings, **extra})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert control_store.load_operator_settings() == before
    assert client.get("/api/operator-config").json()["modes"] == {"spot": False, "futures": False}


@pytest.mark.parametrize("key", ["auto_spot_enabled", "auto_futures_enabled"])
def test_operator_disabling_mode_also_requires_confirmation(client, key):
    response = client.put(
        "/api/operator-config",
        json={"settings": {key: True}, "confirm": True, "reason": "enable"},
    )
    assert response.status_code == 200
    response = client.put(
        "/api/operator-config", json={"settings": {key: False}, "reason": "disable"},
    )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "gate_confirmation_required"
    assert control_store.load_operator_settings()[key] is True
    assert get_config()[key] is True


@pytest.mark.parametrize("invalid_key", ["auto_spot_enabled", "auto_futures_enabled"])
def test_operator_dual_update_rejects_invalid_boolean_without_partial_change(client, invalid_key):
    updates = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    updates[invalid_key] = "true"
    before = control_store.load_operator_settings()

    response = client.put(
        "/api/operator-config",
        json={"settings": updates, "confirm": True, "reason": "enable both"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "coercion_error"
    assert response.json()["error"]["details"]["key"] == invalid_key
    assert control_store.load_operator_settings() == before
    assert client.get("/api/operator-config").json()["modes"] == {"spot": False, "futures": False}


def test_operator_dual_update_rolls_back_when_second_database_write_fails(client):
    import psycopg

    from server import postgres

    before = control_store.load_operator_settings()
    with postgres.session() as conn:
        conn.execute(
            "ALTER TABLE control_operator_settings ADD CONSTRAINT reject_futures_enable "
            "CHECK (key <> 'auto_futures_enabled' OR value <> 'true'::jsonb)"
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        client.put(
            "/api/operator-config",
            json={
                "settings": {"auto_spot_enabled": True, "auto_futures_enabled": True},
                "confirm": True,
                "reason": "enable both",
            },
        )

    assert control_store.load_operator_settings() == before
    assert client.get("/api/operator-config").json()["modes"] == {"spot": False, "futures": False}


@pytest.mark.parametrize(
    "key,other_key,market,other_market",
    [
        ("auto_spot_enabled", "auto_futures_enabled", "spot", "futures"),
        ("auto_futures_enabled", "auto_spot_enabled", "futures", "spot"),
    ],
)
def test_operator_reset_persists_false_and_leaves_other_mode_enabled(
    client, key, other_key, market, other_market,
):
    enabled = client.put(
        "/api/operator-config",
        json={
            "settings": {key: True, other_key: True}, "confirm": True, "reason": "enable both",
        },
    )
    assert enabled.status_code == 200
    response = client.request("DELETE", f"/api/operator-config/{key}", json={})

    assert response.status_code == 200
    assert response.json()["modes"] == {market: False, other_market: True}
    stored = control_store.load_operator_settings()
    assert stored[key] is False
    assert stored[other_key] is True
    assert get_config()[key] is False
    assert get_config()[other_key] is True

    set_config({key: True, other_key: False})
    server_main._apply_operator_config()
    assert get_config()[key] is False
    assert get_config()[other_key] is True


@pytest.mark.parametrize(
    "key", ["auto_spot_enabled", "auto_futures_enabled"],
)
def test_operator_mode_wakes_scheduler_on_update_and_reset(client, key):
    autoloop._wakeup.clear()
    enabled = client.put(
        "/api/operator-config",
        json={"settings": {key: True}, "confirm": True, "reason": "start"},
    )
    assert enabled.status_code == 200
    assert autoloop._wakeup.is_set()
    assert control_store.load_autoloop_state()["enabled"] is True

    autoloop._wakeup.clear()
    reset = client.request("DELETE", f"/api/operator-config/{key}", json={})
    assert reset.status_code == 200
    assert autoloop._wakeup.is_set()
    assert control_store.load_autoloop_state()["enabled"] is False


@pytest.mark.parametrize("key", ["auto_spot_enabled", "auto_futures_enabled", "exec_live"])
def test_operator_modes_are_not_writable_through_engine_settings(client, key):
    response = client.put("/api/settings", json={"settings": {key: True}})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_key"
    assert get_config().get(key) is False


@pytest.mark.parametrize("key", ["exec_live", "exec_auto_confirm", "exec_derivatives"])
def test_operator_config_rejects_legacy_gates(client, key):
    response = client.put(
        "/api/operator-config",
        json={"settings": {key: True}, "confirm": True, "reason": "enable"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_key"
