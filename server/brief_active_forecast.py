"""Read-only active forecast projection for the operating brief."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from server import db

logger = logging.getLogger("server.operating_brief")


def _quote_currency_of(symbol: str | None) -> str | None:
    """The quote currency of a ccxt symbol: ``BTC/USDT:USDT`` → ``USDT``.

    The price band's numbers are in this currency; deriving it server-side keeps
    the UI from re-parsing venue symbol formats.
    """
    if not isinstance(symbol, str):
        return None
    head = symbol.split(":")[0]
    parts = head.split("/")
    return parts[1] if len(parts) == 2 and parts[1] else None


def _forecast_is_expired(block: dict[str, Any]) -> bool:
    """True when the horizon window has fully elapsed.

    A 24h forecast drawn at ``as_of`` stops being a forecast once every bar it
    predicted has closed — reporting it as current then would present history
    as a live model sample. Unparseable/missing bounds never mark it stale.
    """
    as_of = block.get("as_of")
    horizon = block.get("horizon_hours")
    try:
        as_of_dt = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        hours = float(horizon)
    except (TypeError, ValueError):
        return False
    if as_of_dt.tzinfo is None:
        as_of_dt = as_of_dt.replace(tzinfo=UTC)
    return datetime.now(UTC) >= as_of_dt + timedelta(hours=hours)


_ACTIVE_FORECAST_NOTE = (
    "Kronos forecast of the most recent recorded cycle: a model sample ratio "
    "over paths, not a probability, and context for the agents — never an "
    "order, a veto or a profit claim."
)


def _active_forecast_section() -> dict[str, Any]:
    """Return the forecast recorded by the latest Futures decision cycle."""

    try:
        with db.read() as conn:
            row = conn.execute(
                """SELECT cycle_id, symbol, updated_at, payload
                   FROM futures_cycles
                   WHERE payload ? 'kronos_forecast'
                   ORDER BY updated_at DESC
                   LIMIT 1""",
            ).fetchone()
    except Exception as exc:
        logger.warning("brief: active forecast unreadable: %r", exc)
        return {"state": "error", "status": "error",
                "reason": f"the recorded forecasts could not be read: {type(exc).__name__}",
                "note": _ACTIVE_FORECAST_NOTE}
    if row is None:
        return {"state": "empty", "status": "unavailable",
                "reason": "no recorded cycle has a Kronos forecast yet; nothing is claimed",
                "note": _ACTIVE_FORECAST_NOTE}

    payload = row.get("payload") or {}
    block = payload.get("kronos_forecast")
    if not isinstance(block, dict):
        return {"state": "error", "status": "error",
                "reason": "the recorded forecast block is malformed",
                "note": _ACTIVE_FORECAST_NOTE}
    state = "ok"
    if block.get("status") != "ok":
        state = "unavailable"
    elif _forecast_is_expired(block):
        state = "stale"
    return {
        "state": state,
        "status": block.get("status") if state != "stale" else "stale",
        "cycle_id": row.get("cycle_id"),
        "symbol": row.get("symbol"),
        "quote_currency": _quote_currency_of(row.get("symbol")),
        "policy": payload.get("kronos_policy"),
        "defer_reason": payload.get("kronos_defer_reason"),
        "recorded_at": row.get("updated_at"),
        "forecast": block,
        "note": _ACTIVE_FORECAST_NOTE,
    }
