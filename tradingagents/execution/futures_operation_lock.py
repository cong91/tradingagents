"""Process-shared PostgreSQL fencing for futures operations.

The lock is deliberately keyed only by the normalized venue symbol. Account and
profile values are excluded so an old or differently configured worker cannot
bypass the symbol-wide entry fence. A dedicated session is retained for the
whole critical section; callers must check it immediately before every venue
call and durable state transition.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from server import postgres

_NAMESPACE = "tradingagents:futures-symbol-operation:v1:"
_STATE = threading.local()


class OperationLockError(RuntimeError):
    """Base class for fail-closed operation-lock failures."""


class OperationLockBusyError(OperationLockError):
    """Another process or thread currently owns the symbol lock."""


class OperationLockLostError(OperationLockError):
    """The dedicated PostgreSQL session can no longer prove ownership."""


class OperationLockOrderError(OperationLockError):
    """A caller attempted to acquire a different symbol while holding a lock."""


class OperationGuard:
    """A live symbol guard backed by one PostgreSQL backend session."""

    def __init__(self, symbol: str, connection: Any) -> None:
        self.symbol = symbol
        self.connection = connection
        self._depth = 1
        self._lost = False

    def check(self) -> None:
        """Prove this backend is alive and still owns its advisory lock."""
        if self._lost or getattr(self.connection, "closed", False):
            self._lost = True
            raise OperationLockLostError(f"operation lock connection lost for {self.symbol}")
        try:
            row = self.connection.execute(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_locks
                       WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                         AND granted AND objsubid = 1
                         AND ((classid::bigint << 32) | objid::bigint)
                             = hashtextextended(%s, 0)
                   ) AS locked""",
                (_lock_key(self.symbol),),
            ).fetchone()
            if not row or not bool(row["locked"] if isinstance(row, dict) else row[0]):
                raise OperationLockLostError(
                    f"operation lock ownership lost for {self.symbol}"
                )
        except OperationLockError:
            self._lost = True
            raise
        except Exception as exc:
            self._lost = True
            raise OperationLockLostError(
                f"operation lock health check failed for {self.symbol}"
            ) from exc

    def _enter_nested(self) -> None:
        self.check()
        self._depth += 1

    def _exit_nested(self) -> None:
        self._depth -= 1

    def _release(self) -> None:
        try:
            if not self._lost and not getattr(self.connection, "closed", False):
                self.connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (_lock_key(self.symbol),),
                )
        except Exception:
            self._lost = True
        finally:
            self.connection.close()


def _lock_key(symbol: str) -> str:
    normalized = str(symbol).strip()
    if not normalized:
        raise ValueError("futures operation lock requires a symbol")
    return _NAMESPACE + normalized


def _active() -> dict[str, OperationGuard]:
    active = getattr(_STATE, "active", None)
    if active is None:
        active = {}
        _STATE.active = active
    return active


@contextmanager
def operation_lock(symbol: str) -> Iterator[OperationGuard]:
    """Acquire a nonblocking symbol-wide PostgreSQL operation guard.

    Same-thread reentry for the same symbol reuses the existing session. A
    different symbol cannot be acquired while any operation lock is held in the
    thread, which keeps lock ordering explicit instead of hiding a deadlock.
    """
    normalized = str(symbol).strip()
    if not normalized:
        raise ValueError("futures operation lock requires a symbol")
    active = _active()
    existing = active.get(normalized)
    if existing is not None:
        existing._enter_nested()
        try:
            yield existing
        finally:
            existing._exit_nested()
        return
    if active:
        held = next(iter(active))
        raise OperationLockOrderError(
            f"cannot acquire futures operation lock for {normalized} while holding {held}"
        )

    connection = None
    guard = None
    try:
        connection = postgres.connect()
        connection.autocommit = True
        row = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
            (_lock_key(normalized),),
        ).fetchone()
        locked = bool(row["locked"] if isinstance(row, dict) else row[0]) if row else False
        if not locked:
            raise OperationLockBusyError(
                f"futures operation lock is busy for {normalized}"
            )
        guard = OperationGuard(normalized, connection)
        active[normalized] = guard
    except OperationLockError:
        if connection is not None and not getattr(connection, "closed", False):
            connection.close()
        raise
    except Exception as exc:
        if connection is not None and not getattr(connection, "closed", False):
            connection.close()
        raise OperationLockError(
            f"futures operation lock unavailable for {normalized}"
        ) from exc

    try:
        guard.check()
        yield guard
    finally:
        active.pop(normalized, None)
        guard._release()


def require_operation_lock(symbol: str) -> OperationGuard:
    """Return the current thread's guard or fail closed."""
    normalized = str(symbol).strip()
    guard = _active().get(normalized)
    if guard is None:
        raise OperationLockError(
            f"futures operation lock is required for {normalized}"
        )
    guard.check()
    return guard
