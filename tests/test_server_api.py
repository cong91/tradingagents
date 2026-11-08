"""Control-panel health API tests."""

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


# --- health -----------------------------------------------------------------


def test_health_reports_dry_mode_and_counts(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["exec_mode"] == "dry"  # exec_live default False, env flag blanked by conftest
    assert body["generated_at"].endswith("Z")
    assert body["active_run_id"] is None
    assert body["pending_approvals"] == 0
    assert {"openai", "google", "anthropic"}.issubset(body["llm_key_configured"])
    assert any(key.startswith(("quick:", "deep:")) for key in body["llm_key_configured"])
