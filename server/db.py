"""PostgreSQL storage boundary for control-panel state.

The control panel's durable state lives in PostgreSQL; there is no SQLite
fallback. ``TRADINGAGENTS_DATABASE_URL`` selects the database and startup
applies the versioned migrations before a request is served.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from server import postgres


def connect():
    return postgres.connect()


@contextmanager
def session() -> Iterator:
    """One write transaction: commit on exit, roll back on error."""
    with postgres.session() as conn:
        yield conn


@contextmanager
def read() -> Iterator:
    conn = postgres.connect()
    try:
        yield conn
    finally:
        conn.close()


def is_readable() -> bool:
    return postgres.is_readable()
