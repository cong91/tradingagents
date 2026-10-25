"""Safety checks for per-test PostgreSQL schema cleanup."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import psycopg
import pytest

from server import postgres

_FIXTURES_SPEC = importlib.util.spec_from_file_location(
    "tradingagents_test_conftest", Path(__file__).with_name("conftest.py")
)
assert _FIXTURES_SPEC is not None and _FIXTURES_SPEC.loader is not None
test_fixtures = importlib.util.module_from_spec(_FIXTURES_SPEC)
_FIXTURES_SPEC.loader.exec_module(test_fixtures)


def test_postgres_schema_waits_for_persistence_workers_before_drop(monkeypatch):
    timeline: list[str] = []
    timeline_lock = threading.Lock()
    writer_ready = threading.Event()
    release_writer = threading.Event()
    cleanup_entered = threading.Event()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            if statement.startswith("CREATE SCHEMA"):
                with timeline_lock:
                    timeline.append("create_schema")
            elif statement.startswith("DROP SCHEMA"):
                with timeline_lock:
                    timeline.append("drop_schema")

    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(postgres, "migrate", lambda: None)
    monkeypatch.setenv("TRADINGAGENTS_TEST_DATABASE_URL", "postgresql://test/db_test")

    fixture = test_fixtures.postgres_schema.__wrapped__(monkeypatch)
    next(fixture)

    def persist_after_release():
        writer_ready.set()
        release_writer.wait()
        with timeline_lock:
            timeline.append("worker_write")

    worker = threading.Thread(target=persist_after_release, name="server-run-fixture-test")
    worker.start()
    assert writer_ready.wait(timeout=1)

    join_workers = test_fixtures._join_test_persistence_workers

    def observe_join():
        cleanup_entered.set()
        join_workers()

    monkeypatch.setattr(test_fixtures, "_join_test_persistence_workers", observe_join)
    cleanup_errors: list[BaseException] = []

    def finish_fixture():
        try:
            next(fixture)
        except StopIteration:
            pass
        except BaseException as exc:
            cleanup_errors.append(exc)

    cleanup = threading.Thread(target=finish_fixture, name="fixture-teardown")
    cleanup.start()

    try:
        assert cleanup_entered.wait(timeout=1)
        with timeline_lock:
            assert "drop_schema" not in timeline
        release_writer.set()
        cleanup.join(timeout=1)
    finally:
        release_writer.set()
        cleanup.join(timeout=1)
        worker.join(timeout=1)

    assert not cleanup.is_alive()
    assert not worker.is_alive()
    assert cleanup_errors == []
    with timeline_lock:
        assert timeline.index("worker_write") < timeline.index("drop_schema")


def test_postgres_schema_rejects_runtime_database_name(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_TEST_DATABASE_URL", "postgresql://test/tradingagents")
    monkeypatch.setattr(
        psycopg, "connect", lambda *_args, **_kwargs: pytest.fail("must reject before connecting")
    )

    fixture = test_fixtures.postgres_schema.__wrapped__(monkeypatch)
    with pytest.raises(RuntimeError, match="dedicated test database"):
        next(fixture)
