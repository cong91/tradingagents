"""Control-panel settings API tests."""

import pytest

from server import settings as settings_mod
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import _ENV_OVERRIDES

pytestmark = pytest.mark.unit


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

    assert body["gates_readonly"]["exec_live_config"] is False
    assert body["effective_exec_mode"] == "dry"


def test_settings_put_applies_in_memory_and_persists_env(client, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# keep this comment\nTRADINGAGENTS_OUTPUT_LANGUAGE=English\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_file)

    response = client.put("/api/settings", json={"settings": {"max_debate_rounds": 2}})
    assert response.status_code == 200
    assert response.json()["applied_at"]
    assert get_config()["max_debate_rounds"] == 2  # in-memory, effective for new runs

    text = env_file.read_text(encoding="utf-8")
    assert "# keep this comment" in text  # unrelated lines survive
    assert "TRADINGAGENTS_OUTPUT_LANGUAGE=English" in text
    assert "TRADINGAGENTS_MAX_DEBATE_ROUNDS=2" in text


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
