"""Durable account-identity columns on bracket tasks and position states."""

from datetime import UTC, datetime

import pytest

from server import db, job_store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema):
    return postgres_schema


def _identity(hash_suffix: str = "a" * 64):
    from tests.execution_derivatives_support import identity_for_api_key

    return identity_for_api_key(hash_suffix)


def _task_kwargs(**extra):
    return {
        "symbol": "BTC/USDT:USDT", "entry_order_id": "entry-1", "close_side": "sell",
        "quantity": 0.005, "legs": [], "now": datetime.now(UTC).isoformat(), **extra,
    }


def _stored_identity_row(row):
    if row is None:
        return None
    return (
        row["account_identity_exchange"], row["account_identity_profile"],
        row["account_identity_type"], row["account_identity_hash"],
    )


def _stored_task_identity(task_id):
    with db.read() as conn:
        row = conn.execute(
            "SELECT account_identity_exchange, account_identity_profile, "
            "account_identity_type, account_identity_hash "
            "FROM bracket_recovery_tasks WHERE task_id = %s",
            (task_id,),
        ).fetchone()
    return _stored_identity_row(row)


def _stored_state_identity(position_key):
    with db.read() as conn:
        row = conn.execute(
            "SELECT account_identity_exchange, account_identity_profile, "
            "account_identity_type, account_identity_hash "
            "FROM position_states WHERE position_key = %s",
            (position_key,),
        ).fetchone()
    return _stored_identity_row(row)


def test_create_bracket_recovery_persists_identity_columns():
    identity = _identity()

    assert job_store.create_bracket_recovery("task-1", identity=identity, **_task_kwargs())

    assert _stored_task_identity("task-1") == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_create_bracket_recovery_accepts_a_four_tuple():
    assert job_store.create_bracket_recovery(
        "task-tuple", identity=("binance", "demo", "usds_futures", "b" * 64),
        **_task_kwargs(),
    )
    assert _stored_task_identity("task-tuple") == (
        "binance", "demo", "usds_futures", "b" * 64,
    )


def test_create_bracket_recovery_without_identity_leaves_columns_null():
    assert job_store.create_bracket_recovery("task-legacy", **_task_kwargs())
    assert _stored_task_identity("task-legacy") == (None, None, None, None)


def test_create_bracket_recovery_rejects_a_malformed_hash():
    with pytest.raises(ValueError, match="identity hash"):
        job_store.create_bracket_recovery(
            "task-bad", identity=("binance", "demo", "usds_futures", "NOTHASH"),
            **_task_kwargs(),
        )
    assert job_store.load_bracket_recovery("task-bad") is None


def test_update_bracket_recovery_never_touches_identity_columns():
    identity = _identity()
    job_store.create_bracket_recovery("task-2", identity=identity, **_task_kwargs())

    job_store.update_bracket_recovery(
        "task-2", status="monitoring", placed_ids=["1"], attempts=1,
        last_error=None, now=datetime.now(UTC).isoformat(), legs=[],
    )

    assert _stored_task_identity("task-2") == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_persist_futures_entry_state_persists_identity_on_the_position_row():
    identity = _identity()
    position_key = "BTC/USDT:USDT:buy:entry-1"

    assert job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-1", identity=identity,
    )

    assert _stored_state_identity(position_key) == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )
    state = job_store.load_position_state(position_key)
    assert state["account_identity_hash"] == identity.identity_hash
    assert state["account_identity_exchange"] == "binance"
    assert state["account_identity_profile"] == "demo"
    assert state["account_identity_type"] == "usds_futures"


def test_save_position_state_without_identity_never_overwrites_stored_columns():
    identity = _identity()
    position_key = "BTC/USDT:USDT:buy:entry-2"
    job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-2", identity=identity,
    )
    state = job_store.load_position_state(position_key)
    state["snapshot"]["stop"] = 95.0

    assert job_store.save_position_state(position_key, state["revision"], state)

    assert _stored_state_identity(position_key) == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_save_position_state_round_trips_the_carried_identity():
    identity = _identity()
    position_key = "BTC/USDT:USDT:buy:entry-3"
    job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-3", identity=identity,
    )
    state = job_store.load_position_state(position_key)
    state["snapshot"]["stop"] = 95.0

    assert job_store.save_position_state(position_key, state["revision"], state)

    assert _stored_state_identity(position_key) == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )
    # The identity lives in the columns, never duplicated inside the jsonb state.
    with db.read() as conn:
        row = conn.execute(
            "SELECT state FROM position_states WHERE position_key = %s", (position_key,),
        ).fetchone()
    assert "account_identity_hash" not in row["state"]


def test_save_position_state_without_identity_starts_null():
    position_key = "BTC/USDT:USDT:buy:entry-4"
    job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-4",
    )
    assert _stored_state_identity(position_key) == (None, None, None, None)
    assert "account_identity_hash" not in job_store.load_position_state(position_key)


def test_active_position_states_carry_the_identity_columns():
    identity = _identity()
    position_key = "BTC/USDT:USDT:buy:entry-5"
    job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="entry-5", identity=identity,
    )

    active = job_store.active_position_states()

    assert active[0]["account_identity_hash"] == identity.identity_hash
    assert active[0]["account_identity_profile"] == "demo"


# --- late binding of legacy (previously unbound) bracket rows ------------------


def test_bind_bracket_recovery_identity_binds_a_previously_null_row():
    identity = _identity()
    job_store.create_bracket_recovery("task-bind", **_task_kwargs())

    assert job_store.bind_bracket_recovery_identity("task-bind", identity=identity) == "bound"

    assert _stored_task_identity("task-bind") == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_bind_bracket_recovery_identity_reports_unknown_task():
    assert job_store.bind_bracket_recovery_identity(
        "task-missing", identity=_identity(),
    ) == "not_found"


def test_bind_bracket_recovery_identity_is_idempotent_for_the_same_identity():
    identity = _identity()
    job_store.create_bracket_recovery("task-same", identity=identity, **_task_kwargs())

    assert job_store.bind_bracket_recovery_identity(
        "task-same", identity=identity,
    ) == "already_bound_same"
    assert _stored_task_identity("task-same") == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_bind_bracket_recovery_identity_never_overwrites_a_different_identity():
    from tests.execution_derivatives_support import identity_for_api_key

    identity = _identity()
    other = identity_for_api_key("different-account")
    job_store.create_bracket_recovery("task-other", identity=identity, **_task_kwargs())

    assert job_store.bind_bracket_recovery_identity(
        "task-other", identity=other,
    ) == "already_bound_other"
    assert _stored_task_identity("task-other") == (
        identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
    )


def test_bind_bracket_recovery_identity_rejects_a_malformed_identity():
    job_store.create_bracket_recovery("task-malformed", **_task_kwargs())

    with pytest.raises(ValueError, match="identity"):
        job_store.bind_bracket_recovery_identity(
            "task-malformed",
            identity=("binance", "demo", "usds_futures", "NOT-A-HASH"),
        )

    assert _stored_task_identity("task-malformed") == (None, None, None, None)
