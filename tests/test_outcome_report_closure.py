"""Report counts distinguish verified closure from legacy timestamps."""

from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest

from server import outcome_report, outcome_store

pytestmark = pytest.mark.unit


def _record_position(client_order_id, *, closed_at, provenance):
    outcome_store.record_outcome(
        account_id="binance:demo:report-test",
        position={
            "cycle_id": client_order_id,
            "client_order_id": client_order_id,
            "venue_order_id": client_order_id,
            "symbol": "BTC/USDT:USDT",
            "market_type": "perpetual",
            "side": "long",
            "planned_quantity": Decimal("1"),
            "filled_quantity": Decimal("1"),
            "opened_at": "2026-11-13T12:00:00+00:00",
            "closed_at": closed_at,
            "attribution": "managed",
            "provenance": provenance,
        },
        fills=[],
        exits=[],
        funding=[],
    )


def test_report_preserves_legacy_timestamps_and_counts_only_proof_v2(postgres_schema):
    verified_closed = datetime(2026, 11, 13, 13, tzinfo=UTC)
    legacy_closed = datetime(2026, 11, 13, 14, tzinfo=UTC)
    _record_position(
        "verified", closed_at=verified_closed,
        provenance={"closure_proof_version": 2, "closure_proof": {"version": 2}},
    )
    _record_position("legacy", closed_at=legacy_closed, provenance={"source": "legacy"})

    positions = outcome_report.summary()["positions"]

    assert positions["total"] == 2
    assert positions["closed"] == 2
    assert positions["recorded_closed"] == 2
    assert positions["verified_closed"] == 1
    assert positions["last_closed"] == verified_closed
    assert positions["last_recorded_closed"] == legacy_closed
    assert positions["closure_evidence"] == {
        "status": "partial",
        "verified_closed": 1,
        "unverified_positions": 1,
        "unverified_closed_timestamps": 1,
        "reason": "Closure is verified only against complete, identity-bound stored fill history.",
    }


def test_explicit_revocation_preserves_recorded_timestamps(postgres_schema):
    verified_closed = datetime(2026, 11, 13, 13, tzinfo=UTC)
    legacy_closed = datetime(2026, 11, 13, 14, tzinfo=UTC)
    _record_position(
        "verified", closed_at=verified_closed,
        provenance={"closure_proof_version": 2, "closure_proof": {"version": 2}},
    )
    _record_position("legacy", closed_at=legacy_closed, provenance={"source": "legacy"})
    _record_position("open", closed_at=None, provenance={"source": "test"})

    for _ in range(2):
        for cycle_id in ("legacy", "open"):
            outcome_store.revoke_cycle_closure(cycle_id, "test_revocation")
    with psycopg.connect(postgres_schema) as conn:
        rows = conn.execute(
            """SELECT cycle_id, closed_at, provenance->>'recorded_closed_at',
                      provenance->>'closure_verdict', provenance->>'closure_verdict_reason'
               FROM outcome_positions WHERE cycle_id IN ('legacy', 'open')
               ORDER BY cycle_id"""
        ).fetchall()
    assert rows[0][0:2] == ("legacy", None)
    assert datetime.fromisoformat(rows[0][2]) == legacy_closed
    assert rows[0][3:] == ("unverified", "test_revocation")
    assert rows[1] == ("open", None, None, "unverified", "test_revocation")
    positions = outcome_report.summary()["positions"]
    assert positions["closed"] == positions["recorded_closed"] == 2
    assert positions["verified_closed"] == 1
    assert positions["last_closed"] == verified_closed
    assert positions["last_recorded_closed"] == legacy_closed
    assert positions["closure_evidence"]["unverified_closed_timestamps"] == 1
