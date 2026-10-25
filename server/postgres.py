"""Explicit PostgreSQL connection and versioned migration runner."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).with_name("migrations")
_LEDGER = "schema_migrations"
_LOCK_KEY = 0x54524144494E47


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def _load_migrations() -> list[Migration]:
    migrations = []
    seen_versions: set[int] = set()
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(path.name[:4])
        if version in seen_versions:
            raise RuntimeError(f"Duplicate migration version: {version}")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        migrations.append(Migration(version, path.name, sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()))
    return migrations


def connect():
    dsn = os.environ.get("TRADINGAGENTS_DATABASE_URL")
    if not dsn:
        raise RuntimeError("TRADINGAGENTS_DATABASE_URL must be set for PostgreSQL")
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


@contextmanager
def session() -> Iterator[psycopg.Connection]:
    """One write transaction: commit on exit, roll back on error."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[psycopg.Connection]:
    """A connection for SELECTs; the transaction is closed on exit."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()



def migrate(*, dry_run: bool = False) -> list[str]:
    """Return migration plans, or apply unapplied files atomically under a DB lock."""
    migrations = _load_migrations()
    if dry_run:
        # A plan is intentionally offline: it neither connects nor changes DB state.
        return [migration.name for migration in migrations]

    applied: list[str] = []
    with connect() as conn:
        # Session-level lock must be acquired outside a transaction. Otherwise
        # PostgreSQL's implicit transaction can hide the lock acquisition from
        # concurrent runners until its commit.
        conn.autocommit = True
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            with conn.transaction():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_LEDGER} ("
                    "version integer PRIMARY KEY, name text NOT NULL, "
                    "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                rows = conn.execute(
                    f"SELECT version, name, checksum FROM {_LEDGER}"
                ).fetchall()
                known = {row["version"]: row for row in rows}
                for migration in migrations:
                    row = known.get(migration.version)
                    if row is not None:
                        if row["name"] != migration.name or row["checksum"] != migration.checksum:
                            raise RuntimeError(f"Migration checksum or name changed: {migration.name}")
                        continue
                    conn.execute(migration.sql)
                    conn.execute(
                        f"INSERT INTO {_LEDGER}(version, name, checksum) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                    applied.append(migration.name)
        finally:
            # Unlock runs even when a migration fails; failed DDL rolls back.
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied


def is_readable() -> bool:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except (psycopg.Error, RuntimeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or apply PostgreSQL SQL migrations")
    parser.add_argument("--dry-run", action="store_true", help="list migration files without connecting")
    args = parser.parse_args()
    try:
        migrations = migrate(dry_run=args.dry_run)
    except (OSError, RuntimeError, psycopg.Error) as exc:
        parser.exit(1, f"Migration failed: {exc}\n")
    for name in migrations:
        print(("plan: " if args.dry_run else "applied: ") + name)
    if not migrations:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
