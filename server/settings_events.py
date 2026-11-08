"""SSE transport for UI settings invalidation hints.

Effective exec mode changes only through deliberate mutations: an operator
config PUT (``server/operator_config.py``) or an env edit plus a restart.
Instead of the client polling ``GET /api/settings`` on a timer, the server
publishes a change hint here and every connected tab refetches once. A
restart drops the stream, so reconnects start with a ``reset`` event that
triggers the same refetch — env halves are covered without any polling.

Events are hints, not data: the client reacts by re-fetching the settings.
Blocking waits use the same ``threading.Condition`` pattern as the snapshot
invalidation channel (``server/snapshot_coordinator.py``).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

_LOCK = threading.Lock()
_EVENTS_CHANGED = threading.Condition(_LOCK)
_EVENTS: deque[dict] = deque(maxlen=64)
_SEQUENCE = 0


def publish_change(reason: str) -> None:
    """Record one change hint and wake every connected stream."""
    global _SEQUENCE
    with _EVENTS_CHANGED:
        _SEQUENCE += 1
        _EVENTS.append({"id": _SEQUENCE, "type": "changed", "reason": reason})
        _EVENTS_CHANGED.notify_all()


def latest_event_id() -> int:
    with _EVENTS_CHANGED:
        return _SEQUENCE


def events_after(last_id: int, wait_seconds: float = 15.0) -> list[dict]:
    """Buffered change hints after ``last_id``; blocks up to ``wait_seconds``."""
    with _EVENTS_CHANGED:
        if not _EVENTS or _EVENTS[-1]["id"] <= last_id:
            _EVENTS_CHANGED.wait(timeout=wait_seconds)
        return [event for event in _EVENTS if event["id"] > last_id]


def _stream() -> Iterator[str]:
    last_id = latest_event_id()
    # Connect (and every reconnect — event ids are process-local) starts with
    # a reset hint so the client refetches once and holds no stale mode.
    yield 'event: reset\ndata: {"reason":"connect"}\n\n'
    while True:
        events = events_after(last_id)
        for event in events:
            last_id = int(event["id"])
            yield (
                f"id: {last_id}\nevent: {event['type']}\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )
        if not events:
            yield ": keep-alive\n\n"


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/events")
def settings_events():
    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
