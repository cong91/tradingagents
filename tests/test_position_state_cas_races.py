"""Position-state compare-and-swap and monitor lease races in PostgreSQL."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from server import job_store

pytestmark = pytest.mark.unit

_POSITION_KEY = "position-state-cas-race"
_IDENTITY = ("test-exchange", "test-profile", "futures", "a" * 64)


class StaleSaveInvariantError(AssertionError):
    """A stale save succeeded or recreated a deleted position."""


@pytest.fixture(params=[None, _IDENTITY], ids=["unbound", "bound"])
def identity(request):
    return request.param


def test_initial_save_with_revision_zero_creates_revision_one(postgres_schema, identity):
    state = {"snapshot": {"stop": 90}, "orders": []}

    assert job_store.load_position_state(_POSITION_KEY) is None
    assert job_store.save_position_state(_POSITION_KEY, 0, state, identity=identity) is True

    stored = job_store.load_position_state(_POSITION_KEY)
    assert stored["revision"] == 1
    assert stored["snapshot"] == state["snapshot"]
    assert stored["orders"] == []
    if identity is not None:
        assert stored["account_identity_hash"] == identity[3]


def test_stale_save_cannot_overwrite_a_newer_revision(postgres_schema, identity):
    initial = {"snapshot": {"stop": 90}, "orders": []}
    assert job_store.save_position_state(_POSITION_KEY, 0, initial, identity=identity)
    first_reader = job_store.load_position_state(_POSITION_KEY)
    second_reader = job_store.load_position_state(_POSITION_KEY)
    assert first_reader["revision"] == second_reader["revision"] == 1

    first_reader["snapshot"]["stop"] = 95
    assert job_store.save_position_state(_POSITION_KEY, first_reader["revision"], first_reader)
    winning_state = job_store.load_position_state(_POSITION_KEY)
    assert winning_state["revision"] == 2
    assert winning_state["snapshot"]["stop"] == 95

    second_reader["snapshot"]["stop"] = 80
    assert job_store.save_position_state(_POSITION_KEY, second_reader["revision"], second_reader) is False
    assert job_store.load_position_state(_POSITION_KEY) == winning_state
    assert job_store.save_position_state(_POSITION_KEY, 0, initial, identity=identity) is False
    assert job_store.load_position_state(_POSITION_KEY) == winning_state


@pytest.fixture
def deleted_position_with_stale_reader(postgres_schema, identity):
    assert job_store.save_position_state(
        _POSITION_KEY, 0, {"snapshot": {"stop": 90}, "orders": []}, identity=identity,
    )
    original = job_store.load_position_state(_POSITION_KEY)
    original["snapshot"]["stop"] = 95
    assert job_store.save_position_state(_POSITION_KEY, original["revision"], original)
    stale_reader = job_store.load_position_state(_POSITION_KEY)
    assert stale_reader["revision"] == 2
    assert job_store.delete_position_state(_POSITION_KEY, stale_reader["revision"]) is True
    assert job_store.load_position_state(_POSITION_KEY) is None
    return stale_reader


@pytest.mark.xfail(
    strict=True,
    raises=StaleSaveInvariantError,
    reason=(
        "save_position_state inserts an absent row even when expected_revision is nonzero, "
        "so a stale writer can resurrect a deleted position"
    ),
)
def test_stale_save_after_delete_is_rejected_without_resurrection(deleted_position_with_stale_reader):
    stale_reader = deleted_position_with_stale_reader
    saved = job_store.save_position_state(_POSITION_KEY, stale_reader["revision"], stale_reader)
    stored = job_store.load_position_state(_POSITION_KEY)

    if saved is not False or stored is not None:
        raise StaleSaveInvariantError(f"stale save returned {saved!r}, persisted state: {stored!r}")


def test_stale_delete_preserves_independent_writer_update(postgres_schema):
    initial = {"snapshot": {"stop": 90}, "orders": []}
    assert job_store.save_position_state(_POSITION_KEY, 0, initial)
    stale_reader_ready = Future()
    allow_delete = Event()

    def delete_from_independent_reader():
        try:
            stale_reader = job_store.load_position_state(_POSITION_KEY)
        except Exception as exc:
            stale_reader_ready.set_exception(exc)
            raise
        stale_reader_ready.set_result(stale_reader)
        if not allow_delete.wait(timeout=10):
            raise TimeoutError("writer did not permit the stale delete")
        return job_store.delete_position_state(_POSITION_KEY, stale_reader["revision"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_delete = executor.submit(delete_from_independent_reader)
        try:
            assert stale_reader_ready.result(timeout=10)["revision"] == 1
            current = job_store.load_position_state(_POSITION_KEY)
            assert current["revision"] == 1
            current["snapshot"]["stop"] = 95
            assert job_store.save_position_state(_POSITION_KEY, current["revision"], current) is True
        finally:
            allow_delete.set()
        assert stale_delete.result(timeout=10) is False

    stored = job_store.load_position_state(_POSITION_KEY)
    assert stored["revision"] == 2
    assert stored["snapshot"]["stop"] == 95


@pytest.fixture
def lease_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(job_store, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def test_expired_lease_owner_cannot_release_replacement_lease(postgres_schema, lease_clock):
    now = lease_clock
    first_owner = job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10)
    assert first_owner is not None
    assert job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10) is None

    now[0] = 109.0
    assert job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10) is None
    now[0] = 110.0
    second_owner = job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10)
    assert second_owner is not None
    assert second_owner != first_owner
    assert job_store.release_position_monitor_lease(_POSITION_KEY, first_owner) is False
    assert job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10) is None
    assert job_store.release_position_monitor_lease(_POSITION_KEY, second_owner) is True
    third_owner = job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10)
    assert third_owner is not None
    assert job_store.release_position_monitor_lease(_POSITION_KEY, third_owner) is True


def test_expired_lease_owner_still_can_save_if_revision_is_current(postgres_schema, lease_clock):
    assert job_store.save_position_state(
        _POSITION_KEY, 0, {"snapshot": {"stop": 90}, "orders": []},
    )
    first_owner = job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10)
    assert first_owner is not None
    stale_owner_state = job_store.load_position_state(_POSITION_KEY)
    assert stale_owner_state["revision"] == 1

    lease_clock[0] = 110.0
    second_owner = job_store.acquire_position_monitor_lease(_POSITION_KEY, lease_seconds=10)
    assert second_owner is not None
    assert second_owner != first_owner
    assert job_store.release_position_monitor_lease(_POSITION_KEY, first_owner) is False

    stale_owner_state["snapshot"]["stop"] = 95
    assert job_store.save_position_state(
        _POSITION_KEY, stale_owner_state["revision"], stale_owner_state,
    ) is True
    stored = job_store.load_position_state(_POSITION_KEY)
    assert stored["revision"] == 2
    assert stored["snapshot"]["stop"] == 95
    assert job_store.release_position_monitor_lease(_POSITION_KEY, second_owner) is True
