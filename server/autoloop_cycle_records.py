"""Build durable Futures cycle rejection records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server import (
    autoloop_futures_context,
    autoloop_schedule,
    futures_cycle_telemetry,
)
from server.auto_modes import AutoMode
from tradingagents.auto_timeframes import last_closed_candle
from tradingagents.dataflows.config import get_config
from tradingagents.execution.futures_scheduler import closed_candle_pair


def _preselection_identity(now: datetime) -> dict[str, Any]:
    """Selected-candle identity for a rejection raised BEFORE selection ran.

    Market/preflight/context failures happen before the venue candle was read,
    so there is no verified selected candle. The identity is derived from the
    wall clock and the mode's configured timeframe — exactly the candle a
    later claim on this boundary would select — keeping early rejections
    per-candle instead of collapsing a whole 1m/15m window into one row.
    """
    timeframe = autoloop_schedule.selected_timeframe(get_config(), AutoMode.FUTURES)
    opened, closed = last_closed_candle(now, timeframe)
    return {
        "timeframe": timeframe,
        "open_time": int(opened.timestamp() * 1000),
        "close_time": int(closed.timestamp() * 1000),
    }


def _record_futures_cycle_rejection(
    ticker: str,
    *,
    unified_symbol: str | None = None,
    market_rejection_reason: str | None = None,
    confirmation_reason: str | None = None,
    snapshot: dict | None = None,
    signal: str | None = None,
    signal_reason: str | None = None,
    phase: str = "market",
    status: str = "rejected",
    agent_note: str | None = None,
    cycle_id: str | None = None,
    telemetry: dict | None = None,
    phase_timestamps: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    one_hour, four_hour = closed_candle_pair(now)
    symbol = unified_symbol or ticker
    snapshot_identity = (
        snapshot.get("candle_identity")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("candle_identity"), dict)
        else None
    )
    if snapshot_identity is not None:
        # Snapshot-bearing context: the id and the stored opens must come from
        # the same recorded candles, or the canonical-id CHECK would reject a
        # row whose id was built at a different boundary than its columns.
        four_open_ms = snapshot.get("four_hour_open")
        one_open_ms = snapshot.get("one_hour_open")
        four_open = (
            datetime.fromtimestamp(four_open_ms / 1000, UTC).isoformat()
            if isinstance(four_open_ms, (int, float)) and not isinstance(four_open_ms, bool)
            else four_hour[0].isoformat()
        )
        one_open = (
            datetime.fromtimestamp(one_open_ms / 1000, UTC).isoformat()
            if isinstance(one_open_ms, (int, float)) and not isinstance(one_open_ms, bool)
            else one_hour[0].isoformat()
        )
        identity = dict(snapshot_identity)
    else:
        # Pre-selection failure: no verified candle exists, so the identity
        # and the stored opens all come from the current wall-clock boundary —
        # exactly what a later claim on this boundary would select.
        four_open, one_open = four_hour[0].isoformat(), one_hour[0].isoformat()
        identity = _preselection_identity(now)
    cycle_id = cycle_id or futures_cycle_telemetry.canonical_cycle_id(
        symbol=symbol,
        four_hour_open=four_open,
        one_hour_open=one_open,
        timeframe=str(identity["timeframe"]),
        open_time=int(identity["open_time"]),
    )
    payload = {
        "requested_ticker": ticker,
        "unified_symbol": unified_symbol,
        "market_rejection_reason": market_rejection_reason,
        "confirmation_result": None,
        "confirmation_reason": confirmation_reason,
        "confirmation": {"result": None, "reason": confirmation_reason},
        "signal": signal,
        "signal_reason": signal_reason,
        "candle_identity": identity,
    }
    if telemetry:
        payload.update(telemetry)
    if phase_timestamps:
        payload["phase_timestamps"] = dict(phase_timestamps)
    if snapshot and signal:
        confirmed, derived_reason = autoloop_futures_context._confirmation_state(snapshot, signal)
        payload["confirmation_result"] = confirmed
        payload["confirmation_reason"] = confirmation_reason or derived_reason
        payload["confirmation"] = {"result": confirmed, "reason": payload["confirmation_reason"]}
    if agent_note:
        payload["agent_decision_note"] = futures_cycle_telemetry.redact_payload(agent_note[:6000])

    if snapshot:
        payload["snapshot"] = futures_cycle_telemetry.snapshot_payload(snapshot)
    futures_cycle_telemetry.persist_rejection(
        cycle_id,
        symbol=symbol,
        four_hour_open=four_open,
        one_hour_open=one_open,
        timeframe=str(identity["timeframe"]),
        selected_open=int(identity["open_time"]),
        phase=phase,
        payload=payload,
        status=status,
    )
