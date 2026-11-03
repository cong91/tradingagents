"""Control-panel CSRF guard API tests."""

import json

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


# --- CSRF guard (§0) ---------------------------------------------------------


def test_csrf_rejects_safelisted_content_type(client):
    response = client.post(
        "/api/watchlist",
        content=json.dumps({"symbol": "XRP-USD"}),
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_csrf_rejects_foreign_origin(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


def test_csrf_rejects_cross_site_fetch(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_rejected"


def test_csrf_allows_control_panel_origin(client):
    response = client.post(
        "/api/watchlist", json={"symbol": "XRP-USD"}, headers={"Origin": "http://localhost:3000"}
    )
    assert response.status_code == 201
