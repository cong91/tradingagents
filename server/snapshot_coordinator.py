"""Bounded, process-local coordination for display-only venue reads.

Off by default: enabling requires ``TRADINGAGENTS_UI_SNAPSHOT_SYNC=true`` AND
``TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS=true``. The second flag is the
operator's assertion that exactly one API process serves UI reads; the
coordinator's single-flight, cache and rate window are per-process facts and
must not be presented as system-wide guarantees. Disabling this module never
affects the execution or monitor paths: they keep their own direct venue
reads (tradingagents/execution/futures_protection.py, brackets.py).

- ``freshness`` is always ``unknown``: a transport timestamp does not prove
  the age of the source data at the venue.
- Mutation outcomes are hints, not evidence of fills. An undisturbed read
  clears dirty for this display cache, not a read-after-write guarantee.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from server import performance_history_cache as history_cache
from tradingagents.execution.exchange_client import venue_key as bridge_venue_key

_SECRET = secrets.token_bytes(32)
_LOCK = threading.Lock()
_EVENTS_CHANGED = threading.Condition(_LOCK)
_EPOCH = uuid.uuid4().hex
_GENERATION: dict[str, int] = {}
_PENDING: dict[str, int] = {}
_TOKENS: dict[str, tuple[str, float]] = {}
_DIRTY: set[str] = set()
_IN_FLIGHT: dict[tuple, Future] = {}
_CACHE: dict[tuple, tuple[float, Any, dict]] = {}
_EVENTS: deque[dict] = deque(maxlen=256)
_EVENT_SEQUENCE = 0
_OVERFLOW = threading.Event()
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ui-snapshot")
_LAST_START: dict[tuple, float] = {}
_ACCOUNT_WINDOW: dict[str, deque[float]] = {}

MAX_IN_FLIGHT = 4
MAX_CACHE_ITEMS = 64
CACHE_TTL_SECONDS = 25.0
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_SOURCE_AGE_SECONDS = 300.0
MAX_READS_PER_MINUTE = 12
PENDING_TIMEOUT_SECONDS = 120.0
SYNC_ENV_VAR = "TRADINGAGENTS_UI_SNAPSHOT_SYNC"
SINGLE_PROCESS_ENV_VAR = "TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS"


class SnapshotUnavailable(RuntimeError):
    """A bounded UI read could not be scheduled or completed in time."""


def _true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def enabled() -> bool:
    """Both the sync switch and the single-process assertion must be set."""
    return _true(os.environ.get(SYNC_ENV_VAR)) and _true(
        os.environ.get(SINGLE_PROCESS_ENV_VAR)
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def credential_context(bridge: Any, client: Any | None = None) -> str | None:
    """HMAC the credentials the client actually carries; never return raw keys.

    Called with the same client instance the read or mutation uses, so a
    credential rotation rebinds the context immediately instead of trusting a
    key captured at bridge construction.
    """
    if client is None:
        client = bridge.client()
    key = getattr(client, "apiKey", None)
    secret = getattr(client, "secret", None)
    if not isinstance(key, str) or not key or not isinstance(secret, str) or not secret:
        return None
    payload = "\x00".join((bridge_venue_key(bridge), key, secret)).encode("utf-8", "strict")
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _generation_locked(context: str) -> int:
    return _GENERATION.get(context, 0)


def _expire_pending_locked(now: float) -> None:
    for token, (context, started) in tuple(_TOKENS.items()):
        if now - started >= PENDING_TIMEOUT_SECONDS:
            _TOKENS.pop(token, None)
            _PENDING[context] = max(0, _PENDING.get(context, 0) - 1)
            _GENERATION[context] = _generation_locked(context) + 1
            _DIRTY.add(context)


def _consume_overflow_locked(context: str) -> None:
    if _OVERFLOW.is_set():
        _OVERFLOW.clear()
        contexts = set(_GENERATION) | {context} | {key[0] for key in _CACHE}
        contexts.update(key[0] for key in _IN_FLIGHT)
        for item in contexts:
            _GENERATION[item] = _generation_locked(item) + 1
            _DIRTY.add(item)


def _metadata(
    *,
    context: str | None,
    resource: str,
    generation_value: int | None,
    request_started_at: str | None,
    response_received_at: str | None,
    last_attempt_at: str | None,
    error: str | None = None,
) -> dict:
    dirty = context in _DIRTY if context is not None else False
    pending = _PENDING.get(context, 0) > 0 if context is not None else False
    if _OVERFLOW.is_set():
        dirty = True
    return {
        "resource": resource,
        "context_id": context,
        "epoch": _EPOCH,
        "generation": generation_value,
        "status": "unavailable" if error else "partial",
        # No source timestamp is proven in this codebase: transport windows
        # describe the read, never the age of the data at the venue.
        "freshness": "unknown",
        "pending": pending,
        "dirty": dirty,
        "request_started_at": request_started_at,
        "response_received_at": response_received_at,
        "source_timestamp": None,
        "last_attempt_at": last_attempt_at,
        "retention_seconds": MAX_SOURCE_AGE_SECONDS,
        "error": error,
        "skipped": None,
    }


def _run(loader: Callable[[], Any]) -> tuple[Any, str, str, float]:
    started = _now()
    result = loader()
    received = _now()
    return result, started, received, monotonic()


def _cached_locked(cached: tuple, context: str, *, stale: bool, reason: str | None) -> tuple[Any, dict]:
    result, metadata = copy.deepcopy(cached[1]), copy.deepcopy(cached[2])
    metadata.update({
        "cache": "stale" if stale else "hit",
        "dirty": (
            context in _DIRTY or _OVERFLOW.is_set()
            or metadata.get("generation") != _generation_locked(context)
        ),
        "pending": _PENDING.get(context, 0) > 0,
        "error": reason,
        "last_attempt_at": _now(),
    })
    return result, metadata


def read(
    context: str | None,
    resource: str,
    params: Any,
    loader: Callable[[], Any],
    *,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    min_interval_seconds: float = 5.0,
) -> tuple[Any, dict]:
    """Coalesce reads and bound starts across all generations of a key.

    Timestamps on cached data remain those of the original read. A superseded
    in-flight result is never published; an older cached result can instead be
    returned explicitly as stale until its retention limit.
    """
    if not enabled() or context is None:
        started = _now()
        result = loader()
        received = _now()
        return result, _metadata(
            context=None, resource=resource, generation_value=None,
            request_started_at=started, response_received_at=received,
            last_attempt_at=started,
        )

    key = (context, resource, _canonical(params))
    with _LOCK:
        now = monotonic()
        _consume_overflow_locked(context)
        _expire_pending_locked(now)
        generation_value = _generation_locked(context)
        cached = _CACHE.get(key)
        if cached is not None and now - cached[0] >= MAX_SOURCE_AGE_SECONDS:
            _CACHE.pop(key, None)
            cached = None
        dirty = context in _DIRTY or _OVERFLOW.is_set()
        pending = _PENDING.get(context, 0) > 0
        if (cached and cached[2].get("generation") == generation_value
                and not dirty and not pending and now - cached[0] < ttl_seconds):
            reason = "rate_limit_cooldown" if history_cache.is_rate_limited() else None
            return _cached_locked(cached, context, stale=reason is not None, reason=reason)
        flight_key = (*key, generation_value)
        future = _IN_FLIGHT.get(flight_key)
        created = future is None
        if created:
            reason = None
            last_start = _LAST_START.get(key)
            if history_cache.is_rate_limited():
                reason = "rate_limit_cooldown"
            elif len(_IN_FLIGHT) >= MAX_IN_FLIGHT:
                reason = "capacity"
            elif last_start is not None and now - last_start < min_interval_seconds:
                reason = "rate_limited"
            window = _ACCOUNT_WINDOW.setdefault(context, deque())
            while window and window[0] <= now - 60.0:
                window.popleft()
            if len(window) >= MAX_READS_PER_MINUTE:
                reason = "rate_limited"
            if reason:
                if cached is not None:
                    return _cached_locked(cached, context, stale=True, reason=reason)
                raise SnapshotUnavailable(reason)
            window.append(now)
            _LAST_START[key] = now
            future = _POOL.submit(_run, loader)
            _IN_FLIGHT[flight_key] = future
        last_attempt = _now()

    if created:
        future.add_done_callback(lambda completed, flight=flight_key: _finished(flight, completed))

    try:
        result, started, received, completed_at = future.result(timeout=REQUEST_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        future.add_done_callback(lambda completed, flight=flight_key: _discard(flight, completed))
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and monotonic() - cached[0] < MAX_SOURCE_AGE_SECONDS:
                return _cached_locked(cached, context, stale=True, reason="timeout")
        raise SnapshotUnavailable("timeout") from exc

    except SnapshotUnavailable:
        _discard(flight_key, future)
        raise
    except Exception:
        _discard(flight_key, future)
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and monotonic() - cached[0] < MAX_SOURCE_AGE_SECONDS:
                return _cached_locked(cached, context, stale=True, reason="read_failed")
        raise

    with _LOCK:
        if _IN_FLIGHT.get(flight_key) is future:
            _IN_FLIGHT.pop(flight_key, None)
        _consume_overflow_locked(context)
        _expire_pending_locked(monotonic())
        current_generation = _generation_locked(context)
        if current_generation != generation_value:
            raise SnapshotUnavailable("superseded")
        pending = _PENDING.get(context, 0) > 0
        if not pending:
            _DIRTY.discard(context)
        metadata = _metadata(
            context=context, resource=resource, generation_value=current_generation,
            request_started_at=started, response_received_at=received,
            last_attempt_at=last_attempt,
        )
        metadata["cache"] = "miss"
        if not pending and not _OVERFLOW.is_set() and ttl_seconds > 0 and not (
            isinstance(result, dict) and result.get("_snapshot_no_cache")
        ):
            _CACHE[key] = (completed_at, copy.deepcopy(result), copy.deepcopy(metadata))
            while len(_CACHE) > MAX_CACHE_ITEMS:
                _CACHE.pop(next(iter(_CACHE)))
        return result, metadata


def _discard(key: tuple, future: Future) -> None:
    with _LOCK:
        if _IN_FLIGHT.get(key) is future:
            _IN_FLIGHT.pop(key, None)


def _finished(key: tuple, future: Future) -> None:
    if future.exception() is not None:
        _discard(key, future)


def publish_mutation(
    bridge: Any,
    client: Any,
    *,
    operation: str,
    symbol: str | None,
    phase: str,
    token: str | None = None,
) -> str | None:
    """Invalidate UI generations synchronously, without any venue or store I/O.

    Uses a try-lock so an observer running under the bracket task lock can
    never block the protection path. A lost notification sets an overflow flag;
    the next read invalidates known generations before clearing it.
    """
    if not enabled():
        return None
    try:
        context = credential_context(bridge, client)
        if context is None or phase not in {"pending", "outcome"}:
            _OVERFLOW.set()
            return None
        if not _LOCK.acquire(blocking=False):
            _OVERFLOW.set()
            return None
        try:
            if phase == "pending":
                token = uuid.uuid4().hex
                _TOKENS[token] = (context, monotonic())
                _PENDING[context] = _PENDING.get(context, 0) + 1
            elif token is not None:
                pending = _TOKENS.get(token)
                if pending is not None and pending[0] == context:
                    _TOKENS.pop(token, None)
                    _PENDING[context] = max(0, _PENDING.get(context, 0) - 1)
            _GENERATION[context] = _generation_locked(context) + 1
            _DIRTY.add(context)
            _append_event_locked({
                "type": "dirty",
                "context_id": context,
                "generation": _GENERATION[context],
                "resource": "account",
                "symbol": symbol if isinstance(symbol, str) else None,
                "operation": operation,
                "phase": phase,
                # An outcome marker says the operation returned; it is never
                # proof of a fill (status alone does not prove fills).
                "outcome": "uncertain" if phase == "outcome" else None,
                "occurred_at": _now(),
            })
            return token
        finally:
            _LOCK.release()
    except Exception:
        _OVERFLOW.set()
        return None


def _append_event_locked(event: dict) -> None:
    global _EVENT_SEQUENCE
    _EVENT_SEQUENCE += 1
    event["id"] = _EVENT_SEQUENCE
    event["epoch"] = _EPOCH
    _EVENTS.append(event)
    _EVENTS_CHANGED.notify_all()


def latest_event_id() -> int:
    with _LOCK:
        return _EVENT_SEQUENCE


def events_after(last_id: int, wait_seconds: float = 15.0) -> tuple[list[dict], bool]:
    """Buffered invalidations after ``last_id``; ``(events, missed_history)``.

    A subscriber whose last id predates the buffer receives one ``reset``
    event instead of a fabricated gap-free history; the client must treat it
    as "everything is dirty" and re-fetch.
    """
    with _EVENTS_CHANGED:
        if _EVENTS and last_id > _EVENTS[-1]["id"]:
            return [{
                "id": _EVENTS[-1]["id"], "epoch": _EPOCH, "type": "reset",
                "context_id": None, "generation": None, "resource": "account",
                "occurred_at": _now(),
            }], True
        if not _EVENTS or _EVENTS[-1]["id"] <= last_id:
            _EVENTS_CHANGED.wait(timeout=wait_seconds)
        if not _EVENTS:
            return [], False
        oldest = _EVENTS[0]["id"]
        missed = bool(last_id and last_id < oldest - 1)
        if missed:
            return [{
                "id": _EVENTS[-1]["id"],
                "epoch": _EPOCH,
                "type": "reset",
                "context_id": None,
                "generation": None,
                "resource": "account",
                "occurred_at": _now(),
            }], True
        return [copy.deepcopy(event) for event in _EVENTS if event["id"] > last_id], False


def status(context: str | None = None) -> dict:
    """Metadata only; never claims source freshness or full venue coverage."""
    if not _LOCK.acquire(blocking=False):
        _OVERFLOW.set()
        return _metadata(
            context=context,
            resource="account",
            generation_value=None,
            request_started_at=None,
            response_received_at=None,
            last_attempt_at=None,
            error="coordinator_busy",
        )
    try:
        return _metadata(
            context=context,
            resource="account",
            generation_value=_generation_locked(context) if context else None,
            request_started_at=None,
            response_received_at=None,
            last_attempt_at=None,
        )
    finally:
        _LOCK.release()


def reset_for_tests() -> None:
    """Reset process-local state for isolated unit tests."""
    global _EPOCH, _EVENT_SEQUENCE
    with _LOCK:
        _EPOCH = uuid.uuid4().hex
        _GENERATION.clear()
        _PENDING.clear()
        _TOKENS.clear()
        _DIRTY.clear()
        _IN_FLIGHT.clear()
        _CACHE.clear()
        _EVENTS.clear()
        _EVENT_SEQUENCE = 0
        _OVERFLOW.clear()
        _LAST_START.clear()
        _ACCOUNT_WINDOW.clear()
