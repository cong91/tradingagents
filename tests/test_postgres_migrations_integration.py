"""PostgreSQL migration runner integration tests against a disposable service."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).parents[1]
_COMPOSE = _ROOT / "tests" / "postgres-compose.yml"
_PROJECT = f"ta-migration-test-{os.getpid()}"
_SERVICE = "postgres-test"
_HOST_PORT: int | None = None


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _free_host_port() -> int:
    """Pick an unused host port below the OS ephemeral ranges.

    A listener inside the ephemeral range collides with the source port the OS
    hands a client, which fails connects with WSAEADDRINUSE/EADDRINUSE.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        for candidate in range(15433, 32000):
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("no free host port below the ephemeral range")


def _compose_env() -> dict[str, str]:
    global _HOST_PORT
    if _HOST_PORT is None:
        _HOST_PORT = _free_host_port()
    return {**os.environ, "TRADINGAGENTS_TEST_PG_PORT": str(_HOST_PORT)}


def _dsn() -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", _PROJECT, "-f", str(_COMPOSE), "port", _SERVICE, "5432"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=_compose_env(),
    )
    address, port = result.stdout.strip().rsplit(":", 1)
    host = address.strip("[]")
    return f"postgresql://migration:migration@{host}:{port}/migration_test"


@pytest.fixture(scope="module")
def postgres_dsn():
    if not _docker_available():
        pytest.skip("Docker engine is unavailable")

    subprocess.run(
        ["docker", "compose", "-p", _PROJECT, "-f", str(_COMPOSE), "up", "-d", "--wait"],
        cwd=_ROOT,
        check=True,
        timeout=180,
        env=_compose_env(),
    )
    dsn = _dsn()
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        yield dsn
    finally:
        subprocess.run(
            ["docker", "compose", "-p", _PROJECT, "-f", str(_COMPOSE), "down", "--volumes"],
            cwd=_ROOT,
            check=True,
            timeout=60,
            env=_compose_env(),
        )


def test_migrate_applies_once_and_checks_checksum(postgres_dsn, monkeypatch):
    from server import postgres

    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", postgres_dsn)
    assert postgres.migrate() == [
        "0001_core_ledger.sql",
        "0002_control_plane.sql",
        "0003_operator_settings.sql",
        "0004_data_source_credentials.sql",
        "0005_futures_account_identity.sql",
        "0006_engine_settings.sql",
        "0007_llm_credentials.sql",
        "0008_outcome_ledger.sql",
        "0009_experiment_registry.sql",
        "0010_shadow_forecasts.sql",
        "0011_experiment_run_kinds.sql",
        "0012_outcome_event_identity.sql",
        "0013_futures_cycle_timeframe_identity.sql",
        "0014_futures_cycle_identity_standardization.sql",
        "0015_forecast_evaluations.sql",
    ]
    assert postgres.migrate() == []

    with psycopg.connect(postgres_dsn) as conn:
        row = conn.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchone()
        assert row == (1, "0001_core_ledger.sql", postgres._load_migrations()[0].checksum)
        assert conn.execute("SELECT to_regclass('public.workspaces')").fetchone()[0] == "workspaces"
        assert conn.execute("SELECT to_regclass('public.app_schema_migrations')").fetchone()[0] is None


def test_migrate_serializes_concurrent_invocations(postgres_dsn):
    from server import postgres

    schema = f"migration_{uuid4().hex}"
    dsn = make_conninfo(postgres_dsn, options=f"-csearch_path={schema}")
    old = os.environ.get("TRADINGAGENTS_DATABASE_URL")
    with psycopg.connect(postgres_dsn) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    result: list[list[str]] = []
    failure: list[BaseException] = []
    started = Barrier(2)
    lock_conn = psycopg.connect(dsn, autocommit=True)
    lock_conn.execute("SELECT pg_advisory_lock(%s)", (postgres._LOCK_KEY,))

    def run_migration():
        os.environ["TRADINGAGENTS_DATABASE_URL"] = dsn
        started.wait(timeout=10)
        try:
            result.extend(postgres.migrate())
        except BaseException as exc:
            failure.append(exc)

    worker = Thread(target=run_migration)
    try:
        worker.start()
        started.wait(timeout=10)
        time.sleep(0.25)
        assert worker.is_alive(), "runner must wait while another session holds the advisory lock"
        lock_conn.execute("SELECT pg_advisory_unlock(%s)", (postgres._LOCK_KEY,))
        worker.join(timeout=30)
        assert not worker.is_alive(), "runner did not resume after advisory lock release"
        assert not failure
        assert result == [
            "0001_core_ledger.sql",
            "0002_control_plane.sql",
            "0003_operator_settings.sql",
            "0004_data_source_credentials.sql",
            "0005_futures_account_identity.sql",
            "0006_engine_settings.sql",
            "0007_llm_credentials.sql",
            "0008_outcome_ledger.sql",
            "0009_experiment_registry.sql",
            "0010_shadow_forecasts.sql",
            "0011_experiment_run_kinds.sql",
            "0012_outcome_event_identity.sql",
            "0013_futures_cycle_timeframe_identity.sql",
            "0014_futures_cycle_identity_standardization.sql",
            "0015_forecast_evaluations.sql",
        ]
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 15
            assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    finally:
        if worker.is_alive():
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (postgres._LOCK_KEY,))
            worker.join(timeout=30)
        lock_conn.close()
        if old is None:
            os.environ.pop("TRADINGAGENTS_DATABASE_URL", None)
        else:
            os.environ["TRADINGAGENTS_DATABASE_URL"] = old
        with psycopg.connect(postgres_dsn) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
