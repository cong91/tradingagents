"""Bounded, read-only polling for recorded forward Kronos forecasts."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from server import (
    forecast_evaluation_scoring as scoring,
    forecast_evaluation_store,
    forecast_evaluation_venue as venue,
)
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.market_data import fetch_closed_candles

logger = logging.getLogger(__name__)
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_BATCH_SIZE = 25
_STOP_TIMEOUT_SECONDS = 15.0
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_poll_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
# Transitions may wait for polls, but polls never acquire the transition lock.
# Never hold the lifecycle lock while waiting for a poll or worker to finish.
_transition_lock = threading.Lock()
_runtime_enabled: bool | None = None


# These wrappers keep the historical test seams and public module surface stable
# while the scoring and venue responsibilities live in dedicated modules.
def _digest(value: Any) -> str:
    return scoring.digest(value)


def _evaluate_block(
    cycle: dict[str, Any], block: dict[str, Any], *, exchange: Any | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return scoring.evaluate(
        cycle, block, exchange=exchange, now=now, candle_fetcher=fetch_closed_candles,
    )


def _public_futures_exchange(scope: dict[str, Any]) -> Any:
    return venue.public_futures_exchange(scope)


def _close_exchanges(exchanges: dict[str, Any]) -> None:
    for exchange in exchanges.values():
        venue.close_client(exchange)


def _enabled() -> bool:
    configured = get_config().get("forecast_evaluation_enabled") is True
    with _lifecycle_lock:
        override = _runtime_enabled
    return configured and override is not False


def configure(*, enabled: bool, reset_cursor: bool = False) -> None:
    """Fence reconfiguration against in-flight work and reset cutover safely."""
    global _runtime_enabled, _stop_event, _worker_thread
    with _transition_lock:
        if not enabled:
            with _lifecycle_lock:
                _runtime_enabled = False
                _stop_event.set()
        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        if not _poll_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TimeoutError("forecast evaluation poll did not stop in time")
        _poll_lock.release()
        worker = _worker_thread
        if _stop_event.is_set() and worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                raise TimeoutError("forecast evaluation worker did not stop in time")
            _worker_thread = None
        if enabled:
            if not _poll_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise TimeoutError("forecast evaluation poll did not stop in time")
            try:
                if reset_cursor:
                    forecast_evaluation_store.initialize_cursor(reset=True)
                with _lifecycle_lock:
                    if _stop_event.is_set():
                        _stop_event = threading.Event()
                    _runtime_enabled = True
            finally:
                _poll_lock.release()


def _score_cycle(
    cycle: dict[str, Any], block: dict[str, Any], *, exchange: Any | None,
) -> dict[str, Any]:
    """Score a snapshot and attach the immutable evidence used by retries."""
    row = _evaluate_block(cycle, block, exchange=exchange)
    row["forecast_snapshot"] = block
    return row


def _row_from_cycle(
    cycle: dict[str, Any], *, exchange_factory: Callable[[dict[str, Any]], Any] | None,
    exchanges: dict[str, Any], exchange_errors: dict[str, str],
) -> dict[str, Any]:
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    snapshot = cycle.get("forecast_snapshot")
    if snapshot is None:
        snapshot = payload.get("kronos_forecast")
    if not isinstance(snapshot, dict):
        return scoring.malformed(cycle, "forecast evidence is missing or not an object")

    score_payload = dict(payload)
    score_payload["kronos_forecast"] = snapshot
    if isinstance(cycle.get("venue_scope"), dict):
        score_payload["kronos_venue_scope"] = cycle["venue_scope"]
    score_cycle = {**cycle, "payload": score_payload}
    venue_scope = cycle.get("venue_scope") or score_payload.get("kronos_venue_scope")
    if not isinstance(venue_scope, dict) or not venue_scope:
        venue_scope = {}

    # Preflight without a client avoids creating a network client for invalid or
    # not-yet-due forecasts. A mature, valid forward forecast then gets one
    # credential-free client for the exact venue scope.
    preliminary = _score_cycle(score_cycle, snapshot, exchange=None)
    if preliminary["status"] in {"invalid", "pending"} or snapshot.get("status") != "ok":
        return preliminary
    exchange = None
    venue_error: str | None = None
    if callable(exchange_factory):
        try:
            exchange = exchange_factory(score_cycle)
        except Exception as exc:
            venue_error = f"read-only venue factory failed: {type(exc).__name__}"
    elif venue_scope:
        key = json.dumps(venue_scope, sort_keys=True, separators=(",", ":"))
        if key not in exchanges and key not in exchange_errors:
            try:
                exchanges[key] = _public_futures_exchange(venue_scope)
            except Exception as exc:
                exchange_errors[key] = type(exc).__name__
        exchange = exchanges.get(key)
        if exchange is None:
            venue_error = f"read-only venue unavailable: {exchange_errors.get(key, 'unknown')}"
    if venue_error is not None:
        return scoring.unavailable(preliminary, venue_error)
    if exchange is None:
        return scoring.unavailable(preliminary, "recorded Futures venue scope is missing")
    return _score_cycle(score_cycle, snapshot, exchange=exchange)


def poll_once(
    *, exchange_factory: Callable[[dict[str, Any]], Any] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    if not _enabled():
        return {"status": "disabled"}
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not _poll_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        if not _enabled() or _stop_event.is_set():
            return {"status": "disabled"}
        cursor = forecast_evaluation_store.get_cursor()
        if cursor is None:
            forecast_evaluation_store.initialize_cursor()
            return {"status": "initialized", "note": "evaluation starts now; no backfill"}

        cycles = forecast_evaluation_store.terminal_forecast_cycles_after(cursor, batch_size=batch_size)
        due_rows = forecast_evaluation_store.pending_evaluations_due(limit=max(1, batch_size // 2))
        exchanges: dict[str, Any] = {}
        exchange_errors: dict[str, str] = {}
        processed: list[str] = []
        cursor_blocked = False
        try:
            seen: set[tuple[str, str]] = set()
            for cycle in [*due_rows, *cycles]:
                if _stop_event.is_set() or not _enabled():
                    cursor_blocked = True
                    break
                cycle_id = str(cycle.get("cycle_id") or "")
                snapshot = cycle.get("forecast_snapshot")
                payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
                if snapshot is None:
                    snapshot = payload.get("kronos_forecast")
                key = (_digest(snapshot), cycle_id)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    row = _row_from_cycle(
                        cycle, exchange_factory=exchange_factory,
                        exchanges=exchanges, exchange_errors=exchange_errors,
                    )
                    forecast_evaluation_store.upsert_evaluation(row, expected_cursor=cursor)
                except forecast_evaluation_store.CursorLostError:
                    cursor_blocked = True
                    break
                except Exception:
                    logger.exception("forecast evaluation row failed for %s", cycle_id)
                    cursor_blocked = True
                    break
                processed.append(cycle_id)
        finally:
            _close_exchanges(exchanges)

        if cycles and not cursor_blocked and not _stop_event.is_set() and _enabled():
            for last in reversed(cycles):
                if forecast_evaluation_store.advance_cursor(
                    cursor, last["updated_at"], last["cycle_id"],
                ):
                    break
        return {
            "status": "polled",
            "cycles": len(cycles),
            "due_retries": len(due_rows),
            "processed": processed,
            "cursor_blocked": cursor_blocked,
        }
    finally:
        _poll_lock.release()


def interval_seconds(config: dict[str, Any] | None = None) -> float:
    value = (config or get_config()).get("forecast_evaluation_interval_seconds")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return number if math.isfinite(number) and number > 0 else DEFAULT_INTERVAL_SECONDS


def start() -> None:
    global _worker_thread
    with _transition_lock:
        if not _enabled():
            logger.info("forecast evaluation is disabled; worker not started")
            return
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        with _poll_lock:
            if forecast_evaluation_store.get_cursor() is None:
                forecast_evaluation_store.initialize_cursor()

        stop_event = _stop_event

        def loop() -> None:
            while not stop_event.is_set():
                try:
                    poll_once()
                except Exception:
                    logger.exception("forecast evaluation poll failed")
                if stop_event.wait(interval_seconds()):
                    break

        _worker_thread = threading.Thread(
            target=loop, name="server-forecast-evaluation", daemon=True,
        )
        _worker_thread.start()


def start_at_boot() -> None:
    """Start a fresh collection epoch after startup, conservatively excluding downtime."""
    if get_config().get("forecast_evaluation_enabled") is not True:
        return
    configure(enabled=True, reset_cursor=True)
    start()


def pause() -> None:
    """Fence and wait for an in-flight poll before disabling future polls."""
    configure(enabled=False)
    logger.info("forecast evaluation paused; no future poll will run")


def stop() -> None:
    pause()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "_digest",
    "_evaluate_block",
    "_public_futures_exchange",
    "configure",
    "interval_seconds",
    "pause",
    "poll_once",
    "start",
    "stop",
]
