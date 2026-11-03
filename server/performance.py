"""Expose execution audit events without treating estimates as verified fills."""

from __future__ import annotations

import math

from fastapi import APIRouter

from server import audit as audit_log
from server.contract import utc_now_iso

router = APIRouter(prefix="/api", tags=["performance"])

RECENT_ORDERS_LIMIT = 50


def _number(value):
    """Keep finite audit numbers; malformed values are unavailable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value):
    """Do not expose non-string audit metadata as display text."""
    return value if isinstance(value, str) else None


def _bracket_counts(events: list[dict]) -> dict:
    """Historical audit event counts, not current venue protection."""
    return {
        "placed": sum(1 for event in events if event.get("action") == "bracket_placed"),
        "failed": sum(1 for event in events if event.get("action") == "bracket_failed"),
    }


def _recent_orders(events: list[dict]) -> list[dict]:
    """Newest execute-phase audit events, including failures and rejections."""
    execute_lines = [
        event for event in events if event.get("phase") == "execute" and event.get("action")
    ]
    return [{
        "timestamp": _text(event.get("timestamp")),
        "ticker": _text(event.get("ticker")),
        "ccxt_symbol": _text(event.get("ccxt_symbol")),
        "action": _text(event.get("action")),
        "qty": _number(event.get("qty")),
        "price_est": _number(event.get("price_est")),
        "market": _text(event.get("market")) or "spot",
        "leverage": _number(event.get("leverage")),
        "cost": _number(event.get("cost")),
        "mode": _text(event.get("mode")),
        "order_id": _text(event.get("order_id")),
    } for event in reversed(execute_lines[-RECENT_ORDERS_LIMIT:])]


def compute_performance(events: list[dict]) -> dict:
    """Return audit events; neither holdings nor results can be verified from them."""
    return {
        "summary": None,
        "open_positions": None,
        "brackets": _bracket_counts(events),
        "recent_orders": _recent_orders(events),
    }


@router.get("/performance")
def get_performance():
    events, skipped = audit_log.read_events()
    return {"generated_at": utc_now_iso(), "skipped_lines": skipped, **compute_performance(events)}
