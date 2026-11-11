"""L1: outcome attribution is evidence-only, never a time/price heuristic."""

from types import SimpleNamespace

import pytest

from tradingagents.execution.futures_entry_journal import (
    entry_journal_leg,
    managed_live_provenance,
)
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.outcomes.attribution import (
    CycleOrder,
    attribute_exit_fill,
    attribute_fill,
    attribute_recovered_fill,
    bracket_exit_order_ids,
    recovered_client_order_ids,
)

pytestmark = pytest.mark.unit

_ORDER = PlannedOrder(
    ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", side="buy",
    quantity=0.1, estimated_price=100.0, cost=10.0,
)

_IDENTITY = SimpleNamespace(
    exchange="binance", profile="demo", account_type="futures",
    identity_hash="a" * 64,
)


def _marker(client_order_id: str = "ta_abc123") -> dict:
    return managed_live_provenance(_ORDER, client_order_id, identity=_IDENTITY, venue="binance:demo")


def _journal(client_order_id: str = "ta_abc123", placed_ids: list | None = None) -> dict:
    return {
        "task_id": "entry-intent:ta_abc123",
        "symbol": "BTC/USDT:USDT",
        "entry_order_id": client_order_id,
        "close_side": "sell",
        "quantity": 0.1,
        "legs": [{"entry_provenance": _marker(client_order_id)}],
        "placed_ids": placed_ids if placed_ids is not None else [],
        "status": "completed",
    }


def _cycle(order_id: str = "1", journal: dict | None = None) -> CycleOrder:
    return CycleOrder(
        cycle_id="futures:BTC/USDT:USDT:1", order_id=order_id,
        symbol="BTC/USDT:USDT", journal_task=journal,
    )


def test_managed_when_order_id_and_marker_match():
    journal = _journal(placed_ids=["1"])
    decision = attribute_fill("1", "BTC/USDT:USDT", _cycle("1", journal))
    assert decision.attribution == "managed"
    assert decision.client_order_id == "ta_abc123"
    assert decision.cycle_id == "futures:BTC/USDT:USDT:1"


def test_missing_marker_stays_unattributed():
    journal = _journal(placed_ids=["1"])
    journal["legs"][0]["entry_provenance"]["kind"] = "something_else"
    decision = attribute_fill("1", "BTC/USDT:USDT", _cycle("1", journal))
    assert decision.attribution == "unattributed"
    assert decision.client_order_id is None


def test_order_id_mismatch_stays_unattributed_even_when_symbol_and_time_match():
    # A fill that shares symbol and would share the window is NOT the cycle's
    # trade: without the venue order id match there is no attribution, no
    # retro-assignment heuristic.
    journal = _journal(placed_ids=["1"])
    decision = attribute_fill("999", "BTC/USDT:USDT", _cycle("1", journal))
    assert decision.attribution == "unattributed"
    assert decision.reason == "order_id_mismatch"


def test_fill_without_order_id_stays_unattributed():
    decision = attribute_fill(None, "BTC/USDT:USDT", _cycle("1", _journal()))
    assert decision.attribution == "unattributed"


def test_symbol_mismatch_stays_unattributed():
    journal = _journal(placed_ids=["1"])
    decision = attribute_fill("1", "ETH/USDT:USDT", _cycle("1", journal))
    assert decision.attribution == "unattributed"


def test_cycle_without_journal_stays_unattributed():
    decision = attribute_fill("1", "BTC/USDT:USDT", _cycle("1", journal=None))
    assert decision.attribution == "unattributed"
    assert decision.reason == "managed_live_entry_marker_absent"


def test_disagreeing_marker_copies_yield_no_marker():
    journal = _journal(placed_ids=["1"])
    second = _journal()["legs"][0]["entry_provenance"]
    second["client_order_id"] = "ta_other"
    journal["legs"].append({"entry_provenance": second})
    decision = attribute_fill("1", "BTC/USDT:USDT", _cycle("1", journal))
    assert decision.attribution == "unattributed"


def test_recovered_fill_maps_to_the_same_client_order_id():
    journal = _journal(client_order_id="ta_abc123", placed_ids=["77"])
    recovered = recovered_client_order_ids([journal])
    decision = attribute_recovered_fill("77", "BTC/USDT:USDT", recovered, "c1")
    assert decision.attribution == "recovered"
    assert decision.client_order_id == "ta_abc123"
    assert decision.cycle_id == "c1"


def test_unknown_order_id_has_no_recovery_evidence():
    recovered = recovered_client_order_ids([_journal(placed_ids=["77"])])
    decision = attribute_recovered_fill("999", "BTC/USDT:USDT", recovered, "c1")
    assert decision.attribution == "unattributed"


def test_invalid_marker_yields_no_recovered_identity():
    journal = _journal(placed_ids=["77"])
    journal["legs"][0]["entry_provenance"]["version"] = 99
    assert recovered_client_order_ids([journal]) == {}


# --- Blocker 2 fix: exit fills attribute through the bracket task evidence ---


def _bracket_task(entry_order_id: str = "1", *, placed_ids: list | None = None,
                  identity_hash: str = "a" * 64, symbol: str = "BTC/USDT:USDT") -> dict:
    legs = [
        {"name": "stop-loss", "status": "placed", "order_id": "9002"},
        {"name": "take-profit-1", "status": "placed", "order_id": "9003"},
    ]
    return {
        "task_id": f"bracket:{symbol}:{entry_order_id}",
        "symbol": symbol,
        "entry_order_id": entry_order_id,
        "close_side": "sell",
        "legs": legs,
        "placed_ids": placed_ids if placed_ids is not None else ["9002", "9003"],
        "account_identity_hash": identity_hash,
    }


def test_entry_journal_leg_always_carries_the_marker():
    """Blocker 1 fix: the journal builder itself embeds the provenance marker,
    so no caller can write a marker-less managed entry leg."""
    leg = entry_journal_leg(
        _ORDER, "ta_abc123", quantity="0.1", created_at="2026-11-10T00:00:00+00:00",
        identity=_IDENTITY, venue="binance:demo", config={},
    )
    marker = leg["entry_provenance"]
    assert marker["kind"] == "managed_live_entry"
    assert marker["client_order_id"] == "ta_abc123"
    assert marker["account_identity"]["hash"] == "a" * 64


def test_bracket_exit_evidence_collects_leg_and_placed_order_ids():
    evidence = bracket_exit_order_ids(
        _bracket_task("1"), entry_order_id="1",
        identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    )
    assert set(evidence) == {"9002", "9003"}
    assert evidence["9002"] == "bracket_leg_stop-loss"


def test_bracket_exit_evidence_requires_the_same_identity_hash():
    evidence = bracket_exit_order_ids(
        _bracket_task("1", identity_hash="b" * 64), entry_order_id="1",
        identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    )
    assert evidence == {}


def test_bracket_exit_evidence_requires_the_same_symbol():
    evidence = bracket_exit_order_ids(
        _bracket_task("1", symbol="ETH/USDT:USDT"), entry_order_id="1",
        identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    )
    assert evidence == {}


def test_bracket_exit_evidence_requires_the_same_entry_order_id():
    evidence = bracket_exit_order_ids(
        _bracket_task("999"), entry_order_id="1",
        identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    )
    assert evidence == {}


def test_exit_fill_attributes_to_the_same_position():
    evidence = bracket_exit_order_ids(
        _bracket_task("1"), entry_order_id="1",
        identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    )
    decision = attribute_exit_fill(
        "9002", "BTC/USDT:USDT", evidence, "ta_abc123", "c1",
    )
    assert decision.attribution == "managed"
    assert decision.client_order_id == "ta_abc123"
    assert decision.cycle_id == "c1"
    assert decision.reason == "bracket_leg_stop-loss"


def test_exit_fill_without_bracket_evidence_stays_unattributed():
    decision = attribute_exit_fill(
        "88888", "BTC/USDT:USDT", {"9002": "bracket_leg_stop-loss"},
        "ta_abc123", "c1",
    )
    assert decision.attribution == "unattributed"
    assert decision.reason == "no_bracket_exit_evidence"


def test_missing_bracket_task_contributes_no_exit_evidence():
    assert bracket_exit_order_ids(
        None, entry_order_id="1", identity_hash="a" * 64, symbol="BTC/USDT:USDT",
    ) == {}
