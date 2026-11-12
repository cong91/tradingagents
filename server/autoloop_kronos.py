"""Prepare and record active Kronos forecasts for a futures cycle."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from server import autoloop_execution, autoloop_futures_context, kronos_context
from server.auto_modes import AutoMode
from server.autoloop_telemetry import CycleTelemetry
from server.contract import utc_now_iso
from tradingagents.execution.auto_authorization import AuthorizationSnapshot
from tradingagents.forecast.block import ForecastBlock


def _prepare_forecast(
    derivatives: Any,
    ticker: str,
    futures_context: dict,
    config: dict,
    execution_context: str,
    *,
    cycle_id: str | None,
    four_hour_open: str,
    one_hour_open: str,
    cycle_payload: dict,
    cycle_timestamps: dict[str, str],
    deadline: float | None,
    cancelled: threading.Event | None,
    telemetry: CycleTelemetry,
    mode: AutoMode | None,
    authorization: AuthorizationSnapshot | None,
    on_cycle_checkpoint: Callable[[dict], None] | None,
    persist_progress: Callable[..., None],
) -> tuple[str | None, str]:
    """Return a defer reason or the execution context with its forecast."""
    # Mode and budget are checked on both sides of inference. No execution lock
    # is held while the candidate waits for the forecast.
    autoloop_execution.raise_if_mode_disabled(mode, "kronos forecast", authorization)
    autoloop_execution.raise_if_execution_closed(deadline, cancelled, f"candidate {ticker} kronos forecast")
    cycle_payload["forecast_started"] = True
    cycle_timestamps["forecast_started_at"] = utc_now_iso()
    cycle_payload["phase_timestamps"] = dict(cycle_timestamps)
    if on_cycle_checkpoint is not None:
        on_cycle_checkpoint({
            "cycle_id": cycle_id,
            "symbol": futures_context["unified_symbol"],
            "four_hour_open": four_hour_open,
            "one_hour_open": one_hour_open,
            "payload": dict(cycle_payload),
        })
    with telemetry.span("kronos_active_context", ticker):
        kronos_block = autoloop_futures_context._kronos_block_for(
            derivatives, ticker, futures_context, config,
            cycle_id=cycle_id or f"{ticker}:{four_hour_open}",
            deadline=deadline, cancelled=cancelled,
        )
    autoloop_execution.raise_if_mode_disabled(mode, "kronos forecast", authorization)
    autoloop_execution.raise_if_execution_closed(deadline, cancelled, f"candidate {ticker} kronos forecast")
    if kronos_block is None:
        kronos_block = ForecastBlock(
            symbol=str(futures_context.get("unified_symbol") or ticker),
            status="unavailable",
            reason="the Kronos forecast path returned no block",
        )
    kronos_policy = kronos_context.policy_for(kronos_block)
    cycle_payload["kronos_forecast"] = kronos_block.as_json()
    cycle_payload["kronos_policy"] = kronos_policy
    cycle_timestamps["kronos_finished_at"] = utc_now_iso()
    cycle_payload["phase_timestamps"] = dict(cycle_timestamps)
    if on_cycle_checkpoint is not None:
        on_cycle_checkpoint({
            "cycle_id": cycle_id,
            "symbol": futures_context["unified_symbol"],
            "four_hour_open": four_hour_open,
            "one_hour_open": one_hour_open,
            "payload": dict(cycle_payload),
        })
    if cycle_id is not None and autoloop_execution.cycle_writes_allowed(deadline, cancelled):
        persist_progress(
            cycle_id, phase="context", payload=cycle_payload,
            timestamp=cycle_timestamps["kronos_finished_at"],
        )
    if kronos_policy == "defer":
        defer_reason = (
            f"kronos forecast deferred: status={kronos_block.status} "
            f"reason={kronos_block.reason}" if kronos_block is not None
            else "kronos forecast deferred: no forecast was produced"
        )
        if cycle_id is not None and autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            persist_progress(
                cycle_id,
                status="rejected",
                phase="forecast",
                payload={
                    **cycle_payload,
                    "execution_status": "not_started",
                    "kronos_defer_reason": defer_reason,
                    "executed": False,
                    "phase_timestamps": cycle_timestamps,
                },
                terminal=True,
            )
        return defer_reason, execution_context
    return None, kronos_context.append_to_context(execution_context, kronos_block)
