"""Focused evidence rules for outcome-position closure."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from server.outcome_closure import fill_history_complete, verified_closure

pytestmark = pytest.mark.unit

START = datetime(2026, 11, 13, 12, tzinfo=UTC)
END = START + timedelta(hours=1)
IDENTITY = {
    "order_id": "venue-entry",
    "client_order_id": "client-entry",
    "symbol": "BTC/USDT:USDT",
    "venue_key": "binance:demo",
    "account_id": "binance:demo:identity-hash",
    "account_identity": {
        "exchange": "binance", "profile": "demo", "type": "futures", "hash": "identity-hash",
    },
}
FILL_HISTORY = {
    "source_status": "ok",
    "source_certified_complete": True,
    "requested_start": START.isoformat(),
    "requested_end": END.isoformat(),
    "observed_start": START.isoformat(),
    "observed_end": END.isoformat(),
    "source_warnings": ["funding_history_unverified"],
    "malformed_fill_count": 0,
    "duplicate_fill_count": 0,
    "unattributed_fill_count": 0,
    "contradictory_fill_count": 0,
}
FILLS = [
    {
        "trade_id": "entry-trade", "order_id": "venue-entry", "symbol": IDENTITY["symbol"],
        "side": "buy", "role": "entry", "quantity": "1", "price": "100",
        "occurred_at": (START + timedelta(minutes=1)).isoformat(),
    },
    {
        "trade_id": "exit-trade", "order_id": "venue-exit", "symbol": IDENTITY["symbol"],
        "side": "sell", "role": "exit", "quantity": "1", "price": "110",
        "occurred_at": (START + timedelta(minutes=2)).isoformat(),
    },
]


def _proof(**overrides):
    args = {
        "fill_history": dict(FILL_HISTORY),
        "planned_quantity": Decimal("1"),
        "entry_quantity": Decimal("1"),
        "exit_quantity": Decimal("1"),
        "opened_at": FILLS[0]["occurred_at"],
        "closed_at": FILLS[1]["occurred_at"],
        "minimum_balance": Decimal("0"),
        "entry_order_final": None,
        "expected_identity": IDENTITY,
        "fill_facts": FILLS,
        "stored_fill_facts": FILLS,
    }
    args.update(overrides)
    return verified_closure(**args)


def test_fill_history_requires_certified_ok_source_and_trustworthy_bounds():
    base = {
        "source_status": "ok",
        "source_certified_complete": True,
        "requested_start": START,
        "requested_end": END,
        "observed_start": START,
        "observed_end": END,
        "source_warnings": ["funding_history_unverified"],
    }
    assert fill_history_complete(**base)
    assert not fill_history_complete(**{**base, "source_status": "partial"})
    assert not fill_history_complete(**{**base, "source_certified_complete": False})
    assert not fill_history_complete(**{**base, "observed_start": None})
    assert not fill_history_complete(**{**base, "source_warnings": ["page_limit_reached"]})


@pytest.mark.parametrize("field", [
    "malformed_fill_count", "duplicate_fill_count", "unattributed_fill_count",
    "contradictory_fill_count",
])
def test_fill_history_rejects_each_fill_integrity_failure(field):
    args = {
        "source_status": "ok", "source_certified_complete": True,
        "requested_start": START, "requested_end": END,
        "observed_start": START, "observed_end": END, "source_warnings": [],
    }
    assert not fill_history_complete(**args, **{field: 1})


def test_verified_closure_stores_version_two_and_exact_fill_facts():
    proof = _proof()
    assert proof["version"] == 2
    assert proof["entry_quantity_basis"] == "planned_quantity"
    assert len(proof["fill_facts"]) == 2
    assert _proof(stored_fill_facts=[FILLS[0]]) is None


def test_partial_entry_requires_exact_terminal_order_identity():
    terminal = {
        **IDENTITY,
        "status": "canceled",
        "original_quantity": "1",
        "filled_quantity": "0.5",
    }
    facts = [
        {**FILLS[0], "quantity": "0.5"},
        {**FILLS[1], "quantity": "0.5"},
    ]
    proof = _proof(
        entry_quantity=Decimal("0.5"),
        exit_quantity=Decimal("0.5"),
        entry_order_final=terminal,
        fill_facts=facts,
        stored_fill_facts=facts,
    )
    assert proof["entry_quantity_basis"] == "terminal_partial_entry_order"
    assert _proof(
        entry_quantity=Decimal("0.5"),
        exit_quantity=Decimal("0.5"),
        fill_facts=facts,
        stored_fill_facts=facts,
    ) is None
    assert _proof(
        entry_quantity=Decimal("0.5"),
        exit_quantity=Decimal("0.5"),
        entry_order_final={**terminal, "account_id": "other-account"},
        fill_facts=facts,
        stored_fill_facts=facts,
    ) is None


def test_source_status_partial_cannot_issue_closure_proof():
    history = {**FILL_HISTORY, "source_status": "partial"}
    assert _proof(fill_history=history) is None
