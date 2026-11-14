"""Session ownership for one outcome-ledger poll per PostgreSQL schema."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from server import postgres

_NAMESPACE = "tradingagents:outcome-ledger-poll:v1:"
_STATE = threading.local()


class PollLockError(RuntimeError):
    """A poll could not establish or retain its database ownership."""


class PollLockBusyError(PollLockError):
    """Another session owns the poll for this ledger schema."""


class PollLockLostError(PollLockError):
    """The owner session can no longer prove it holds the poll lock."""


def _lock_key(connection: Any) -> str:
    # Resolve the actual relation: current_schema() may be an unrelated
    # temporary or earlier search-path schema while both sessions use the same
    # ledger. The relation OID survives schema renames and moves, unlike a
    # schema-name-based key.
    row = connection.execute(
        """SELECT c.oid::bigint AS relation_oid, n.nspname AS schema_name
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.oid = to_regclass('outcome_ledger_state')""",
    ).fetchone()
    if not row:
        raise PollLockError("outcome ledger state table is unavailable")
    return _NAMESPACE + str(row["relation_oid"])


class PollGuard:
    """A session lock whose connection also performs every poll transaction."""

    def __init__(self, connection: Any, key: str) -> None:
        self.connection = connection
        self._key = key
        self._lost = False

    def check(self) -> None:
        """Fail closed if this backend no longer owns its advisory lock."""
        if self._lost or getattr(self.connection, "closed", False):
            self._lost = True
            raise PollLockLostError("outcome ledger poll lock connection lost")
        try:
            row = self.connection.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_locks
                       WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                         AND granted AND objsubid = 1
                         AND ((classid::bigint << 32) | objid::bigint)
                             = hashtextextended(%s, 0)
                   ) AS locked""",
                (self._key,),
            ).fetchone()
            if not row or not row["locked"]:
                raise PollLockLostError("outcome ledger poll lock ownership lost")
        except PollLockError:
            self._lost = True
            raise
        except Exception as exc:
            self._lost = True
            raise PollLockLostError("outcome ledger poll lock health check failed") from exc

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Run one short transaction on the same backend that holds the lock."""
        self.check()
        try:
            with self.connection.transaction():
                yield self.connection
                self.check()
        except PollLockError:
            raise
        except Exception:
            # The transaction context has rolled back. Distinguish a healthy
            # constraint failure from a lost backend before a caller retries.
            self.check()
            raise

    def _release(self) -> None:
        try:
            if not self._lost and not getattr(self.connection, "closed", False):
                self.connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (self._key,),
                )
        except Exception:
            self._lost = True
        finally:
            self.connection.close()


def current_guard() -> PollGuard | None:
    """Return this thread's poll owner, if one is active."""
    return getattr(_STATE, "guard", None)


@contextmanager
def poll_lock() -> Iterator[PollGuard]:
    """Acquire a nonblocking schema-specific lock for the entire poll."""
    if current_guard() is not None:
        raise PollLockError("outcome ledger poll lock is already active in this thread")

    connection = None
    try:
        connection = postgres.connect()
        connection.autocommit = True
        key = _lock_key(connection)
        row = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", (key,),
        ).fetchone()
        if not row or not row["locked"]:
            raise PollLockBusyError("outcome ledger poll is already owned")
        guard = PollGuard(connection, key)
    except PollLockError:
        if connection is not None:
            connection.close()
        raise
    except Exception as exc:
        if connection is not None:
            connection.close()
        raise PollLockError("outcome ledger poll lock unavailable") from exc

    _STATE.guard = guard
    try:
        guard.check()
        yield guard
        guard.check()
    finally:
        _STATE.guard = None
        guard._release()
