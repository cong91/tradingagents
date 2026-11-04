"""Control-panel watchlist API tests."""

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit

# §0 CSRF: every state-changing method must send application/json — DELETE too.
_JSON = {"Content-Type": "application/json"}


# --- watchlist ----------------------------------------------------------------


def test_watchlist_add_delete_roundtrip(client):
    # Startup seeds the engine defaults (BTC-USD, ETH-USD), so assert on SOL-USD.
    added = client.post("/api/watchlist", json={"symbol": "sol-usd"})
    assert added.status_code == 201
    symbols = [item["symbol"] for item in added.json()["symbols"]]
    assert "SOL-USD" in symbols
    assert "sol-usd" not in symbols  # normalized to Yahoo form
    assert all(item["added_at"] for item in added.json()["symbols"])

    duplicate = client.post("/api/watchlist", json={"symbol": "SOL-USD"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"

    empty = client.post("/api/watchlist", json={"symbol": "   "})
    assert empty.status_code == 400

    malformed = client.post("/api/watchlist", json={"symbol": "MOVR-USD, LITE"})
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "validation_error"

    removed = client.delete("/api/watchlist/sol-usd", headers=_JSON)
    assert removed.status_code == 200
    assert "SOL-USD" not in [item["symbol"] for item in removed.json()["symbols"]]

    again = client.delete("/api/watchlist/SOL-USD", headers=_JSON)
    assert again.status_code == 404

    # Deleting the last symbol is allowed: an empty watchlist is valid (§2).
    for item in removed.json()["symbols"]:
        response = client.delete(f"/api/watchlist/{item['symbol']}", headers=_JSON)
        assert response.status_code == 200
    assert client.get("/api/watchlist").json()["symbols"] == []
