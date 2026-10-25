"""Apply an adoption plan on the venue, then hand the position to the monitor.

Safety order is the same as bracket replacement: place the new full-size legs,
prove they exist, and only then cancel the old ladder. A failure before that
point leaves the venue exactly as it was.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server import job_store
from tradingagents.execution.brackets import conditional_params
from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.execution.position_adoption import AdoptionPlan, plan_adoption

LIVE_ALGO_STATUSES = {"NEW", "PARTIALLY_FILLED", "TRIGGERING", "TRIGGERED"}


def adopt_position(
    bridge: Any,
    position: FuturesPosition,
    *,
    venue_legs: list[dict],
    allow_mutation: bool,
) -> dict:
    """Replace one position's ladder with a single verified stop/target pair."""
    symbol = str(position.symbol)
    try:
        plan = plan_adoption(
            position, venue_legs, fallback_stop=_recorded_stop(symbol),
        )
    except ValueError as exc:
        return {"symbol": symbol, "status": "invalid_position", "reason": str(exc)}

    if not allow_mutation:
        return {"symbol": symbol, "status": "mutation_gated", "plan": _plan_payload(plan)}
    if plan.stop is None:
        return {
            "symbol": symbol,
            "status": "no_stop_to_adopt",
            "reason": "the ladder carries no usable stop; refusing to invent one",
            "plan": _plan_payload(plan),
        }
    if plan.stop_shortfall > 0:
        return {
            "symbol": symbol,
            "status": "uncovered_contracts",
            "reason": f"{plan.stop_shortfall:g} contracts have no stop in the ladder",
            "plan": _plan_payload(plan),
        }

    task_id = f"bracket:{symbol}:adopted"
    placed = _place_replacement(bridge, plan, task_id)
    if placed["status"] != "placed":
        return placed

    cleanup = _cancel_ladder(bridge, symbol, exclude=set(placed["order_ids"]))
    if cleanup["errors"]:
        return {
            "symbol": symbol,
            "status": "replacement_cleanup_pending",
            "reason": "; ".join(cleanup["errors"]),
            "plan": _plan_payload(plan),
            "order_ids": placed["order_ids"],
        }

    pruned = _prune_states(symbol)
    _write_state(bridge, plan, task_id, placed["order_ids"])
    return {
        "symbol": symbol,
        "status": "adopted",
        "plan": _plan_payload(plan),
        "order_ids": placed["order_ids"],
        "cancelled": cleanup["cancelled"],
        "pruned_states": pruned,
    }


def _recorded_stop(symbol: str) -> float | None:
    """The stop the bot recorded for this symbol, newest state first."""
    states = [
        state for state in job_store.active_position_states()
        if state["position_key"].rsplit(":", 2)[0] == symbol
    ]
    for state in reversed(states):
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        try:
            value = float(snapshot.get("initial_stop") or snapshot.get("stop"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _plan_payload(plan: AdoptionPlan) -> dict:
    return {
        "side": plan.side,
        "contracts": plan.contracts,
        "entry": plan.entry,
        "stop": plan.stop,
        "target": plan.target,
        "dropped_legs": list(plan.dropped_legs),
        "stop_shortfall": plan.stop_shortfall,
    }


def _place_replacement(bridge: Any, plan: AdoptionPlan, task_id: str) -> dict:
    exchange = bridge.client()
    legs = [
        {"name": name, "params": params, "status": "pending", "order_id": None}
        for name, params in (
            ("stop-loss", {"stopLossPrice": plan.stop}),
            ("take-profit", {"takeProfitPrice": plan.target}),
        ) if params.get("stopLossPrice") is not None or params.get("takeProfitPrice") is not None
    ]
    job_store.create_bracket_recovery(
        task_id, symbol=plan.symbol, entry_order_id="adopted",
        close_side=plan.close_side, quantity=plan.contracts, legs=legs,
        now=_now(), venue_key=_venue_key(bridge),
    )
    task = job_store.load_bracket_recovery(task_id)
    order_ids: list[str] = []
    try:
        for leg in task["legs"]:
            response = exchange.create_order(
                plan.symbol, "market", plan.close_side, plan.contracts,
                None,
                conditional_params(bridge.exchange_id, {**leg["params"], "reduceOnly": True}),
            )
            order_id = str((response or {}).get("id") or "")
            if not order_id:
                raise RuntimeError(f"{leg['name']} returned no order id")
            leg.update(status="placed", order_id=order_id)
            order_ids.append(order_id)
            _save_task(task, task_id, "pending", order_ids)
    except Exception as exc:
        # Nothing has been cancelled yet: the venue still holds the old ladder.
        _save_task(task, task_id, "adoption_failed", order_ids, bridge._redact(repr(exc)))
        _cancel(bridge, plan.symbol, order_ids)
        return {"symbol": plan.symbol, "status": "replacement_failed", "reason": repr(exc)}

    _save_task(task, task_id, "monitoring", order_ids)
    return {"symbol": plan.symbol, "status": "placed", "order_ids": order_ids}


def _save_task(task: dict, task_id: str, status: str, order_ids: list[str], error=None) -> None:
    job_store.update_bracket_recovery(
        task_id, status=status, placed_ids=list(order_ids), attempts=0,
        last_error=error, now=_now(), legs=task["legs"],
    )


def _cancel_ladder(bridge: Any, symbol: str, *, exclude: set[str]) -> dict:
    raw = _raw_symbol(symbol)
    remaining = [
        str(leg["algoId"]) for leg in bridge.client().fapiPrivateGetOpenAlgoOrders()
        if str(leg.get("symbol")) == raw
        and str(leg.get("algoId")) not in exclude
        and str(leg.get("algoStatus") or "NEW").upper() in LIVE_ALGO_STATUSES
    ]
    return _cancel(bridge, symbol, remaining)


def _cancel(bridge: Any, symbol: str, order_ids: list[str]) -> dict:
    errors, cancelled = [], []
    for order_id in order_ids:
        try:
            bridge.client().cancel_order(order_id, symbol, params={"trigger": True})
            cancelled.append(order_id)
        except Exception as exc:
            errors.append(f"cancel {order_id}: {exc!r}")
    return {"errors": errors, "cancelled": cancelled}


def _prune_states(symbol: str) -> int:
    removed = 0
    for state in job_store.active_position_states():
        if state["position_key"].rsplit(":", 2)[0] != symbol:
            continue
        if job_store.delete_position_state(state["position_key"], state["revision"]):
            removed += 1
    return removed


def _write_state(bridge: Any, plan: AdoptionPlan, task_id: str, order_ids: list[str]) -> None:
    position_key = f"{plan.symbol}:{'buy' if plan.side == 'long' else 'sell'}:adopted"
    job_store.save_position_state(position_key, 0, {
        "snapshot": {
            "position_key": position_key,
            "side": plan.side,
            "entry": plan.entry,
            "quantity": plan.contracts,
            "contracts": plan.contracts,
            "stop": plan.stop,
            "initial_stop": plan.stop,
            "targets": [plan.target] if plan.target is not None else [],
            "current_target": plan.target,
            "phase": "adopted",
            "observed_at": _now(),
        },
        "orders": [],
        "entry_order_id": "adopted",
        "bracket_task_id": task_id,
        "venue_key": _venue_key(bridge),
    })


def _raw_symbol(symbol: str) -> str:
    # "ETH/USDC:USDC" -> "ETHUSDC"; the quote is part of the venue id, so it
    # must come from the unified symbol rather than an assumed USDT.
    base, _, rest = symbol.partition("/")
    quote = rest.split(":", 1)[0] or "USDT"
    return base + quote


def _venue_key(bridge: Any) -> str:
    config = bridge.config
    return "|".join((
        str(bridge.exchange_id).lower(),
        str(config.get("exec_base_url") or "").rstrip("/").lower(),
        str(config.get("exec_sandbox") is True),
    ))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
