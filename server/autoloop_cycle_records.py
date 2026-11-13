"""Build durable Futures cycle rejection records."""

from __future__ import annotations

from datetime import UTC, datetime

from server import (
    autoloop_futures_context,
    futures_cycle_telemetry,
)
from tradingagents.execution.futures_scheduler import closed_candle_pair


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
    cycle_id = cycle_id or f"futures:{symbol}:{four_hour[0].isoformat()}:{one_hour[0].isoformat()}:1"
    payload = {
        "requested_ticker": ticker,
        "unified_symbol": unified_symbol,
        "market_rejection_reason": market_rejection_reason,
        "confirmation_result": None,
        "confirmation_reason": confirmation_reason,
        "confirmation": {"result": None, "reason": confirmation_reason},
        "signal": signal,
        "signal_reason": signal_reason,
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
        four_hour_open=(
            datetime.fromtimestamp(snapshot["four_hour_open"] / 1000, UTC).isoformat()
            if snapshot and isinstance(snapshot.get("four_hour_open"), (int, float))
            else four_hour[0].isoformat()
        ),
        one_hour_open=(
            datetime.fromtimestamp(snapshot["one_hour_open"] / 1000, UTC).isoformat()
            if snapshot and isinstance(snapshot.get("one_hour_open"), (int, float))
            else one_hour[0].isoformat()
        ),
        phase=phase,
        payload=payload,
        status=status,
    )
