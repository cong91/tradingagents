"""Operator-authorized takeover of legacy futures execution records.

Binding a legacy position state and its bracket task to the currently
configured account is a persistence operation only: this module never sends
``create_order``, ``cancel_order`` or any close to the venue. The evidence
chain before any bind is (1) a signed account-identity read, (2) one fresh
raw position read whose scope is complete, and (3) per-leg venue verification
of every live protective leg the monitor will manage. Unbound rows stay
fail-closed: any missing, ambiguous or unreadable evidence refuses the bind
and leaves both rows untouched.
"""

from __future__ import annotations

import contextlib
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server import job_store
from tradingagents.execution import bracket_legs, futures_identity
from tradingagents.execution.audit import append_event
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.futures_account import observe_futures_positions

# DB statuses of legs the monitor still manages (pending legs without ids are
# freshly placed after binding; terminal legs need no venue verification).
_LIVE_LEG_STATUSES = {"placed", "uncertain"}


def take_over_legacy_positions(bridge: Any, *, allow_mutation: bool, config: dict) -> list[dict]:
    """Bind legacy rows to the current account; one report dict per active state.

    The identity and venue position reads happen once per run. With
    ``allow_mutation=False`` no venue call is made at all.
    """
    states = job_store.active_position_states()
    if not states:
        return []
    pending = [state for state in states if not state.get("account_identity_hash")]
    if not pending:
        return [_refuse(state, "already_bound") for state in states]
    if not allow_mutation:
        return [_refuse(state) for state in states]
    identity = _read_identity(bridge)
    if identity is None:
        return [_refuse(state, "identity_unavailable") for state in states]
    try:
        observation = observe_futures_positions(bridge)
    except Exception:
        # A read that cannot complete is not a flat book: refuse, write nothing.
        return [_refuse(state, "venue_read_failed") for state in states]
    if observation.unknown_scope or observation.unknown_symbols:
        return [_refuse(state, "scope_incomplete") for state in states]
    outcomes = {
        state["position_key"]: _take_over_state(bridge, state, observation, identity, config)
        for state in pending
    }
    return [
        _refuse(state) if state.get("account_identity_hash")
        else outcomes[state["position_key"]]
        for state in states
    ]


def _refuse(state: dict, status: str = "mutation_gated") -> dict:
    """One report per state with no venue reads: bound rows stay already_bound."""
    if state.get("account_identity_hash"):
        return {"position_key": state["position_key"], "status": "already_bound"}
    return {"position_key": state["position_key"], "status": status}


def _read_identity(bridge: Any) -> Any | None:
    """Module-ref read so the identity seam stays single-source; failures refuse."""
    try:
        return futures_identity.read_futures_account_identity(bridge)
    except Exception:
        return None


def _take_over_state(bridge: Any, state: dict, observation, identity, config: dict) -> dict:
    position_key = state["position_key"]
    try:
        return _bind_state(bridge, state, observation, identity, config)
    except Exception as exc:
        # One malformed legacy record must not abort the takeover of the rest.
        return {"position_key": position_key, "status": f"error:{type(exc).__name__}"}


def _bind_state(bridge: Any, state: dict, observation, identity, config: dict) -> dict:
    position_key = state["position_key"]
    symbol, side_word, _order_id = position_key.rsplit(":", 2)
    side = "long" if side_word == "buy" else "short"
    candidates = [
        position for position in observation.positions
        if position.symbol == symbol and position.side == side
    ]
    if len(candidates) != 1:
        return {"position_key": position_key, "status": "position_unmatched"}
    venue = candidates[0]
    if not _anchors_match(state.get("snapshot") or {}, venue):
        return {"position_key": position_key, "status": "position_unmatched"}
    task_id = state.get("bracket_task_id")
    task = job_store.load_bracket_recovery(task_id) if task_id else None
    if not task_id or task is None:
        return {"position_key": position_key, "status": "missing_bracket_task"}
    if task.get("venue_key") and task["venue_key"] != venue_key(bridge):
        return {"position_key": position_key, "status": "venue_mismatch"}
    verified, leg_state = _verify_legs(bridge, task)
    if verified is None:
        report = {"position_key": position_key, "status": "leg_unverified"}
        if leg_state is not None:
            report["leg_state"] = leg_state
        return report
    if job_store.bind_bracket_recovery_identity(task_id, identity=identity) not in {
        "bound", "already_bound_same",
    }:
        return {"position_key": position_key, "status": "bind_refused"}
    if not job_store.save_position_state(
        position_key, state["revision"], _without_identity_keys(state), identity=identity,
    ):
        return {"position_key": position_key, "status": "bind_refused"}
    _audit_bind(config, symbol=symbol, task_id=task_id, legs_verified=verified)
    return {
        "position_key": position_key, "status": "bound", "symbol": symbol,
        "side": side, "contracts": venue.contracts, "entry": venue.entry_price,
        "legs_verified": verified, "task_id": task_id,
    }


def _anchors_match(snapshot: dict, venue: Any) -> bool:
    """Entry price must match; the venue size may only be smaller (partials)."""
    entry = _finite(snapshot.get("entry"))
    venue_entry = _finite(venue.entry_price)
    if entry is None or venue_entry is None or not math.isclose(
        entry, venue_entry, rel_tol=1e-6, abs_tol=1e-9,
    ):
        return False
    quantity = _finite(snapshot.get("quantity"))
    contracts = _finite(venue.contracts)
    return (
        quantity is not None and contracts is not None
        and contracts <= quantity * (1 + 1e-9)
    )


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _verify_legs(bridge: Any, task: dict) -> tuple[int | None, str | None]:
    """Fetch and match every live leg; ``(count, None)`` or ``(None, leg state)``.

    A leg that matches but is no longer ``placed`` (triggered, finished or
    cancelled between read and check) refuses the bind: the monitor would
    manage a ghost order. A leg whose persisted trigger/quantity differ from
    the venue ONLY by the exchange's own tick/step rounding is reconciled to
    the venue values before binding: the persisted plan stays bookkeeping,
    the signed venue read is the truth.
    """
    exchange = bridge.client()
    is_binance = bridge.exchange_id == "binance"
    params = {"trigger": True} if is_binance else {}
    verified = 0
    reconciled = False
    for leg in task["legs"]:
        if not leg.get("order_id") or leg.get("status") not in _LIVE_LEG_STATUSES:
            continue
        try:
            item = exchange.fetch_order(str(leg["order_id"]), task["symbol"], params=params)
        except Exception:
            return None, None
        if not bracket_legs.matches(item, task, leg, require_mark_price=is_binance):
            if not _reconcile_leg_to_venue(exchange, task, leg, item):
                return None, None
            reconciled = True
        state_name = bracket_legs.state(item)
        if state_name != "placed":
            return None, state_name
        verified += 1
    if reconciled and verified:
        job_store.update_bracket_recovery(
            task["task_id"], status=task["status"], placed_ids=[
                str(leg["order_id"]) for leg in task["legs"] if leg.get("order_id")
            ], attempts=task.get("attempts", 0), last_error=None,
            now=datetime.now(UTC).isoformat(), legs=task["legs"],
        )
    return verified, None


def _reconcile_leg_to_venue(exchange: Any, task: dict, leg: dict, item: Any) -> bool:
    """Accept a leg when ccxt's own precision functions explain the difference.

    ``bracket_legs.matches`` compares the persisted planned values verbatim,
    but the venue acknowledges tick-rounded triggers and step-rounded
    quantities. The rounded values are computed by ccxt itself — nothing here
    invents a tolerance — and the leg record is rewritten to the venue's
    acknowledged values so the monitor can match it afterwards.
    """
    raw_info = item.get("info") if isinstance(item, dict) else {}
    if not isinstance(raw_info, dict):
        return False
    param = "stopLossPrice" if leg["name"] == "stop-loss" else "takeProfitPrice"
    planned_trigger = leg.get("params", {}).get(param)
    planned_quantity = leg.get("quantity", task.get("quantity"))
    if planned_trigger is None or planned_quantity is None:
        return False
    venue_trigger = bracket_legs.info(item).get("triggerPrice")
    venue_quantity = bracket_legs.info(item).get("quantity")
    try:
        if not _same_number(venue_trigger, exchange.price_to_precision(task["symbol"], planned_trigger)):
            return False
        if not _same_number(venue_quantity, exchange.amount_to_precision(task["symbol"], planned_quantity)):
            return False
    except Exception:
        return False
    leg["params"][param] = float(venue_trigger)
    leg["quantity"] = float(venue_quantity)
    return True


def _same_number(left: Any, right: Any) -> bool:
    try:
        number_left, number_right = float(left), float(right)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number_left) and math.isfinite(number_right) and math.isclose(
        number_left, number_right, rel_tol=1e-9, abs_tol=1e-12,
    )


def _without_identity_keys(state: dict) -> dict:
    """The identity rides in the columns; never duplicated inside the jsonb."""
    return {
        key: value for key, value in state.items()
        if not key.startswith("account_identity_")
    }


def _audit_bind(config: dict, *, symbol: str, task_id: str, legs_verified: int) -> None:
    """Best-effort audit line; the returned report is the operator evidence."""
    with contextlib.suppress(OSError):
        append_event(Path(config["exec_log_path"]), {
            "event": "legacy_takeover_bound",
            "timestamp": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "task_id": task_id,
            "legs_verified": legs_verified,
        })
