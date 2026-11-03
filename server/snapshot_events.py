"""SSE transport for UI snapshot invalidation hints.

Events are hints, not data: the client reacts by re-fetching the affected
snapshot. Connections start with a reset hint because event IDs are local to
one process epoch; clients must re-fetch and keep polling as a fallback.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from server import snapshot_coordinator

router = APIRouter(prefix="/api/snapshot", tags=["snapshot"])


def _stream() -> Iterator[str]:
    # Event ids are process-local; restart each connection with a reset and
    # follow events created after it. Polling covers disconnected intervals.
    last_id = snapshot_coordinator.latest_event_id()
    yield 'event: reset\ndata: {"context_id":null}\n\n'
    while True:
        events, missed = snapshot_coordinator.events_after(last_id)
        if missed:
            event = events[-1]
            last_id = int(event["id"])
            yield (
                f"id: {last_id}\nevent: reset\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )
            continue
        if not events:
            yield ": keep-alive\n\n"
            continue
        for event in events:
            last_id = int(event["id"])
            yield (
                f"id: {last_id}\nevent: {event['type']}\n"
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            )


@router.get("/events")
def snapshot_events():
    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
