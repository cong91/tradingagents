"""PostgreSQL ownership and fencing for outcome-ledger polls."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from server import outcome_ledger, outcome_poll_lock, outcome_store
from tests.test_outcome_ledger_worker import (
    ORDER_ID,
    _enable_and_seed,
    _fill_source,
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


@pytest.fixture
def prepared_cycle(postgres_schema, ingest_enabled):
    _enable_and_seed()
    _record_executed_cycle("poll-owner-cycle")
    _record_journal(placed_ids=[ORDER_ID])
    return postgres_schema


def _blocked_source(entered, release, owner, *, fail=False):
    def source(account_id, symbol, since_ms, until_ms):
        guard = outcome_poll_lock.current_guard()
        owner.append(guard.connection.info.backend_pid)
        entered.set()
        if not release.wait(15):
            raise TimeoutError("test did not release the fake venue read")
        if fail:
            raise RuntimeError("fake venue read failed")
        funding = [{
            "id": "income-1", "timestamp": datetime.now(UTC).isoformat(),
            "amount": "-0.1", "currency": "USDT", "venue_semantics": "signed income",
        }]
        return _fill_source([_venue_fill("trade-1", ORDER_ID)], funding=funding)(
            account_id, symbol, since_ms, until_ms,
        )
    return source


def _state_snapshot(dsn):
    with psycopg.connect(dsn) as conn:
        state = conn.execute(
            "SELECT state_key, cursor_value, updated_at FROM outcome_ledger_state ORDER BY state_key"
        ).fetchall()
        counts = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
            "outcome_positions", "outcome_fills", "outcome_exits", "outcome_funding",
            "outcome_funding_observations", "outcome_coverage",
        ))
    return state, counts


def test_journal_mutation_during_venue_read_cannot_create_closure_proof(
    prepared_cycle,
):
    entered, release = threading.Event(), threading.Event()
    owner = []

    def source(*args):
        window = _blocked_source(entered, release, owner)(*args)
        window["fills_complete"] = True
        return window

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(outcome_ledger.poll_once, source)
        try:
            assert entered.wait(10)
            with psycopg.connect(prepared_cycle) as conn:
                updated = conn.execute(
                    """UPDATE bracket_recovery_tasks SET quantity = %s
                       WHERE symbol = %s AND status = 'completed'""",
                    (2, "BTC/USDT:USDT"),
                ).rowcount
                assert updated == 1
            release.set()
            assert first.result(timeout=15)["status"] == "polled"
        finally:
            release.set()

    with psycopg.connect(prepared_cycle) as conn:
        assert conn.execute("SELECT count(*) FROM outcome_positions").fetchone()[0] == 1
        provenance = conn.execute(
            "SELECT provenance FROM outcome_positions"
        ).fetchone()[0]
    assert provenance.get("closure_proof_version") is None
    assert provenance["closure_verdict"] == "unverified"


def test_contending_poll_is_busy_and_owner_is_idle_during_venue_read(prepared_cycle):
    with psycopg.connect(prepared_cycle) as conn:
        conn.execute("CREATE TABLE poll_write_backends (table_name text, backend_pid integer)")
        conn.execute("""CREATE FUNCTION log_poll_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                INSERT INTO poll_write_backends VALUES (TG_TABLE_NAME, pg_backend_pid());
                RETURN NEW;
            END $$""")
        for table in ("outcome_ledger_state", "outcome_positions", "outcome_fills",
                      "outcome_funding_observations", "outcome_coverage"):
            conn.execute(psycopg.sql.SQL(
                "CREATE TRIGGER log_poll_write AFTER INSERT OR UPDATE ON {} "
                "FOR EACH ROW EXECUTE FUNCTION log_poll_write()"
            ).format(psycopg.sql.Identifier(table)))
    entered, release = threading.Event(), threading.Event()
    owner = []
    calls = []

    def unexpected_source(*args):
        calls.append(args)
        raise AssertionError("busy poll must not read the venue")

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(outcome_ledger.poll_once, _blocked_source(entered, release, owner))
        try:
            assert entered.wait(10)
            before = _state_snapshot(prepared_cycle)
            assert outcome_ledger.poll_once(unexpected_source) == {"status": "busy"}
            with psycopg.connect(prepared_cycle) as conn:
                state, transaction_start = conn.execute(
                    "SELECT state, xact_start FROM pg_stat_activity WHERE pid = %s", (owner[0],),
                ).fetchone()
            assert state == "idle"
            assert transaction_start is None
            assert calls == []
            assert _state_snapshot(prepared_cycle) == before
        finally:
            release.set()
        assert first.result(timeout=15)["status"] == "polled"

    state, counts = _state_snapshot(prepared_cycle)
    assert counts == (1, 1, 0, 0, 1, 1)
    assert any(key == "outcome_ledger" and "poll-owner-cycle" in value for key, value, _ in state)
    with psycopg.connect(prepared_cycle) as conn:
        writes = conn.execute("SELECT table_name, backend_pid FROM poll_write_backends").fetchall()
    assert {table for table, _ in writes} == {
        "outcome_ledger_state", "outcome_positions", "outcome_fills",
        "outcome_funding_observations", "outcome_coverage",
    }
    assert {pid for _, pid in writes} == {owner[0]}


def test_poll_initialization_uses_owner_backend(postgres_schema, ingest_enabled, monkeypatch):
    seen = []
    original = outcome_poll_lock.postgres.connect

    def connect():
        conn = original()
        seen.append(conn.info.backend_pid)
        return conn

    monkeypatch.setattr(outcome_poll_lock.postgres, "connect", connect)
    assert outcome_ledger.poll_once(_fill_source([]))["status"] == "initialized"
    assert len(seen) == 1
    assert outcome_store.get_cursor() is not None


@pytest.mark.parametrize("fail", [False, True])
def test_killed_owner_cannot_write_after_blocked_venue_read(prepared_cycle, fail):
    entered, release = threading.Event(), threading.Event()
    owner = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            outcome_ledger.poll_once, _blocked_source(entered, release, owner, fail=fail),
        )
        try:
            assert entered.wait(10)
            before = _state_snapshot(prepared_cycle)
            with psycopg.connect(prepared_cycle, autocommit=True) as conn:
                assert conn.execute("SELECT pg_terminate_backend(%s)", (owner[0],)).fetchone()[0]
            assert outcome_ledger.poll_once(_fill_source([]))["status"] == "polled"
            after_successor = _state_snapshot(prepared_cycle)
        finally:
            release.set()
        with pytest.raises(outcome_poll_lock.PollLockLostError):
            first.result(timeout=15)

    assert _state_snapshot(prepared_cycle) == after_successor
    assert after_successor != before
    assert not any(key.startswith("failed:") for key, _, _ in after_successor[0])


def test_killed_revisit_owner_never_rotates_or_writes_after_loss(prepared_cycle):
    assert outcome_ledger.poll_once(_fill_source([_venue_fill("initial", ORDER_ID)]))["status"] == "polled"
    entered, release = threading.Event(), threading.Event()
    owner = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            outcome_ledger.poll_once, _blocked_source(entered, release, owner, fail=True),
        )
        try:
            assert entered.wait(10)
            with psycopg.connect(prepared_cycle, autocommit=True) as conn:
                assert conn.execute("SELECT pg_terminate_backend(%s)", (owner[0],)).fetchone()[0]
            assert outcome_ledger.poll_once(_fill_source([]))["status"] == "polled"
            after_successor = _state_snapshot(prepared_cycle)
        finally:
            release.set()
        with pytest.raises(outcome_poll_lock.PollLockLostError):
            first.result(timeout=15)
    assert _state_snapshot(prepared_cycle) == after_successor


def test_unlock_during_poll_aborts_before_next_write(prepared_cycle):
    def source(account_id, symbol, since_ms, until_ms):
        guard = outcome_poll_lock.current_guard()
        assert guard.connection.execute("SELECT pg_advisory_unlock_all()").fetchone() is not None
        return _fill_source([_venue_fill("lost-lock", ORDER_ID)])(
            account_id, symbol, since_ms, until_ms,
        )

    with pytest.raises(outcome_poll_lock.PollLockLostError):
        outcome_ledger.poll_once(source)
    state, counts = _state_snapshot(prepared_cycle)
    assert counts == (0, 0, 0, 0, 0, 0)
    assert not any(key.startswith("failed:") for key, _, _ in state)
    assert not any(key == "outcome_ledger" and "poll-owner-cycle" in value for key, value, _ in state)


def test_same_table_with_different_search_path_contends(postgres_schema, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def hold():
        with outcome_poll_lock.poll_lock():
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(hold)
        try:
            assert entered.wait(10)
            alternate = psycopg.connect(postgres_schema, autocommit=True, row_factory=dict_row)
            schema = alternate.execute("SELECT current_schema() AS name").fetchone()["name"]
            alternate.execute(
                psycopg.sql.SQL("SET search_path TO pg_catalog, {} ").format(
                    psycopg.sql.Identifier(schema),
                ),
            )
            monkeypatch.setattr(outcome_poll_lock.postgres, "connect", lambda: alternate)
            with pytest.raises(outcome_poll_lock.PollLockBusyError), outcome_poll_lock.poll_lock():
                pytest.fail("the same ledger table must contend")
        finally:
            release.set()
        first.result(timeout=15)


def test_renamed_schema_keeps_same_ledger_lock(postgres_schema, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_schema = None
    renamed_schema = None

    def hold():
        with outcome_poll_lock.poll_lock():
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(hold)
        try:
            assert entered.wait(10)
            with psycopg.connect(postgres_schema, autocommit=True) as admin:
                original_schema = admin.execute("SELECT current_schema()").fetchone()[0]
                renamed_schema = f"{original_schema}_moved"
                admin.execute(psycopg.sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                    psycopg.sql.Identifier(original_schema),
                    psycopg.sql.Identifier(renamed_schema),
                ))
            alternate = psycopg.connect(postgres_schema, autocommit=True, row_factory=dict_row)
            alternate.execute(
                psycopg.sql.SQL("SET search_path TO pg_catalog, {}").format(
                    psycopg.sql.Identifier(renamed_schema),
                ),
            )
            monkeypatch.setattr(outcome_poll_lock.postgres, "connect", lambda: alternate)
            with pytest.raises(outcome_poll_lock.PollLockBusyError), outcome_poll_lock.poll_lock():
                pytest.fail("a schema rename must not change relation lock identity")
        finally:
            release.set()
            if original_schema is not None and renamed_schema is not None:
                with psycopg.connect(postgres_schema, autocommit=True) as admin:
                    admin.execute(psycopg.sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                        psycopg.sql.Identifier(renamed_schema),
                        psycopg.sql.Identifier(original_schema),
                    ))
        first.result(timeout=15)
def test_different_schema_can_own_its_poll(postgres_schema, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def hold():
        with outcome_poll_lock.poll_lock():
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(hold)
        try:
            assert entered.wait(10)
            alternate = psycopg.connect(postgres_schema, autocommit=True, row_factory=dict_row)
            alternate.execute("CREATE TEMP TABLE outcome_ledger_state (state_key text)")
            monkeypatch.setattr(outcome_poll_lock.postgres, "connect", lambda: alternate)
            with outcome_poll_lock.poll_lock() as guard:
                guard.check()
        finally:
            release.set()
        first.result(timeout=15)


def test_poll_initialization_obeys_owner_lock(postgres_schema, ingest_enabled):
    entered, release = threading.Event(), threading.Event()

    def hold():
        with outcome_poll_lock.poll_lock():
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(hold)
        try:
            assert entered.wait(10)
            assert outcome_ledger.poll_once(_fill_source([])) == {"status": "busy"}
            assert outcome_store.get_cursor() is None
        finally:
            release.set()
        first.result(timeout=15)
    assert outcome_ledger.poll_once(_fill_source([]))["status"] == "initialized"


def test_revisit_failures_are_bounded_and_leave_a_permanent_marker(prepared_cycle):
    cycle_id = "poll-owner-cycle"
    assert outcome_ledger.poll_once(_fill_source([_venue_fill("first", ORDER_ID)]))["status"] == "polled"

    def fail_source(*args):
        raise RuntimeError("simulated venue outage")

    for _attempt in range(outcome_ledger.MAX_CYCLE_ATTEMPTS):
        result = outcome_ledger.poll_once(fail_source)
        assert result["status"] == "polled"
        assert result["reconciled"][0]["result"] == "error:RuntimeError"

    state, _ = _state_snapshot(prepared_cycle)
    assert (f"reconcile:{cycle_id}", "permanent") in {(key, value) for key, value, _ in state}
    assert (f"failed:{cycle_id}", "permanent") in {(key, value) for key, value, _ in state}
    before = _state_snapshot(prepared_cycle)
    result = outcome_ledger.poll_once(fail_source)
    assert result["reconciled"] == []
    assert _state_snapshot(prepared_cycle) == before


def test_busy_poll_does_not_change_reconciliation_rotation(prepared_cycle):
    cycle = "poll-owner-cycle"
    outcome_store.schedule_reconciliation(cycle)
    earlier = datetime.now(UTC) - timedelta(days=1)
    with psycopg.connect(prepared_cycle) as conn:
        conn.execute(
            "UPDATE outcome_ledger_state SET updated_at = %s WHERE state_key = %s",
            (earlier, f"reconcile:{cycle}"),
        )
    entered, release = threading.Event(), threading.Event()

    def hold():
        with outcome_poll_lock.poll_lock():
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(hold)
        try:
            assert entered.wait(10)
            before = _state_snapshot(prepared_cycle)
            assert outcome_ledger.poll_once(_fill_source([]), batch_size=1) == {"status": "busy"}
            assert _state_snapshot(prepared_cycle) == before
        finally:
            release.set()
        first.result(timeout=15)
