"""HTTP views and controls for the autoloop runtime."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from server import (
    audit as audit_log,
    autoloop,
    daily as daily_mod,
    futures_cycle_store,
    runs as runs_mod,
)
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.audit import append_event

router = APIRouter(prefix="/api", tags=["autoloop"])


_CYCLE_SUMMARY_BUCKETS = (
    "market_rejected", "confirmation_rejected", "forecast_deferred", "proposal_rejected", "entered",
    "awaiting_approval", "risk_rejected", "not_executed", "execution_failed",
    "in_flight", "interrupted", "other",
)


def _payload() -> dict:
    state = autoloop._load_state()
    mode_gates = autoloop.gates()
    return {
        "generated_at": utc_now_iso(),
        "enabled": bool(state["enabled"]) and mode_gates["any_auto_enabled"],
        "interval_minutes": autoloop._interval_from(state),
        "running_cycle": autoloop._cycle_lock.locked(),
        "modes": mode_gates,
        "gates": mode_gates,
        "last_cycle": autoloop._last_cycle_snapshot(),
        "cycles": futures_cycle_store.list_futures_cycles(limit=20),
    }


def _cycle_payload(cycle: dict) -> dict:
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    return {
        "cycle_id": cycle["cycle_id"],
        "symbol": cycle["symbol"],
        "four_hour_open": cycle["four_hour_open"],
        "one_hour_open": cycle["one_hour_open"],
        "strategy_version": cycle["strategy_version"],
        "status": cycle["status"],
        "phase": cycle["phase"],
        "payload": payload,
        "created_at": cycle["created_at"],
        "updated_at": cycle["updated_at"],
        "finished_at": cycle["finished_at"],
    }


@router.get("/autoloop/cycles")
def get_autoloop_cycles(limit: int = 20, symbol: str | None = None):
    if limit < 1 or limit > 100:
        raise ApiError(400, "validation_error", '"limit" must be between 1 and 100')
    if symbol is not None and not symbol.strip():
        raise ApiError(400, "validation_error", '"symbol" must be a non-empty string when provided')
    symbol_filter = (symbol.strip() or None) if symbol else None
    summary = futures_cycle_store.futures_cycle_summary()
    return {
        "generated_at": utc_now_iso(),
        "items": [
            _cycle_payload(item)
            for item in futures_cycle_store.list_futures_cycles(limit=limit, symbol=symbol_filter)
        ],
        "summary": {
            **dict.fromkeys(_CYCLE_SUMMARY_BUCKETS, 0),
            **summary,
            "total": sum(summary.values()),
        },
        "symbols": futures_cycle_store.futures_cycle_symbol_rollup(),
        "enabled": bool(autoloop._load_state().get("enabled", False)),
        "interval_minutes": autoloop._interval_from(autoloop._load_state()),
        "running_cycle": autoloop._cycle_lock.locked(),
        "modes": autoloop.gates(),
        "last_cycle": autoloop._last_cycle_snapshot(),
    }


@router.get("/autoloop")
def get_autoloop():
    return _payload()


@router.post("/autoloop")
async def set_autoloop(request: Request):
    body = await json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(400, "validation_error", '"enabled" must be a boolean')
    interval = body.get("interval_minutes", autoloop.DEFAULT_INTERVAL_MINUTES)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not autoloop.MIN_INTERVAL_MINUTES <= interval <= autoloop.MAX_INTERVAL_MINUTES
    ):
        raise ApiError(
            400, "validation_error",
            f'"interval_minutes" must be an integer between {autoloop.MIN_INTERVAL_MINUTES} and {autoloop.MAX_INTERVAL_MINUTES}',
        )
    with autoloop._state_lock:
        state = autoloop.gates()
        if enabled and not state["any_auto_enabled"]:
            raise ApiError(
                409, "mode_closed",
                "enabling the autoloop requires Auto Spot or Auto Futures to be enabled",
                {"gates": state},
            )
        enabled = state["any_auto_enabled"]
        autoloop._save_state({"enabled": enabled, "interval_minutes": interval, "updated_at": utc_now_iso()})
    audit_log.record("autoloop_updated", enabled=enabled, interval_minutes=interval)
    if enabled:
        autoloop.ensure_started()
    autoloop._wakeup.set()  # a changed interval or enabled flag takes effect on the next wake
    return _payload()


@router.post("/autoloop/run-now", status_code=202)
async def run_autoloop_now(request: Request):
    """Run one cycle now on a worker thread if at least one auto mode is on."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    if not autoloop.gates()["any_auto_enabled"]:
        raise ApiError(409, "mode_closed", "enable Auto Spot or Auto Futures before running a cycle")
    if autoloop._cycle_lock.locked():
        status = "already_running"
    else:
        busy = runs_mod.active_run_id() is not None or daily_mod.active_job_id() is not None
        status = "skipped_busy" if busy else "started"
        autoloop._spawn_cycle()
    return {"generated_at": utc_now_iso(), "status": status}


@router.post("/halt")
async def halt_now(request: Request):
    """The emergency STOP button: one halt line in the shared execution audit
    log and the RiskGuard of every process refuses the rest of today. Never
    gated — a kill switch must always work."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    # The timestamp is what makes the halt day-scoped (risk.py:111-119): a
    # halt line without one would be ignored by _halted_today.
    append_event(get_config()["exec_log_path"], {
        "event": "halt",
        "timestamp": datetime.now(UTC).isoformat(),
        "reason": "manual halt from the control panel",
    })
    audit_log.record("autoloop_halt")
    return {"generated_at": utc_now_iso(), "halted": True}


@router.post("/halt/reset")
async def reset_halt(request: Request):
    """Lift today's halt: a halt_reset line today ends the sticky halt
    (risk.py:163-173) until a new breach or a new halt line."""
    await json_body(request)  # CSRF §0: application/json required, body may be {}
    append_event(get_config()["exec_log_path"], {
        "event": "halt_reset",
        "timestamp": datetime.now(UTC).isoformat(),
    })
    audit_log.record("autoloop_halt_reset")
    return {"generated_at": utc_now_iso(), "halted": False}
