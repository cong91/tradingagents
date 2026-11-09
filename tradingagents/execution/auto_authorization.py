"""Process-local authorization for new spot and futures entries.

Mode writers must hold ``mode_update_lock`` across persistence and ``set_config``.
Callers that snapshot configuration before starting work should keep the token
returned by ``capture_process_config`` with that work until submission.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import Context
from dataclasses import dataclass
from typing import TypeVar

from server.auto_modes import MODE_KEYS, AutoMode
from tradingagents.dataflows.config import config_key_revision, get_config

_Result = TypeVar("_Result")
_MODE_LOCKS = {mode: threading.RLock() for mode in AutoMode}


class AutoAuthorizationError(PermissionError):
    """An entry's mode is disabled or its authorization has expired."""


@contextmanager
def mode_update_lock(modes: Iterable[AutoMode] = tuple(AutoMode)) -> Iterator[None]:
    """Serialize mode writes with captures and final entry submissions.

    Acquire multiple mode locks in enum order. Writers must commit their mode
    values and call ``set_config`` before releasing the locks.
    """
    requested = set(modes)
    if not requested.issubset(_MODE_LOCKS):
        raise ValueError("unknown auto mode")
    with ExitStack() as stack:
        for mode in AutoMode:
            if mode in requested:
                stack.enter_context(_MODE_LOCKS[mode])
        yield


def _process_config() -> dict:
    # A graph's run_config context must not hide an operator's process update.
    return Context().run(get_config)


@dataclass(frozen=True)
class _ModeSnapshot:
    enabled: bool
    revision: int


@dataclass(frozen=True)
class AuthorizationSnapshot:
    """Immutable per-mode permission and generation at work creation."""

    spot: _ModeSnapshot
    futures: _ModeSnapshot

    def _allows_locked(self, mode: AutoMode, *, live: bool) -> bool:
        captured = self.spot if mode is AutoMode.SPOT else self.futures
        key = MODE_KEYS[mode]
        return (
            captured.enabled
            and captured.revision == config_key_revision(key)
            and (not live or _process_config().get(key) is True)
        )

    def allows(self, mode: AutoMode, *, live: bool = False) -> bool:
        """Check this generation without holding a lock over caller work."""
        with mode_update_lock((mode,)):
            return self._allows_locked(mode, live=live)

    def submit(
        self,
        mode: AutoMode,
        call: Callable[[], _Result],
        *,
        preflight: Callable[[], None] | None = None,
    ) -> _Result:
        """Recheck permission and call the venue while holding the mode lock.

        A futures operation-lock health check runs outside the mode lock. If
        the mode lock was contended, repeat that check after the wait.
        """
        lock = _MODE_LOCKS[mode]
        if preflight is None:
            lock.acquire()
        else:
            while True:
                preflight()
                if lock.acquire(blocking=False):
                    break
                with lock:
                    pass
        try:
            if not self._allows_locked(mode, live=True):
                raise AutoAuthorizationError(
                    f"Auto {mode.name.title()} is disabled or authorization is stale; "
                    "nothing was sent"
                )
            return call()
        finally:
            lock.release()


def capture_authorization(config: Mapping[str, object] | None = None) -> AuthorizationSnapshot:
    """Capture both modes; explicit config can authorize standalone dry runs."""
    with mode_update_lock():
        source = _process_config() if config is None else config
        snapshots = {
            mode: _ModeSnapshot(source.get(key) is True, config_key_revision(key))
            for mode, key in MODE_KEYS.items()
        }
        return AuthorizationSnapshot(snapshots[AutoMode.SPOT], snapshots[AutoMode.FUTURES])


def capture_process_config() -> tuple[dict, AuthorizationSnapshot]:
    """Atomically capture process config and its entry authorization token."""
    with mode_update_lock():
        config = _process_config()
        return config, capture_authorization(config)
