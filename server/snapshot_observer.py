"""Best-effort execution notifications for display-only invalidation.

Every helper is exception-safe and does no network, database or blocking
work: ``publish_mutation`` uses a try-lock and marks an overflow on contention
or failure, so observation does not change the venue result or exception.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

from server import snapshot_coordinator

_Result = TypeVar("_Result")


def before(
    bridge: Any,
    client: Any,
    *,
    operation: str,
    symbol: str | None,
) -> str | None:
    """Mark the credential context dirty before a venue mutation."""
    try:
        return snapshot_coordinator.publish_mutation(
            bridge,
            client,
            operation=operation,
            symbol=symbol,
            phase="pending",
        )
    except Exception:
        return None


def after(
    bridge: Any,
    client: Any,
    *,
    operation: str,
    symbol: str | None,
    token: str | None,
) -> None:
    """Publish an outcome marker; it is not evidence of a fill."""
    with contextlib.suppress(Exception):
        snapshot_coordinator.publish_mutation(
            bridge,
            client,
            operation=operation,
            symbol=symbol,
            phase="outcome",
            token=token,
        )


def run(
    bridge: Any,
    client: Any,
    *,
    operation: str,
    symbol: str | None,
    call: Callable[[], _Result],
) -> _Result:
    """Run one existing venue operation with exception-safe UI observation."""
    token = before(bridge, client, operation=operation, symbol=symbol)
    try:
        return call()
    finally:
        after(bridge, client, operation=operation, symbol=symbol, token=token)
