"""Cycle-scoped ownership and cleanup of the active Kronos service."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tradingagents.forecast.service import KronosForecastService

logger = logging.getLogger(__name__)

# A timed-out cycle can finish after dispatch returns. The next owner must not
# start a child until the previous owner's service has closed successfully.
_WARM_OWNER_SLOT = threading.Lock()
_LEASE_POLL_SECONDS = 0.1
_CLEANUP_ATTEMPTS = 2


class ActiveForecastUnavailable(RuntimeError):
    """The active forecast could not be produced; the caller degrades."""


class KronosForecastOwner:
    """Own one warm service through exclusive leases for one Futures mode cycle."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config)
        self._service: KronosForecastService | None = None
        self._active = False
        self._closing = False
        self._cleanup_active = False
        self._cleanup_error: Exception | None = None
        self._closed = False
        self._has_slot = False
        self._condition = threading.Condition()

    @contextmanager
    def lease(
        self, deadline: float | None, cancelled: threading.Event | None = None,
    ) -> Iterator[KronosForecastService]:
        with self._condition:
            while self._active and not self._closing:
                if cancelled is not None and cancelled.is_set():
                    raise ActiveForecastUnavailable("the cycle was cancelled waiting for a Kronos lease")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise ActiveForecastUnavailable("the cycle deadline was reached waiting for a Kronos lease")
                wait = remaining if cancelled is None else min(
                    remaining if remaining is not None else _LEASE_POLL_SECONDS,
                    _LEASE_POLL_SECONDS,
                )
                self._condition.wait(timeout=wait)
            if self._closing:
                raise RuntimeError("Kronos forecast owner is closing")
            if cancelled is not None and cancelled.is_set():
                raise ActiveForecastUnavailable("the cycle was cancelled before acquiring a Kronos lease")
            if deadline is not None and time.monotonic() >= deadline:
                raise ActiveForecastUnavailable("the cycle deadline was reached before acquiring a Kronos lease")
            self._active = True
        try:
            if not self._has_slot:
                while True:
                    with self._condition:
                        if self._closing:
                            raise ActiveForecastUnavailable("the Kronos forecast owner is closing")
                    if cancelled is not None and cancelled.is_set():
                        raise ActiveForecastUnavailable("the cycle was cancelled waiting for Kronos cleanup")
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise ActiveForecastUnavailable("the cycle deadline was reached waiting for Kronos cleanup")
                    wait = _LEASE_POLL_SECONDS if remaining is None else min(remaining, _LEASE_POLL_SECONDS)
                    if not _WARM_OWNER_SLOT.acquire(timeout=wait):
                        continue
                    with self._condition:
                        if self._closing:
                            _WARM_OWNER_SLOT.release()
                            raise ActiveForecastUnavailable("the Kronos forecast owner is closing")
                        self._has_slot = True
                        break
            with self._condition:
                if self._closing:
                    raise ActiveForecastUnavailable("the Kronos forecast owner is closing")
            if cancelled is not None and cancelled.is_set():
                raise ActiveForecastUnavailable("the cycle was cancelled before service preparation")
            if deadline is not None and time.monotonic() >= deadline:
                raise ActiveForecastUnavailable("the cycle deadline was reached before service preparation")
            # Construction remains outside the condition so close can stop waiters.
            if self._service is None:
                self._service = KronosForecastService.from_config(self._config)
            # A close that races construction does not revoke this already-held
            # lease. The caller checks cancellation and deadline before inference,
            # then this lease retains the final cleanup obligation.
            yield self._service
        finally:
            with self._condition:
                self._active = False
                if self._closing:
                    self._start_cleanup_locked()
                elif self._service is None:
                    self._release_slot_locked()
                self._condition.notify_all()

    def _release_slot_locked(self) -> None:
        if self._has_slot:
            self._has_slot = False
            _WARM_OWNER_SLOT.release()

    def _start_cleanup_locked(self) -> None:
        if self._active or self._closed or self._cleanup_active or self._cleanup_error is not None:
            return
        if self._service is None:
            self._closed = True
            self._release_slot_locked()
            self._condition.notify_all()
            return
        self._cleanup_active = True
        threading.Thread(
            target=self._cleanup, name="kronos-owner-cleanup", daemon=True,
        ).start()

    def _cleanup(self) -> None:
        for attempt in range(_CLEANUP_ATTEMPTS):
            try:
                assert self._service is not None
                self._service.close()
            except Exception as exc:
                logger.exception("kronos active: service cleanup attempt %s failed", attempt + 1)
                if attempt + 1 < _CLEANUP_ATTEMPTS:
                    continue
                with self._condition:
                    self._cleanup_error = exc
                    self._cleanup_active = False
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._service = None
                    self._closed = True
                    self._cleanup_active = False
                    self._release_slot_locked()
                    self._condition.notify_all()
                return

    def cleanup_state(self) -> str:
        """Return the cleanup state without claiming pending work is complete."""
        with self._condition:
            if self._closed:
                return "complete"
            if self._cleanup_error is not None:
                return "failed"
            return "pending"

    def close(self, timeout: float = 15.0) -> bool:
        """Stop leases and wait up to timeout; False means cleanup is pending or failed.

        Cleanup retries once after an exception. A failed or stuck close retains
        the slot, so a later cycle cannot start beside an unconfirmed child.
        """
        end = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closing = True
            self._start_cleanup_locked()
            self._condition.notify_all()
            while not self._closed and self._cleanup_error is None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            return self._closed
