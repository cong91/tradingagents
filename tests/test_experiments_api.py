"""Experiment registry API + CLI consumers: read-only aggregates, honest
unavailable states, verdicts pass through verbatim, no winner vocabulary."""

import json

import pytest

from tests.server_api_fixtures import (  # noqa: F401 - shared fixtures re-export
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


def test_experiments_endpoint_reports_unavailable_for_an_empty_registry(client):
    response = client.get("/api/experiments")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert "empty" in payload["reason"]
    assert "Research framework" in payload["disclaimer"]


def test_experiments_endpoint_is_read_only(client):
    assert client.post("/api/experiments", json={}).status_code in (403, 405, 415)
    assert client.delete("/api/experiments").status_code in (403, 405, 415)


def test_unknown_run_id_answers_unavailable(client):
    response = client.get("/api/experiments/no-such-run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert "no experiment registered" in payload["reason"]


def test_endpoint_shape_is_registry_shaped_not_ledger_or_score_shaped(client):
    registry = client.get("/api/experiments").json()
    ledger = client.get("/api/outcomes").json()
    assert "by_rating" not in json.dumps(registry)
    assert "hit_rate" not in json.dumps(registry)
    # The outcome ledger and the experiment registry keep separate shapes.
    assert "outcome_positions" not in json.dumps(registry)
    assert "comparisons" not in json.dumps(ledger)
    # The old decision-score endpoint keeps its own semantics.
    backtest = client.get("/api/backtest").json()
    assert "runs" in backtest


def test_cli_experiments_reads_the_panel_database(postgres_schema):
    from typer.testing import CliRunner

    from cli.main import app

    result = CliRunner().invoke(app, ["experiments"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "unavailable"  # empty schema: honest unavailable


def test_cli_experiments_fails_loudly_without_a_dsn(monkeypatch):
    from typer.testing import CliRunner

    from cli.main import app

    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["experiments"])
    assert result.exit_code == 1
    assert "TRADINGAGENTS_DATABASE_URL" in result.output
