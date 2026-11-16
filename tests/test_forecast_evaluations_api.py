"""Read-only forecast evaluation API tests."""

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


def test_empty_evaluations_are_unavailable_and_not_zero(client):
    response = client.get("/api/forecast-evaluations")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["directional_accuracy"] is None
    assert body["band_containment"] is None
    assert body["coverage"]["total_count"] == 0
    assert "no forecast evaluation rows are available" in body["coverage_warnings"]
    assert any("self-reported" in warning for warning in body["coverage_warnings"])
    assert "does not change execution decisions" in body["disclaimer"]


def test_evaluation_limit_validation_is_read_only(client):
    response = client.get("/api/forecast-evaluations", params={"limit": 0})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert client.post("/api/forecast-evaluations", json={}).status_code in (403, 405, 415)
