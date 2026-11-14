"""API + CLI consumers of the outcome ledger: read-only aggregates, honest
unavailable states, the old decision-score endpoints untouched."""

import json
from decimal import Decimal

import pytest

from tests.server_api_fixtures import (  # noqa: F401 - shared fixtures re-export
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


def test_outcomes_endpoint_reports_unavailable_for_an_empty_ledger(client):
    response = client.get("/api/outcomes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert "not zero" in payload["reason"]
    assert payload["totals"] is None
    assert "Research framework" in payload["disclaimer"]


def test_outcomes_endpoint_carries_totals_and_marks_unknown_funding(client):
    from server import outcome_store

    outcome_store.record_outcome(
        account_id="binance:demo:" + "c" * 64,
        position={
            "cycle_id": "futures:BTC/USDT:USDT:api1", "client_order_id": "ta_api1",
            "venue_order_id": "1", "symbol": "BTC/USDT:USDT",
            "market_type": "perpetual", "side": "long",
            "planned_quantity": Decimal("1"), "filled_quantity": Decimal("1"),
            "opened_at": "2026-11-10T00:00:00+00:00",
            "closed_at": "2026-11-10T05:00:00+00:00",
            "attribution": "managed", "strategy_version": "1",
            "provenance": {},
        },
        fills=[{
            "venue_trade_id": "501", "venue_order_id": "1",
            "symbol": "BTC/USDT:USDT", "side": "sell", "role": "exit",
            "quantity": Decimal("1"), "price": Decimal("105"),
            "fee": Decimal("0.1"), "fee_currency": "USDT",
            "realized_pnl": Decimal("5"), "realized_pnl_currency": "USDT",
            "occurred_at": "2026-11-10T05:00:00+00:00",
        }],
        exits=[], funding=[],
    )
    payload = client.get("/api/outcomes").json()
    assert payload["status"] == "partial"
    assert payload["positions"]["total"] == 1
    assert payload["totals"]["realized_by_currency"]["USDT"] == "5"
    net_row = payload["totals"]["net_by_currency"][0]
    assert net_row["currency"] == "USDT"
    # No funding rows for this currency: the net row must not claim funding.
    assert net_row["funding_known"] is False
    assert "USDT" in payload["funding_unknown_currencies"]


def test_state_changing_methods_are_refused_on_outcomes(client):
    assert client.post("/api/outcomes", json={}).status_code in (403, 405, 415)
    assert client.delete("/api/outcomes").status_code in (403, 405, 415)


def test_old_decision_score_endpoint_keeps_its_semantics(client):
    """The decision-level backtest score (hit rate / mean alpha per rating)
    keeps its own shape and meaning — unchanged by the outcome ledger."""
    response = client.get("/api/backtest")
    assert response.status_code == 200
    payload = response.json()
    assert "runs" in payload
    # The outcomes endpoint never claims to be the decision score: its shape
    # is ledger-shaped, not rating-score shaped.
    outcomes = client.get("/api/outcomes").json()
    assert "by_rating" not in json.dumps(outcomes)
    assert "hit_rate" not in json.dumps(outcomes)


def test_outcomes_window_filter_selects_intersecting_coverage(client):
    """Finding 3: the window params reach the store and filter coverage rows;
    a malformed bound is a 400-grade refusal, never a silent no-filter."""
    from decimal import Decimal

    from server import outcome_store
    from tradingagents.outcomes.coverage import build_coverage
    from tradingagents.outcomes.currency import CurrencyAmount

    for index, (start, end) in enumerate((
        ("2026-11-10T00:00:00+00:00", "2026-11-10T01:00:00+00:00"),
        ("2026-11-11T00:00:00+00:00", "2026-11-11T01:00:00+00:00"),
    )):
        outcome_store.upsert_coverage(build_coverage(
            account_id=f"binance:demo:{'d' * 64}-{index}",
            window_start=start,
            window_end=end,
            fills_found=2,
            attributed_fill_count=2,
            venue_realized=[CurrencyAmount("USDT", Decimal("2"))],
            ledger_realized=[CurrencyAmount("USDT", Decimal("2"))],
            unattributed_manual_detected=0,
            partial_entry_count=0,
            source_status="ok",
            warnings=[],
            snapshot={"cycle_id": f"cycle-{index}"},
        ))
    payload = client.get(
        "/api/outcomes",
        params={"window_start": "2026-11-10T00:30:00+00:00",
                "window_end": "2026-11-10T02:00:00+00:00"},
    )
    assert payload.status_code == 200
    rows = payload.json()["coverage"]
    assert len(rows) == 1
    assert rows[0]["snapshot"]["cycle_id"] == "cycle-0"


def test_cli_outcomes_reads_the_panel_database(postgres_schema):
    """The CLI consumer prints the ledger summary against the scoped DSN
    (postgres_schema has set TRADINGAGENTS_DATABASE_URL)."""
    from typer.testing import CliRunner

    from cli.main import app

    result = CliRunner().invoke(app, ["outcomes"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "unavailable"  # empty schema: honest unavailable


def test_cli_outcomes_fails_loudly_without_a_dsn(monkeypatch):
    from typer.testing import CliRunner

    from cli.main import app

    monkeypatch.delenv("TRADINGAGENTS_DATABASE_URL", raising=False)
    result = CliRunner().invoke(app, ["outcomes"])
    assert result.exit_code == 1
    assert "TRADINGAGENTS_DATABASE_URL" in result.output
