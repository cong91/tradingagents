"""Durable journaling for futures entry submissions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from tradingagents.execution.futures_operation_lock import OperationGuard
from tradingagents.execution.sizing import PlannedOrder

_MANAGED_PROVENANCE_VERSION = 1


def managed_live_provenance(
    order: PlannedOrder,
    client_order_id: str,
    *,
    identity: Any,
    venue: str,
) -> dict[str, Any]:
    """Create the immutable marker for one explicitly managed live attempt."""
    return {
        "version": _MANAGED_PROVENANCE_VERSION,
        "kind": "managed_live_entry",
        "intent_id": f"entry-intent:{order.ccxt_symbol}:{client_order_id}",
        "client_order_id": client_order_id,
        "symbol": order.ccxt_symbol,
        "side": order.side,
        "venue_key": venue,
        "account_identity": {
            "exchange": identity.exchange,
            "profile": identity.profile,
            "type": identity.account_type,
            "hash": identity.identity_hash,
        },
    }


def record_entry_provenance(record: dict[str, Any]) -> dict[str, Any] | None:
    """Read a marker only when every persisted copy agrees exactly."""
    candidates: list[Any] = []
    direct = record.get("entry_provenance")
    if direct is not None:
        candidates.append(direct)
    legs = record.get("legs")
    if isinstance(legs, list):
        candidates.extend(
            leg.get("entry_provenance")
            for leg in legs
            if isinstance(leg, dict) and leg.get("entry_provenance") is not None
        )
    if not candidates or any(not isinstance(item, dict) for item in candidates):
        return None
    first = candidates[0]
    return first if all(item == first for item in candidates[1:]) else None


def valid_managed_live_provenance(marker: Any) -> bool:
    """Validate marker shape before any caller considers it for authorization."""
    if not isinstance(marker, dict):
        return False
    identity = marker.get("account_identity")
    return (
        marker.get("version") == _MANAGED_PROVENANCE_VERSION
        and marker.get("kind") == "managed_live_entry"
        and all(isinstance(marker.get(key), str) and marker.get(key) for key in (
            "intent_id", "client_order_id", "symbol", "side", "venue_key",
        ))
        and marker.get("side") in {"buy", "sell"}
        and isinstance(identity, dict)
        and all(isinstance(identity.get(key), str) and identity.get(key) for key in (
            "exchange", "profile", "type", "hash",
        ))
    )


_ACTIVE_STATUSES = (
    "pending", "monitoring", "submission_uncertain", "emergency_close_failed",
    "emergency_cleanup_pending", "replacement_cleanup_pending", "position_cleanup_pending",
    "time_stop_submission_uncertain", "time_stop_unknown_position", "time_stop_failed",
)
_ACTIVE_STATUS_SQL = ", ".join(f"'{status}'" for status in _ACTIVE_STATUSES)


def entry_journal_leg(
    order: PlannedOrder, client_order_id: str, *, quantity: str, created_at: str,
    identity: Any, venue: str, config: dict,
) -> dict:
    """Capture the exact entry terms and recovery policy before a venue mutation.

    The immutable ``managed_live_entry`` provenance marker rides inside the
    leg itself, so every journal row written through this builder carries the
    attribution evidence the outcome ledger needs — no caller can produce a
    marker-less managed entry by forgetting to attach it.
    """
    return {
        "name": "entry-intent",
        "status": "uncertain",
        "order_id": None,
        "client_order_id": client_order_id,
        "symbol": order.ccxt_symbol,
        "side": order.side,
        "type": "market",
        "quantity": quantity,
        "planned_quantity": float(order.quantity),
        "price": None,
        "params": {"clientOrderId": client_order_id},
        "original_stop_loss": order.stop_loss,
        "take_profit": order.take_profit,
        "created_at": created_at,
        "profile": identity.profile,
        "venue_key": venue,
        "entry_provenance": managed_live_provenance(
            order, client_order_id, identity=identity, venue=venue,
        ),
        "scale_out_policy": {
            "enabled": config.get("risk_derivatives_scale_out_enabled") is True,
            "first_r": config.get("risk_derivatives_scale_out_first_r", 1.0),
            "first_fraction": config.get("risk_derivatives_scale_out_first_fraction", 0.33),
            "second_r": config.get("risk_derivatives_scale_out_second_r", 2.0),
            "second_fraction": config.get("risk_derivatives_scale_out_second_fraction", 0.33),
        },
    }


def create_guarded_entry_intent(
    guard: OperationGuard, task_id: str, order: PlannedOrder,
    client_order_id: str, identity: Any, venue: str, leg: dict,
) -> None:
    """Check active symbol tasks and insert an uncertain intent atomically."""
    guard.check()
    with guard.connection.transaction():
        guard.check()
        existing = guard.connection.execute(
            f"""SELECT task_id FROM bracket_recovery_tasks
                WHERE symbol = %s AND task_id <> %s AND status IN ({_ACTIVE_STATUS_SQL})
                LIMIT 1""",
            (order.ccxt_symbol, task_id),
        ).fetchone()
        if existing:
            raise PermissionError("existing active task blocks entry")
        values = (
            task_id, order.ccxt_symbol, client_order_id,
            "sell" if order.side == "buy" else "buy", float(order.quantity),
            json.dumps([leg]), datetime.now(UTC).isoformat(), venue,
            identity.exchange, identity.profile, identity.account_type, identity.identity_hash,
        )
        inserted = guard.connection.execute(
            """INSERT INTO bracket_recovery_tasks
               (task_id, symbol, entry_order_id, close_side, quantity, legs, updated_at,
                venue_key, account_identity_exchange, account_identity_profile,
                account_identity_type, account_identity_hash, status)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s,
                       'submission_uncertain')
               ON CONFLICT (task_id) DO NOTHING""",
            values,
        )
        if inserted.rowcount != 1:
            raise RuntimeError("durable entry intent already exists")
        guard.check()


def update_guarded_entry_intent(
    guard: OperationGuard, task_id: str, *, status: str, leg: dict,
    order_id: str | None, error: str | None, quantity: float | None = None,
) -> None:
    """Update the entry journal on the same session that owns the symbol lock.

    ``quantity`` carries the exact venue-rounded size once market metadata is
    loaded; passing it rewrites the task's top-level quantity so the outcome
    ledger's planned-quantity comparison uses the same amount the venue
    received, not the pre-precision plan. Omitted updates leave it untouched.
    """
    guard.check()
    now = datetime.now(UTC).isoformat()
    with guard.connection.transaction():
        guard.check()
        cursor = guard.connection.execute(
            """UPDATE bracket_recovery_tasks
               SET status = %s, placed_ids = %s::jsonb, legs = %s::jsonb,
                   quantity = COALESCE(%s, quantity),
                   attempts = 0, last_error = %s, updated_at = %s
               WHERE task_id = %s AND symbol = %s
                 AND status IN ('submission_uncertain', 'pending')""",
            (status, json.dumps([str(order_id)] if order_id else []),
             json.dumps([leg]), quantity, error, now, task_id, guard.symbol),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("durable entry intent changed during guarded update")
        guard.check()


def active_entry_tasks(guard: OperationGuard, symbol: str) -> list[dict]:
    """Read active tasks using the owning advisory-lock session."""
    guard.check()
    rows = guard.connection.execute(
        f"""SELECT * FROM bracket_recovery_tasks
            WHERE symbol = %s AND status IN ({_ACTIVE_STATUS_SQL})""",
        (symbol,),
    ).fetchall()
    return [dict(row) for row in rows]
