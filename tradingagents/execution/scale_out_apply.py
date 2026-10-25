"""Convert an existing single-target position into a deliberate scale-out ladder.

Placement follows the same rule as every other mutation in this layer: add the
new tranches, verify them, and only then remove the leg they replace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server import job_store
from tradingagents.execution.brackets import conditional_params
from tradingagents.execution.scale_out import plan_scale_out


def convert_to_scale_out(
    bridge: Any,
    *,
    position_key: str,
    allow_mutation: bool,
    config: dict,
) -> dict:
    """Replace the single take-profit leg with two R tranches and a runner."""
    state = job_store.load_position_state(position_key)
    if state is None:
        return {"position_key": position_key, "status": "missing_position_state"}
    task_id = state.get("bracket_task_id")
    task = job_store.load_bracket_recovery(task_id) if task_id else None
    if task is None:
        return {"position_key": position_key, "status": "missing_bracket_task"}

    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    stop_leg = next((leg for leg in task["legs"] if leg["name"] == "stop-loss"), None)
    old_targets = [leg for leg in task["legs"] if leg["name"].startswith("take-profit")]
    if stop_leg is None:
        return {"position_key": position_key, "status": "missing_stop"}
    if len(old_targets) > 1:
        return {"position_key": position_key, "status": "already_scale_out"}
    if not allow_mutation:
        return {"position_key": position_key, "status": "mutation_gated"}

    try:
        plan = plan_scale_out(
            side=str(snapshot["side"]),
            entry=float(snapshot["entry"]),
            initial_stop=float(snapshot["initial_stop"]),
            contracts=float(snapshot["contracts"]),
            first_r=float(config.get("risk_derivatives_scale_out_first_r", 1.0)),
            first_fraction=float(config.get("risk_derivatives_scale_out_first_fraction", 0.33)),
            second_r=float(config.get("risk_derivatives_scale_out_second_r", 2.0)),
            second_fraction=float(config.get("risk_derivatives_scale_out_second_fraction", 0.33)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return {"position_key": position_key, "status": "invalid_state", "reason": str(exc)}

    mark = float(snapshot.get("mark") or 0.0)
    crossed = [
        level.multiple for level in plan.levels
        if mark and not (level.price < mark if plan.side == "short" else level.price > mark)
    ]
    if crossed:
        return {
            "position_key": position_key,
            "status": "tranche_already_crossed",
            "reason": f"mark {mark} already beyond tranche(s) {crossed}",
        }

    exchange = bridge.client()
    close_side = "sell" if plan.side == "long" else "buy"
    placed: list[dict] = []
    try:
        for index, level in enumerate(plan.levels, start=1):
            response = exchange.create_order(
                plan_symbol(task), "market", close_side, level.quantity, None,
                conditional_params(bridge.exchange_id, {
                    "takeProfitPrice": level.price, "reduceOnly": True,
                }),
            )
            order_id = str((response or {}).get("id") or "")
            if not order_id:
                raise RuntimeError(f"take-profit-{index} returned no order id")
            placed.append({"index": index, "order_id": order_id, "level": level})
    except Exception as exc:
        _cancel(bridge, plan_symbol(task), [item["order_id"] for item in placed])
        return {"position_key": position_key, "status": "placement_failed", "reason": repr(exc)}

    task["legs"] = [
        leg for leg in task["legs"] if not leg["name"].startswith("take-profit")
    ] + [
        {
            "name": f"take-profit-{item['index']}",
            "params": {"takeProfitPrice": item["level"].price},
            "status": "placed",
            "order_id": item["order_id"],
            "quantity": item["level"].quantity,
        }
        for item in placed
    ]
    job_store.update_bracket_recovery(
        task_id, status="monitoring", placed_ids=[leg["order_id"] for leg in task["legs"]],
        attempts=0, last_error=None, now=_now(), legs=task["legs"],
    )

    cancelled = _cancel(bridge, plan_symbol(task), [leg["order_id"] for leg in old_targets])
    if cancelled["errors"]:
        return {
            "position_key": position_key,
            "status": "previous_target_cleanup_pending",
            "reason": "; ".join(cancelled["errors"]),
            "order_ids": [item["order_id"] for item in placed],
        }
    return {
        "position_key": position_key,
        "status": "converted",
        "stop": plan.initial_stop,
        "tranches": [
            {"r": item["level"].multiple, "price": item["level"].price, "quantity": item["level"].quantity}
            for item in placed
        ],
        "runner_quantity": plan.runner_quantity,
        "cancelled": cancelled["cancelled"],
    }


def plan_symbol(task: dict) -> str:
    return str(task["symbol"])


def _cancel(bridge: Any, symbol: str, order_ids: list[str]) -> dict:
    errors, cancelled = [], []
    for order_id in order_ids:
        try:
            bridge.client().cancel_order(order_id, symbol, params={"trigger": True})
            cancelled.append(order_id)
        except Exception as exc:
            errors.append(f"cancel {order_id}: {exc!r}")
    return {"errors": errors, "cancelled": cancelled}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
