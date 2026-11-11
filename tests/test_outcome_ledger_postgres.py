"""L4: outcome ledger PostgreSQL persistence — idempotency, partial fills,
recovery merge, coverage reconciliation, migration 0008.

These tests need PostgreSQL through the ``postgres_schema`` fixture (a private
schema per test, migrated, dropped); they skip without a test DSN.
"""

from decimal import Decimal

import psycopg
import pytest

from tradingagents.outcomes.coverage import build_coverage
from tradingagents.outcomes.currency import CurrencyAmount
from tradingagents.outcomes.netting import REALIZED_PNL_EXCLUDES_FEE

pytestmark = pytest.mark.unit

_POSITION = {
    "cycle_id": "futures:BTC/USDT:USDT:c1",
    "client_order_id": "ta_abc",
    "venue_order_id": "1",
    "symbol": "BTC/USDT:USDT",
    "market_type": "perpetual",
    "side": "long",
    "planned_quantity": Decimal("1.0"),
    "filled_quantity": Decimal("1.0"),
    "opened_at": "2026-11-10T00:00:00+00:00",
    "closed_at": "2026-11-10T05:00:00+00:00",
    "attribution": "managed",
    "strategy_version": "1",
    "provenance": {"source": "test"},
}

_FILL = {
    "venue_trade_id": "100",
    "venue_order_id": "1",
    "symbol": "BTC/USDT:USDT",
    "side": "buy",
    "role": "entry",
    "quantity": Decimal("1.0"),
    "price": Decimal("100"),
    "fee": Decimal("0.1"),
    "fee_currency": "USDT",
    "realized_pnl": None,
    "realized_pnl_currency": None,
    "realized_pnl_semantics": None,
    "occurred_at": "2026-11-10T00:00:00+00:00",
}

_EXIT = {
    "leg": "target",
    "exit_reason": "target",
    "quantity": Decimal("1.0"),
    "price": Decimal("110"),
    "net_per_currency": [{"currency": "USDT", "amount": "9.9"}],
    "r_multiple": None,
    "closed_at": "2026-11-10T05:00:00+00:00",
    "provenance": {"cycle_id": "futures:BTC/USDT:USDT:c1"},
}

ACCOUNT_ID = "binance:demo:" + "a" * 64  # exchange:profile:identity hash


def _record():
    from server import outcome_store

    return outcome_store.record_outcome(
        account_id=ACCOUNT_ID, position=dict(_POSITION),
        fills=[dict(_FILL)], exits=[dict(_EXIT)], funding=[],
    )


def test_migration_0008_creates_outcome_tables(postgres_schema):
    with psycopg.connect(postgres_schema) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'outcome_%'"
            ).fetchall()
        ]
    assert set(names) >= {
        "outcome_positions", "outcome_fills", "outcome_exits",
        "outcome_funding", "outcome_coverage", "outcome_ledger_state",
    }


def test_ingest_is_idempotent_rereading_the_same_window_changes_no_total(postgres_schema):
    from server import outcome_store

    first = _record()
    assert first["positions_new"] == 1
    assert first["fills_new"] == 1
    second = _record()
    assert second == {
        "positions_new": 0, "fills_new": 0, "fills_duplicate": 1,
        "exits_new": 0, "funding_new": 0,
    }
    summary = outcome_store.summary()
    assert summary["positions"]["total"] == 1
    assert summary["fills"] == 1
    # Totals did not move: same window, same venue trades, same ledger.
    assert summary["totals"]["realized_by_currency"] == {}


def test_partial_entry_keeps_one_position_with_filled_below_planned(postgres_schema):
    from server import outcome_store

    position = {**_POSITION, "filled_quantity": Decimal("0.5")}
    fill_half = {**_FILL, "quantity": Decimal("0.5")}
    outcome_store.record_outcome(
        account_id=ACCOUNT_ID, position=position,
        fills=[fill_half], exits=[], funding=[],
    )
    summary = outcome_store.summary()
    assert summary["positions"]["total"] == 1
    with psycopg.connect(postgres_schema) as conn:
        row = conn.execute(
            "SELECT planned_quantity, filled_quantity FROM outcome_positions"
        ).fetchone()
    assert row == (Decimal("1.0"), Decimal("0.5"))


def test_same_intent_second_ingest_merges_into_one_position(postgres_schema):
    """Store contract: a second ingest of the same intent (e.g. the recovery
    path re-recording under the same client_order_id) merges into the one
    position row — the position key is (account_id, client_order_id)."""
    from server import outcome_store

    counts = _record()
    assert counts["positions_new"] == 1
    second_fill = {
        **_FILL,
        "venue_trade_id": "200",
        "venue_order_id": "77",  # different venue order id, same intent
    }
    counts = outcome_store.record_outcome(
        account_id=ACCOUNT_ID,
        position={**_POSITION, "attribution": "recovered"},
        fills=[second_fill], exits=[], funding=[],
    )
    assert counts["positions_new"] == 0  # merged, never a second position
    assert counts["fills_new"] == 1
    summary = outcome_store.summary()
    assert summary["positions"]["total"] == 1
    assert summary["fills"] == 2


def test_managed_position_without_client_order_id_is_rejected_by_the_schema(
    postgres_schema,
):
    """The schema enforces honesty: a managed/recovered row must carry the
    client_order_id key it claims to be idempotent on."""
    from server import outcome_store

    with pytest.raises(psycopg.errors.CheckViolation):
        outcome_store.record_outcome(
            account_id=ACCOUNT_ID,
            position={**_POSITION, "client_order_id": None},
            fills=[dict(_FILL)], exits=[], funding=[],
        )


def test_mixed_currency_realized_totals_stay_separate(postgres_schema):
    from server import outcome_store

    outcome_store.record_outcome(
        account_id=ACCOUNT_ID, position=dict(_POSITION),
        fills=[dict(_FILL)], exits=[], funding=[],
    )
    pnl_usdt = {
        **_FILL, "venue_trade_id": "300", "role": "exit", "side": "sell",
        "realized_pnl": Decimal("10"), "realized_pnl_currency": "USDT",
        "realized_pnl_semantics": REALIZED_PNL_EXCLUDES_FEE,
    }
    pnl_bnb_fee = {
        **_FILL, "venue_trade_id": "301", "role": "exit", "side": "sell",
        "realized_pnl": Decimal("5"), "realized_pnl_currency": "USDT",
        "fee": Decimal("0.01"), "fee_currency": "BNB",
        "realized_pnl_semantics": REALIZED_PNL_EXCLUDES_FEE,
    }
    outcome_store.record_outcome(
        account_id=ACCOUNT_ID,
        position={**_POSITION, "client_order_id": "ta_def"},
        fills=[pnl_usdt, pnl_bnb_fee], exits=[], funding=[],
    )
    totals = outcome_store.summary()["totals"]
    assert totals["realized_by_currency"]["USDT"] == "15"
    assert totals["fee_by_currency"]["BNB"] == "0.01"


def test_net_subtracts_fee_only_with_recorded_exclusion_semantics(postgres_schema):
    """Finding 5: the fee subtraction decision is recorded in ingestion, not
    assumed — a currency whose realized fills lack the exclusion semantics
    keeps its fee OUT of the net and is flagged fee_unknown."""
    from server import outcome_store

    with_semantics = {
        **_FILL, "venue_trade_id": "310", "role": "exit", "side": "sell",
        "realized_pnl": Decimal("10"), "realized_pnl_currency": "USDT",
        "fee": Decimal("0.5"), "fee_currency": "USDT",
        "realized_pnl_semantics": REALIZED_PNL_EXCLUDES_FEE,
    }
    outcome_store.record_outcome(
        account_id=ACCOUNT_ID,
        position={**_POSITION, "client_order_id": "ta_sem1"},
        fills=[with_semantics], exits=[], funding=[],
    )
    net_rows = {row["currency"]: row for row in outcome_store.summary()["totals"]["net_by_currency"]}
    assert net_rows["USDT"]["amount"] == "9.5"  # 10 - 0.5, recorded contract
    assert net_rows["USDT"]["fee_known"] is True

    without_semantics = {
        **_FILL, "venue_trade_id": "311", "role": "exit", "side": "sell",
        "realized_pnl": Decimal("20"), "realized_pnl_currency": "ETH",
        "fee": Decimal("0.3"), "fee_currency": "ETH",
        "realized_pnl_semantics": None,
    }
    outcome_store.record_outcome(
        account_id=ACCOUNT_ID,
        position={**_POSITION, "client_order_id": "ta_sem2"},
        fills=[without_semantics], exits=[], funding=[],
    )
    summary = outcome_store.summary()
    net_rows = {row["currency"]: row for row in summary["totals"]["net_by_currency"]}
    assert net_rows["ETH"]["amount"] == "20"  # fee NOT subtracted: unproven
    assert net_rows["ETH"]["fee_known"] is False
    assert "ETH" in summary["fee_unknown_currencies"]


def test_summary_window_filter_selects_intersecting_coverage_rows(postgres_schema):
    """Finding 3: window_start/window_end filter the coverage rows to those
    intersecting the window; a malformed bound is refused, never read as
    'no filter'."""
    from server import outcome_store

    for index, (start, end) in enumerate((
        ("2026-11-10T00:00:00+00:00", "2026-11-10T01:00:00+00:00"),
        ("2026-11-11T00:00:00+00:00", "2026-11-11T01:00:00+00:00"),
    )):
        outcome_store.upsert_coverage(build_coverage(
            account_id=f"{ACCOUNT_ID}-{index}",
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
            snapshot={"cycle_id": f"c{index}"},
        ))
    summary = outcome_store.summary(
        window_start="2026-11-10T00:30:00+00:00",
        window_end="2026-11-10T02:00:00+00:00",
    )
    assert len(summary["coverage"]) == 1
    assert summary["coverage"][0]["snapshot"]["cycle_id"] == "c0"
    with pytest.raises(ValueError):
        outcome_store.summary(window_start="not-a-timestamp")


def test_coverage_rows_are_per_cycle_and_never_summed(postgres_schema):
    """Finding 6: each coverage row reconciles ONE cycle's own window; rows
    overlap by construction, so the row keeps its cycle id in the snapshot
    and consumers must not add them up."""
    from server import outcome_store

    for index in range(2):
        outcome_store.upsert_coverage(build_coverage(
            account_id=ACCOUNT_ID,
            window_start="2026-11-10T00:00:00+00:00",
            window_end="2026-11-10T01:00:00+00:00",
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
    summary = outcome_store.summary()
    assert summary["coverage"][0]["fills_found"] == 2
    cycle_ids = {row["snapshot"]["cycle_id"] for row in summary["coverage"]}
    assert len(summary["coverage"]) == len(cycle_ids)  # distinct per-cycle rows


def test_funding_rows_carry_venue_semantics_and_stay_idempotent(postgres_schema):
    from server import outcome_store

    funding = [{
        "funding_at": "2026-11-10T02:00:00+00:00",
        "amount": Decimal("1.5"),
        "currency": "USDT",
        "venue_semantics": "binance: income positive = received",
    }]
    first = outcome_store.record_outcome(
        account_id=ACCOUNT_ID, position=dict(_POSITION),
        fills=[dict(_FILL)], exits=[], funding=funding,
    )
    assert first["funding_new"] == 1
    second = outcome_store.record_outcome(
        account_id=ACCOUNT_ID, position=dict(_POSITION),
        fills=[dict(_FILL)], exits=[], funding=funding,
    )
    assert second["funding_new"] == 0
    summary = outcome_store.summary()
    funding_totals = summary["totals"]["funding_by_currency"]["USDT"]
    assert funding_totals["received"] == "1.5"
    assert funding_totals["venue_semantics"] == "binance: income positive = received"


def test_summary_unavailable_when_ledger_is_empty_not_zero(postgres_schema):
    from server import outcome_store

    summary = outcome_store.summary()
    assert summary["status"] == "unavailable"
    assert "not zero" in summary["reason"]
    assert summary["totals"] is None


def test_coverage_row_upsert_preserves_unsupported_status(postgres_schema):
    from server import outcome_store

    row = build_coverage(
        account_id=ACCOUNT_ID,
        window_start="2026-11-10T00:00:00+00:00",
        window_end="2026-11-10T01:00:00+00:00",
        fills_found=3,
        attributed_fill_count=1,
        venue_realized=[CurrencyAmount("USDT", Decimal("30"))],
        ledger_realized=[CurrencyAmount("USDT", Decimal("10"))],
        unattributed_manual_detected=2,
        partial_entry_count=0,
        source_status="unsupported",
        warnings=["unsupported_exchange"],
    )
    outcome_store.upsert_coverage(row)
    summary = outcome_store.summary()
    assert len(summary["coverage"]) == 1
    stored = summary["coverage"][0]
    assert stored["status"] == "unsupported"
    assert stored["unattributed_manual_detected"] == 2
    assert stored["fills_found"] == 3
    # The published difference is the honest gap, not a hidden zero.
    diff = {item["currency"]: item["amount"] for item in stored["realized_diff"]}
    assert diff["USDT"] == "-20"
    assert float(stored["realized_diff_ratio"]) == pytest.approx(2 / 3)


def test_worker_restart_cursor_survives_across_calls(postgres_schema):
    from server import outcome_store

    assert outcome_store.get_cursor() is None
    outcome_store.set_cursor("2026-11-10T00:00:00+00:00|futures:BTC/USDT:USDT:c1")
    assert outcome_store.get_cursor() == "2026-11-10T00:00:00+00:00|futures:BTC/USDT:USDT:c1"
    outcome_store.set_cursor("2026-11-10T01:00:00+00:00|futures:BTC/USDT:USDT:c2")
    assert outcome_store.get_cursor() == "2026-11-10T01:00:00+00:00|futures:BTC/USDT:USDT:c2"


def test_pg_persistence_restart_cycle_reingest_is_stable(postgres_schema):
    """A fresh store process (new connection) re-reading the same window
    changes nothing — persistence and idempotency across restarts."""
    from server import outcome_store

    _record()
    before = outcome_store.summary()["totals"]
    _record()
    _record()
    after = outcome_store.summary()["totals"]
    assert before == after
    with psycopg.connect(postgres_schema) as conn:
        rows = conn.execute("SELECT count(*) FROM outcome_fills").fetchone()[0]
    assert rows == 1
