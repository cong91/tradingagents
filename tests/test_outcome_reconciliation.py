"""Regression tests for persistent, evidence-based outcome reconciliation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from server import (
    futures_cycle_store,
    futures_trade_history,
    job_store,
    outcome_ledger,
    outcome_report,
    outcome_store,
)
from server.outcome_venue_reads import read_venue_window
from tests.test_outcome_ledger_worker import (
    ACCOUNT_HASH,
    ACCOUNT_ID,
    CLIENT_ORDER_ID,
    ORDER_ID,
    SYMBOL,
    _enable_and_seed,
    _fill_source,
    _marker,
    _record_bracket_task,
    _record_executed_cycle,
    _record_journal,
    _venue_fill,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


@pytest.fixture
def ingest_enabled():
    set_config({"outcome_ledger_ingest_enabled": True})
    yield
    set_config({"outcome_ledger_ingest_enabled": False})


def _fill(trade_id, order_id, quantity, timestamp, *, side="buy"):
    return {
        **_venue_fill(trade_id, order_id, side=side),
        "contracts": quantity,
        "timestamp": timestamp.isoformat(),
    }


def _setup_cycle(postgres_schema, cycle_id="cycle-lifecycle"):
    _enable_and_seed()
    _record_executed_cycle(cycle_id)
    _record_journal(placed_ids=[ORDER_ID])
    _record_bracket_task(ORDER_ID, placed_ids=["9500"])
    start = datetime.now(UTC) - timedelta(minutes=30)
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(
            "UPDATE futures_cycles SET created_at = %s WHERE cycle_id = %s",
            (start, cycle_id),
        )
    return start


def _complete_fill_source(fills, *, status="ok", funding=None):
    def source(account_id, symbol, since_ms, until_ms):
        return {
            "status": status,
            "fills": fills,
            "fills_complete": True,
            "warnings": ["funding_history_unverified"],
            "funding": funding,
            "start_at": datetime.fromtimestamp(since_ms / 1000, UTC).isoformat(),
            "end_at": datetime.fromtimestamp((until_ms + 1) / 1000, UTC).isoformat(),
        }
    return source


def _position(postgres_schema):
    with psycopg.connect(postgres_schema) as conn:
        return conn.execute(
            "SELECT filled_quantity, opened_at, closed_at FROM outcome_positions"
        ).fetchone()


def test_partial_exit_is_revisited_after_cursor_and_late_fills_are_idempotent(
    postgres_schema, ingest_enabled,
):
    start = _setup_cycle(postgres_schema)
    entry = _fill("1", ORDER_ID, "1.0", start + timedelta(minutes=1))
    first_exit = _fill("2", "9500", "0.4", start + timedelta(minutes=2), side="sell")
    final_exit = _fill("3", "9500", "0.6", start + timedelta(minutes=4), side="sell")
    outcome_ledger.poll_once(_fill_source([entry, first_exit]))
    quantity, opened_at, closed_at = _position(postgres_schema)
    assert quantity == Decimal("1.0")
    assert opened_at == start + timedelta(minutes=1)
    assert closed_at is None

    # A new call represents a fresh worker after its discovery cursor advanced.
    for _ in range(2):
        outcome_ledger.poll_once(_complete_fill_source([entry, first_exit, final_exit]))
    assert _position(postgres_schema)[2] == start + timedelta(minutes=4)
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_fills").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM outcome_exits").fetchone()[0] == 2
        assert conn.execute(
            "SELECT provenance->>'closure_proof_version' FROM outcome_positions"
        ).fetchone()[0] == "2"


@pytest.mark.parametrize("read_kind", ["empty", "error", "partial"])
def test_revisit_revokes_and_reverifies_closure_without_losing_recorded_time(
    postgres_schema, ingest_enabled, read_kind,
):
    start = _setup_cycle(postgres_schema, f"cycle-revoke-{read_kind}")
    entry = _fill("revoke-entry", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("revoke-exit", "9500", "1", start + timedelta(minutes=2), side="sell")
    closed_at = start + timedelta(minutes=2)
    outcome_ledger.poll_once(_complete_fill_source([entry, exit_fill]))
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute(
            "SELECT closed_at, provenance->>'closure_proof_version' FROM outcome_positions"
        ).fetchone() == (closed_at, "2")

    if read_kind == "empty":
        source = _fill_source([], status="unavailable")
    elif read_kind == "partial":
        source = _fill_source([entry, exit_fill], status="partial")
    else:
        def source(*args):
            raise RuntimeError("venue unavailable")

    for _ in range(2):
        outcome_ledger.poll_once(source)
        with psycopg.connect(postgres_schema) as conn:
            row = conn.execute(
                """SELECT closed_at, provenance->>'recorded_closed_at',
                          provenance->>'closure_proof_version',
                          provenance->>'closure_verdict',
                          (SELECT count(*) FROM outcome_fills),
                          (SELECT count(*) FROM outcome_exits)
                   FROM outcome_positions""",
            ).fetchone()
        assert row[0] is None
        assert datetime.fromisoformat(row[1]) == closed_at
        assert row[2:] == (None, "unverified", 2, 1)
        positions = outcome_report.summary()["positions"]
        assert positions["closed"] == positions["recorded_closed"] == 1
        assert positions["verified_closed"] == 0
        assert positions["last_closed"] is None
        assert positions["last_recorded_closed"] == closed_at
        assert positions["closure_evidence"]["unverified_closed_timestamps"] == 1

    outcome_ledger.poll_once(_complete_fill_source([entry, exit_fill]))
    with psycopg.connect(postgres_schema) as conn:
        row = conn.execute(
            """SELECT closed_at, provenance->>'recorded_closed_at',
                      provenance->>'closure_proof_version', provenance->>'closure_verdict',
                      (SELECT count(*) FROM outcome_fills),
                      (SELECT count(*) FROM outcome_exits)
               FROM outcome_positions"""
        ).fetchone()
    assert row == (closed_at, None, "2", "verified", 2, 1)
    positions = outcome_report.summary()["positions"]
    assert positions["closed"] == positions["recorded_closed"] == 1
    assert positions["verified_closed"] == 1
    assert positions["last_closed"] == positions["last_recorded_closed"] == closed_at
    assert positions["closure_evidence"]["unverified_closed_timestamps"] == 0


@pytest.mark.parametrize("source_status", ["partial", "error", "unavailable"])
def test_unverified_fill_history_does_not_close_position(
    postgres_schema, ingest_enabled, source_status,
):
    start = _setup_cycle(postgres_schema)
    entry = _fill("10", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("11", "9500", "1", start + timedelta(minutes=2), side="sell")
    outcome_ledger.poll_once(_fill_source([entry, exit_fill], status=source_status))
    assert _position(postgres_schema)[2] is None


def test_funding_warning_does_not_change_certified_fill_completeness(
    postgres_schema, ingest_enabled,
):
    start = _setup_cycle(postgres_schema)
    entry = _fill("10a", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("11a", "9500", "1", start + timedelta(minutes=2), side="sell")
    outcome_ledger.poll_once(_complete_fill_source([entry, exit_fill]))
    assert _position(postgres_schema)[2] == start + timedelta(minutes=2)


def test_incomplete_fill_evidence_never_closes_a_position(postgres_schema, ingest_enabled):
    start = _setup_cycle(postgres_schema)
    entry = _fill("10", ORDER_ID, "1", start + timedelta(minutes=1))
    outcome_ledger.poll_once(_fill_source([entry], status="partial"))
    assert _position(postgres_schema)[2] is None
    partial_exit = _fill("11", "9500", "0.4", start + timedelta(minutes=2), side="sell")
    outcome_ledger.poll_once(_fill_source([entry, partial_exit], status="partial"))
    assert _position(postgres_schema)[2] is None


@pytest.mark.parametrize("extra_fill", ["duplicate", "wrong_symbol", "malformed"])
def test_foreign_or_malformed_history_rows_prevent_closure(
    postgres_schema, ingest_enabled, extra_fill,
):
    """Malformed, duplicate and unattributed rows invalidate history proof."""
    start = _setup_cycle(postgres_schema)
    entry = _fill("16", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("17", "9500", "1", start + timedelta(minutes=2), side="sell")
    extra = {
        "duplicate": dict(entry),
        "wrong_symbol": {**entry, "symbol": "ETH/USDT:USDT"},
        "malformed": "not-a-fill",
    }[extra_fill]
    outcome_ledger.poll_once(_complete_fill_source([entry, exit_fill, extra]))
    assert _position(postgres_schema)[2] is None


def test_late_partial_entry_and_missing_evidence_never_close_early(
    postgres_schema, ingest_enabled,
):
    start = _setup_cycle(postgres_schema)
    first_entry = _fill("20", ORDER_ID, "0.5", start + timedelta(minutes=1))
    first_exit = _fill("21", "9500", "0.5", start + timedelta(minutes=2), side="sell")
    late_entry = _fill("22", ORDER_ID, "0.5", start + timedelta(minutes=3))
    final_exit = _fill("23", "9500", "0.5", start + timedelta(minutes=4), side="sell")
    outcome_ledger.poll_once(_complete_fill_source([first_entry, first_exit]))
    assert _position(postgres_schema)[2] is None
    outcome_ledger.poll_once(_complete_fill_source([first_entry, first_exit, late_entry]))
    assert _position(postgres_schema)[0] == Decimal("1")
    assert _position(postgres_schema)[2] is None
    outcome_ledger.poll_once(_complete_fill_source([first_entry, first_exit, late_entry, final_exit]))
    assert _position(postgres_schema)[2] == start + timedelta(minutes=4)


def test_delayed_unlinked_funding_is_observed_without_guessing_position(
    postgres_schema, ingest_enabled,
):
    start = _setup_cycle(postgres_schema)
    entry = _fill("30", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("31", "9500", "1", start + timedelta(minutes=10), side="sell")
    outcome_ledger.poll_once(_fill_source([entry, exit_fill]))
    funding = [{
        "id": "funding-1", "timestamp": (start + timedelta(minutes=5)).isoformat(),
        "amount": "-0.003", "currency": "USDT",
        "venue_semantics": "binance: income positive = received",
    }]
    for _ in range(2):
        outcome_ledger.poll_once(_fill_source([entry, exit_fill], funding=funding))
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_funding").fetchone()[0] == 0
        assert conn.execute(
            "SELECT amount, currency FROM outcome_funding_observations"
        ).fetchall() == [(Decimal("-0.003"), "USDT")]


def test_journal_task_id_must_equal_marker_intent_id(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("wrong-task")
    _record_journal(placed_ids=[ORDER_ID], task_id="entry-intent:wrong")
    result = outcome_ledger.poll_once(_fill_source([_venue_fill("38", ORDER_ID)]))
    assert result["outcomes"][0]["result"] == "unattributed_ambiguous_journal"
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 0


def test_legacy_cycle_without_marker_is_not_attributed(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("legacy-cycle")
    _record_journal(placed_ids=[ORDER_ID], bind_cycle=False)
    result = outcome_ledger.poll_once(_fill_source([_venue_fill("39", ORDER_ID)]))
    assert result["outcomes"][0]["result"] == "unattributed_no_marker"
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 0


def test_unrelated_journal_does_not_override_exact_cycle_marker(postgres_schema, ingest_enabled):
    _setup_cycle(postgres_schema)
    _record_journal(placed_ids=[ORDER_ID], client_order_id="ta_other", bind_cycle=False)
    result = outcome_ledger.poll_once(_fill_source([_venue_fill("40", ORDER_ID)]))
    assert result["outcomes"][0]["result"] == "recorded"
    with psycopg.connect(postgres_schema) as conn:
        rows = conn.execute("SELECT client_order_id FROM outcome_positions").fetchall()
    assert rows == [(CLIENT_ORDER_ID,)]


def test_funding_observation_key_covers_the_full_scope():
    sql = (Path(__file__).parents[1] / "server" / "migrations" /
           "0012_outcome_event_identity.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (account_id, venue_key, account_type, symbol, venue_income_id)" in sql


def test_funding_observations_are_scoped_by_full_identity(postgres_schema):
    event = {
        "id": "same-income", "funding_at": "2026-11-10T02:00:00+00:00",
        "amount": Decimal("-0.125"), "currency": "USDT",
        "venue_semantics": "binance: income positive = received",
    }
    scopes = [
        (ACCOUNT_ID, "venue-a", "futures", SYMBOL),
        (ACCOUNT_ID, "venue-b", "futures", SYMBOL),
        (ACCOUNT_ID, "venue-a", "spot", SYMBOL),
        (f"binance:demo:{'c' * 64}", "venue-a", "futures", SYMBOL),
        (ACCOUNT_ID, "venue-a", "futures", "ETH/USDT:USDT"),
    ]
    for scope in scopes:
        outcome_store.record_funding_observations(*scope, [event])
        outcome_store.record_funding_observations(*scope, [event])
    with psycopg.connect(postgres_schema) as conn:
        rows = conn.execute(
            "SELECT account_id, venue_key, account_type, symbol "
            "FROM outcome_funding_observations"
        ).fetchall()
    assert set(rows) == set(scopes)
    with pytest.raises(ValueError, match="conflicting venue funding identity"):
        outcome_store.record_funding_observations(
            *scopes[0], [{**event, "amount": Decimal("1")}],
        )


@pytest.mark.parametrize("missing", ["account_id", "venue_key", "account_type", "symbol"])
def test_funding_observations_refuse_incomplete_scope_without_database(monkeypatch, missing):
    scope = {"account_id": ACCOUNT_ID, "venue_key": "venue", "account_type": "futures",
             "symbol": SYMBOL}
    scope[missing] = ""
    monkeypatch.setattr(outcome_store.db, "session", lambda: pytest.fail("unexpected database access"))
    with pytest.raises(ValueError, match="complete account and venue identity"):
        outcome_store.record_funding_observations(**scope, events=[])


def test_funding_observations_refuse_missing_event_id_without_database(monkeypatch):
    monkeypatch.setattr(outcome_store.db, "session", lambda: pytest.fail("unexpected database access"))
    with pytest.raises(ValueError, match="venue income ID"):
        outcome_store.record_funding_observations(
            ACCOUNT_ID, "venue", "futures", SYMBOL, [{"id": ""}],
        )


def test_cross_symbol_trade_ids_are_distinct_in_store(postgres_schema):
    position = {
        "client_order_id": CLIENT_ORDER_ID, "venue_order_id": ORDER_ID,
        "symbol": SYMBOL, "market_type": "perpetual", "side": "long",
        "filled_quantity": Decimal("1"), "attribution": "managed",
    }
    fill = {
        "venue_trade_id": "42", "symbol": SYMBOL, "side": "buy", "role": "entry",
        "quantity": Decimal("1"), "price": Decimal("100"),
        "occurred_at": "2026-11-10T00:00:00+00:00",
    }
    first = outcome_store.record_outcome(account_id=ACCOUNT_ID, position=position,
                                         fills=[fill], exits=[], funding=[])
    second = outcome_store.record_outcome(
        account_id=ACCOUNT_ID,
        position={**position, "client_order_id": "ta_eth", "symbol": "ETH/USDT:USDT"},
        fills=[{**fill, "symbol": "ETH/USDT:USDT"}], exits=[], funding=[],
    )
    assert first["fills_new"] == second["fills_new"] == 1


@pytest.mark.parametrize("field,value", [
    ("venue_order_id", "changed"), ("market_type", "spot"),
    ("planned_quantity", Decimal("2")), ("side", "short"), ("cycle_id", "other-cycle"),
])
def test_existing_position_identity_conflicts_fail_atomically(postgres_schema, field, value):
    position = {
        "client_order_id": CLIENT_ORDER_ID, "venue_order_id": ORDER_ID,
        "symbol": SYMBOL, "market_type": "perpetual", "side": "long",
        "planned_quantity": Decimal("1"), "filled_quantity": Decimal("1"),
        "attribution": "managed", "cycle_id": "cycle",
    }
    outcome_store.record_outcome(account_id=ACCOUNT_ID, position=position, fills=[], exits=[], funding=[])
    with pytest.raises(ValueError, match="conflicting position identity"):
        outcome_store.record_outcome(account_id=ACCOUNT_ID, position={**position, field: value},
                                     fills=[], exits=[], funding=[])
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT venue_order_id, planned_quantity FROM outcome_positions").fetchone() == (
            ORDER_ID, Decimal("1"),
        )


def test_missing_history_bounds_are_reported_but_do_not_close(
    postgres_schema, ingest_enabled,
):
    start = _setup_cycle(postgres_schema)
    entry = _fill("60", ORDER_ID, "1", start + timedelta(minutes=1))
    exit_fill = _fill("61", "9500", "1", start + timedelta(minutes=2), side="sell")

    def source(*args):
        return {"status": "ok", "fills": [entry, exit_fill], "warnings": [], "funding": []}

    outcome_ledger.poll_once(source)
    assert _position(postgres_schema)[2] is None
    with psycopg.connect(postgres_schema) as conn:
        row = conn.execute("SELECT status, warnings FROM outcome_coverage").fetchone()
        assert row[0] == "partial"
        assert "history_window_bounds_unverified" in row[1]


def test_production_usds_futures_identity_is_attributed_end_to_end(
    postgres_schema, ingest_enabled,
):
    """The production reader reports account_type "usds_futures"; the ledger
    must attribute cycles journaled under that exact identity, not only the
    generic "futures" fixtures use."""
    _enable_and_seed()
    _record_executed_cycle("cycle-usds")
    _record_journal(placed_ids=[ORDER_ID], account_type="usds_futures")
    _record_bracket_task(ORDER_ID, placed_ids=["9500"], account_type="usds_futures")
    start = datetime.now(UTC) - timedelta(minutes=30)
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(
            "UPDATE futures_cycles SET created_at = %s WHERE cycle_id = %s",
            (start, "cycle-usds"),
        )
    result = outcome_ledger.poll_once(_complete_fill_source([
        _fill("70", ORDER_ID, "1", start + timedelta(minutes=1)),
        _fill("71", "9500", "1", start + timedelta(minutes=2), side="sell"),
    ]))
    assert result["outcomes"][0]["result"] == "recorded"
    assert _position(postgres_schema)[2] == start + timedelta(minutes=2)
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute(
            "SELECT provenance->>'closure_proof_version' FROM outcome_positions"
        ).fetchone()[0] == "2"


def test_entry_journal_update_writes_the_venue_rounded_quantity():
    """The ledger compares fills against the journal's top-level quantity, but
    the venue receives the amount_to_precision-rounded size that only lives in
    the entry leg. The guarded update must rewrite the task quantity with that
    exact value; calls without a quantity leave the stored value untouched."""
    import contextlib

    from tradingagents.execution.futures_entry_journal import update_guarded_entry_intent

    class _Cursor:
        rowcount = 1

    class _Conn:
        def __init__(self):
            self.calls = []

        def transaction(self):
            return contextlib.nullcontext()

        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            return _Cursor()

    class _Guard:
        def __init__(self):
            self.connection = _Conn()
            self.symbol = SYMBOL

        def check(self):
            return None

    guard = _Guard()
    leg = {"name": "entry-intent", "quantity": "1.2345"}
    update_guarded_entry_intent(
        guard, "entry-intent:x", status="submission_uncertain",
        leg=leg, order_id=None, error=None, quantity=1.2345,
    )
    sql, params = guard.connection.calls[0]
    assert "quantity = COALESCE(%s, quantity)" in sql
    assert params[3] == 1.2345
    update_guarded_entry_intent(
        guard, "entry-intent:x", status="pending",
        leg=leg, order_id="1", error=None,
    )
    sql, params = guard.connection.calls[1]
    assert params[3] is None


def test_ccxt_income_payload_preserves_timestamp_currency_and_raw_precision(monkeypatch):
    from tradingagents.execution import derivatives, futures_funding, futures_identity

    identity = SimpleNamespace(exchange="binance", profile="demo", account_type="futures",
                               identity_hash=ACCOUNT_HASH)
    client = object()
    bridge = SimpleNamespace(client=lambda: client, exchange_id="binance",
                             config={"exec_base_url": "https://demo-api.binance.com"})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **kwargs: SimpleNamespace(_bridge=bridge))
    monkeypatch.setattr(futures_identity, "read_futures_account_identity", lambda bridge: identity)
    monkeypatch.setattr(futures_trade_history, "_endpoint_matches", lambda client, profile: True)
    monkeypatch.setattr(futures_trade_history, "read_futures_trade_history", lambda *args: {
        "status": "partial", "fills": [], "warnings": [],
        "start_at": "2026-11-10T00:00:00Z", "end_at": "2026-11-11T00:00:00Z",
    })
    timestamp = 1794297600123
    monkeypatch.setattr(futures_funding, "fetch_funding_income", lambda *args: [{
        "id": "123456", "symbol": SYMBOL, "timestamp": timestamp, "code": "USDT", "amount": -0.1,
        "info": {"income": "-0.100000000000000001", "asset": "USDT", "tranId": 123456,
                 "incomeType": "FUNDING_FEE"},
    }])
    window = read_venue_window(ACCOUNT_ID, SYMBOL, timestamp - 1, timestamp + 1000)
    event = window["funding"][0]
    assert event["timestamp"] == timestamp
    assert event["currency"] == "USDT"
    assert Decimal(str(event["amount"])) == Decimal("-0.100000000000000001")
    assert event["id"] == "123456"


def test_venue_identity_mismatch_refuses_history_read(monkeypatch):
    from tradingagents.execution import derivatives, futures_identity

    identity = SimpleNamespace(exchange="binance", profile="demo", account_type="futures",
                               identity_hash="c" * 64)
    client = object()
    bridge = SimpleNamespace(client=lambda: client, exchange_id="binance",
                             config={"exec_base_url": "https://demo-api.binance.com"})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **kwargs: SimpleNamespace(_bridge=bridge))
    monkeypatch.setattr(futures_identity, "read_futures_account_identity", lambda bridge: identity)
    monkeypatch.setattr(futures_trade_history, "read_futures_trade_history", lambda *args: pytest.fail(
        "history must not be fetched for a different account"
    ))
    result = read_venue_window(ACCOUNT_ID, SYMBOL, 1, 2)
    assert result["status"] in {"error", "unavailable"}
    assert result["fills"] == []


def test_configured_venue_mismatch_refuses_history_and_identity_reads(monkeypatch):
    from tradingagents.execution import derivatives, futures_identity

    client = object()
    bridge = SimpleNamespace(client=lambda: client, exchange_id="binance",
                             config={"exec_base_url": "https://demo-api.binance.com"})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **kwargs: SimpleNamespace(_bridge=bridge))
    monkeypatch.setattr(futures_identity, "read_futures_account_identity", lambda bridge: pytest.fail(
        "identity must not be read on a different venue"
    ))
    result = read_venue_window(ACCOUNT_ID, SYMBOL, 1, 2, expected_venue="other")
    assert result["status"] == "unavailable"
    assert result["fills"] == []


def test_uncertain_entry_waits_for_exact_recovered_journal_order(postgres_schema, ingest_enabled):
    _enable_and_seed()
    marker = _marker()
    payload = {"execution_status": "uncertain", "entry_provenance": marker,
               "entry_intent_id": marker["intent_id"], "protection_state": "unverified"}
    now = datetime.now(UTC)
    assert futures_cycle_store.claim_futures_cycle(
        "uncertain-cycle", symbol=SYMBOL,
        four_hour_open=(now - timedelta(hours=4)).isoformat(),
        one_hour_open=(now - timedelta(hours=1)).isoformat(),
        strategy_version="1", payload=payload,
    )
    futures_cycle_store.update_futures_cycle(
        "uncertain-cycle", status="interrupted", phase="entry", payload=payload, terminal=True,
    )
    _record_journal(placed_ids=[], task_id=marker["intent_id"])
    fill = _venue_fill("55", ORDER_ID)
    first = outcome_ledger.poll_once(_fill_source([fill]))
    assert first["outcomes"][0]["result"] == "pending_order_evidence"
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 0
    job_store.update_bracket_recovery(
        marker["intent_id"], status="completed", placed_ids=[ORDER_ID], attempts=0,
        last_error=None, now=datetime.now(UTC).isoformat(),
    )
    second = outcome_ledger.poll_once(_fill_source([fill]))
    assert second["reconciled"][0]["result"] == "recorded"
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 1
    assert _position(postgres_schema)[2] is None


@pytest.mark.parametrize("field,value", [
    ("venue_key", "different-venue"),
    ("account_identity_hash", "c" * 64),
])
def test_uncertain_entry_rejects_mismatched_task_identity(
    postgres_schema, ingest_enabled, field, value,
):
    _enable_and_seed()
    marker = _marker()
    payload = {"execution_status": "uncertain", "entry_provenance": marker,
               "entry_intent_id": marker["intent_id"], "protection_state": "unverified"}
    now = datetime.now(UTC)
    futures_cycle_store.claim_futures_cycle(
        "mismatch-cycle", symbol=SYMBOL, four_hour_open=(now - timedelta(hours=4)).isoformat(),
        one_hour_open=(now - timedelta(hours=1)).isoformat(), strategy_version="1", payload=payload,
    )
    futures_cycle_store.update_futures_cycle(
        "mismatch-cycle", status="interrupted", phase="entry", payload=payload, terminal=True,
    )
    _record_journal(placed_ids=[ORDER_ID], task_id=marker["intent_id"])
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(f"UPDATE bracket_recovery_tasks SET {field} = %s", (value,))
    result = outcome_ledger.poll_once(_fill_source([_venue_fill("56", ORDER_ID)]))
    assert result["outcomes"][0]["result"] == "unattributed_no_marker"
    with psycopg.connect(postgres_schema) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 0


def test_existing_0008_ledger_rows_survive_event_identity_migration(postgres_schema):
    sql_dir = Path(__file__).parents[1] / "server" / "migrations"
    with psycopg.connect(postgres_schema) as conn:
        # Recreate only the empty ledger tables in this private test schema at 0008.
        for table in ("outcome_funding_observations", "outcome_coverage", "outcome_funding",
                      "outcome_exits", "outcome_fills", "outcome_positions", "outcome_ledger_state"):
            conn.execute(f"DROP TABLE {table} CASCADE")
        conn.execute((sql_dir / "0008_outcome_ledger.sql").read_text(encoding="utf-8"))
        conn.execute("""INSERT INTO outcome_positions
            (outcome_id, account_id, client_order_id, symbol, market_type, side, filled_quantity, attribution)
            VALUES ('00000000-0000-0000-0000-000000000001', 'a', 'c', 'BTC', 'perpetual', 'long', 1, 'managed')""")
        conn.execute("""INSERT INTO outcome_fills
            (fill_id, account_id, outcome_id, venue_trade_id, symbol, side, role, quantity, price, occurred_at)
            VALUES ('00000000-0000-0000-0000-000000000002', 'a', '00000000-0000-0000-0000-000000000001',
                    '7', 'BTC', 'sell', 'exit', 1, 100, '2026-01-01')""")
        conn.execute("""INSERT INTO outcome_exits
            (exit_id, account_id, outcome_id, leg, quantity, price, closed_at)
            VALUES ('00000000-0000-0000-0000-000000000003', 'a', '00000000-0000-0000-0000-000000000001',
                    'bracket', 1, 100, '2026-01-01')""")
        conn.execute((sql_dir / "0012_outcome_event_identity.sql").read_text(encoding="utf-8"))
        assert conn.execute("SELECT fill_id, venue_trade_id FROM outcome_fills").fetchone()[1] == "7"
        assert conn.execute("SELECT fill_id FROM outcome_exits").fetchone()[0] is None
        conn.execute("""INSERT INTO outcome_fills
            (fill_id, account_id, outcome_id, venue_trade_id, symbol, side, role, quantity, price, occurred_at)
            VALUES ('00000000-0000-0000-0000-000000000004', 'a', '00000000-0000-0000-0000-000000000001',
                    '7', 'ETH', 'buy', 'entry', 1, 100, '2026-01-01')""")
        assert conn.execute("SELECT count(*) FROM outcome_fills").fetchone()[0] == 2


def test_reconciliation_round_robin_remains_fair_when_new_cycles_arrive(postgres_schema, ingest_enabled):
    _enable_and_seed()
    for index in range(4):
        order_id = str(9100 + index)
        _record_executed_cycle(f"fair-{index}", order_id=order_id)
        _record_journal(placed_ids=[order_id], client_order_id=f"ta_fair_{index}")
    calls = []

    def source(account_id, symbol, since, until):
        calls.append((account_id, symbol))
        return {"status": "ok", "fills": [], "warnings": [], "funding": None}

    seen = set()
    for _ in range(8):
        result = outcome_ledger.poll_once(source, batch_size=1)
        assert len(result["outcomes"]) <= 1
        assert len(result["reconciled"]) <= 1
        seen.update(row["cycle_id"] for row in result["reconciled"])
    assert seen == {f"fair-{index}" for index in range(4)}
    assert len(calls) <= 16
