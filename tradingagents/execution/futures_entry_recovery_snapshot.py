"""Bounded, read-only PostgreSQL snapshot for futures entry diagnosis."""

from __future__ import annotations

from typing import Any

from server import db
from tradingagents.execution.futures_entry_journal import _ACTIVE_STATUSES

_MAX_SNAPSHOT_ROWS = 1000


def _read_snapshot(symbol: str, entry_order_id: str, intent_id: str) -> list[dict[str, Any]]:
    """Read every potentially relevant task in a single statement snapshot.

    Active tasks are included across symbols because raw or malformed market
    identities cannot safely be excluded in SQL. Overflow fails closed. The
    snapshot can become stale immediately and does not authorize order creation.
    """
    canonical_id = f"bracket:{symbol}:{entry_order_id}"
    with db.read() as conn, conn.cursor() as cursor:
        cursor.execute(
            """SELECT * FROM bracket_recovery_tasks
               WHERE task_id = %s OR task_id = %s
                  OR (symbol = %s AND entry_order_id = %s)
                  OR status = ANY(%s)
               LIMIT %s""",
            (intent_id, canonical_id, symbol, entry_order_id,
             list(_ACTIVE_STATUSES), _MAX_SNAPSHOT_ROWS + 1),
        )
        rows = cursor.fetchall()
    if len(rows) > _MAX_SNAPSHOT_ROWS:
        raise RuntimeError("entry recovery snapshot exceeded row limit")
    return [dict(row) for row in rows]
