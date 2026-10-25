"""Venue-backed futures profit protection for durable position states."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from server import job_store
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.futures_account import FuturesPosition, read_futures_positions
from tradingagents.execution.profit_protection import evaluate_profit_protection


def manage_active_positions(
    bridge: Any,
    *,
    allow_mutation: bool,
    config: dict,
) -> list[dict]:
    """Advance every linked live position once, without creating new entries."""
    states = job_store.active_position_states()
    if not states:
        return []
    if config.get("risk_derivatives_profit_protection_enabled") is not True:
        return [{"position_key": state["position_key"], "status": "disabled"} for state in states]
    try:
        _validate_policy_config(config)
    except (TypeError, ValueError):
        return [{"position_key": state["position_key"], "status": "invalid_policy_config"} for state in states]
    try:
        positions = read_futures_positions(bridge)
    except Exception as exc:
        return [
            {"position_key": state["position_key"], "status": f"positions_unavailable:{type(exc).__name__}"}
            for state in states
        ]
    if bridge.exchange_id != "binance":
        return [{"position_key": state["position_key"], "status": "venue_unsupported"} for state in states]
    position_map = _position_map(positions)
    mark_status: dict[str, str] = {}
    if allow_mutation:
        try:
            mode = bridge.client().fetch_position_mode(params={"subType": "linear"})
        except Exception:
            mode = None
        if not isinstance(mode, dict):
            return [{"position_key": state["position_key"], "status": "position_mode_unavailable"} for state in states]
        if mode.get("hedged") is not False:
            return [{"position_key": state["position_key"], "status": "hedge_mode_unsupported"} for state in states]
        max_mark_age = float(config.get("risk_derivatives_mark_max_age_seconds", 120.0))
        marks: dict[str, tuple[float | None, str | None]] = {}
        for state in states:
            snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
            key = _position_identity(state["position_key"], snapshot)
            if key not in position_map:
                continue
            symbol = key[0]
            if symbol not in marks:
                marks[symbol] = _fresh_mark(bridge, symbol, max_mark_age)
            _price, status = marks[symbol]
            if status is not None:
                mark_status[state["position_key"]] = status
            # The fetched mark only proves the price feed is fresh; the position
            # itself stays the authority for its mark. A position without a mark
            # must fail closed, never be completed from another endpoint.
    grouped_states: dict[tuple[str, str], list[dict]] = {}
    for state in states:
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        symbol, side = _position_identity(state["position_key"], snapshot)
        grouped_states.setdefault((symbol, side), []).append(state)
    ambiguous_keys = {
        key for key, grouped in grouped_states.items()
        if len(grouped) > 1 and key in position_map
    }
    outcomes = []
    for state in states:
        try:
            snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
            key = _position_identity(state["position_key"], snapshot)
            if key in ambiguous_keys:
                outcomes.append({"position_key": state["position_key"], "status": "ambiguous_net_position"})
                continue
            if state["position_key"] in mark_status:
                outcomes.append({"position_key": state["position_key"], "status": mark_status[state["position_key"]]})
                continue
            outcomes.append(_manage_one(bridge, state, position_map, allow_mutation, config))
        except Exception as exc:
            outcomes.append({
                "position_key": state["position_key"],
                "status": f"error:{type(exc).__name__}:{exc}",
            })
    return outcomes


def _manage_one(
    bridge: Any,
    state: dict,
    position_map: dict[tuple[str, str], FuturesPosition],
    allow_mutation: bool,
    config: dict,
) -> dict:
    position_key = state["position_key"]
    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    if not state.get("bracket_task_id"):
        entry_order_id = state.get("entry_order_id")
        if not entry_order_id:
            return {"position_key": position_key, "status": "missing_bracket_link"}
        symbol, _side = _position_identity(position_key, snapshot)
        candidates = job_store.find_bracket_recoveries(symbol, str(entry_order_id))
        if len(candidates) != 1:
            return {"position_key": position_key, "status": "missing_bracket_link"}
        candidate = candidates[0]
        if candidate.get("venue_key") and candidate["venue_key"] != _venue_key(bridge):
            return {"position_key": position_key, "status": "venue_mismatch"}
        state = {**state, "bracket_task_id": candidate["task_id"], "venue_key": candidate.get("venue_key")}
        if not job_store.save_position_state(position_key, state["revision"], state):
            return {"position_key": position_key, "status": "revision_conflict"}
        state["revision"] += 1
    owner = job_store.acquire_position_monitor_lease(position_key)
    if owner is None:
        return {"position_key": position_key, "status": "lease_busy"}
    try:
        return _manage_one_with_lease(bridge, state, position_map, allow_mutation, config)
    finally:
        job_store.release_position_monitor_lease(position_key, owner)


def _manage_one_with_lease(
    bridge: Any,
    state: dict,
    position_map: dict[tuple[str, str], FuturesPosition],
    allow_mutation: bool,
    config: dict,
) -> dict:
    position_key = state["position_key"]
    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    bracket_task_id = state.get("bracket_task_id")
    if not bracket_task_id:
        return {"position_key": position_key, "status": "missing_bracket_link"}
    side = str(snapshot.get("side") or "")
    symbol, side = _position_identity(position_key, snapshot)
    venue_position = position_map.get((symbol, side))
    manager = BracketManager(bridge)
    if venue_position is None:
        cleanup = manager.cancel_all(bracket_task_id, allow_mutation=allow_mutation)
        if cleanup == "completed":
            job_store.delete_position_state(position_key, expected_revision=state["revision"])
            return {"position_key": position_key, "status": "closed"}
        return {"position_key": position_key, "status": f"closed_{cleanup}"}

    if not _valid_position(venue_position):
        return {"position_key": position_key, "status": "invalid_position_snapshot"}
    if not allow_mutation:
        return {"position_key": position_key, "status": "mutation_gated"}

    entry = float(venue_position.entry_price)
    contracts = float(venue_position.contracts) if venue_position.contracts is not None else 0.0
    quantity = abs(float(venue_position.quantity)) if venue_position.quantity is not None else 0.0
    mark = float(venue_position.mark_price)
    initial_stop = float(snapshot.get("initial_stop", snapshot.get("stop")))
    task = job_store.load_bracket_recovery(bracket_task_id)
    if task is None:
        return {"position_key": position_key, "status": "missing_bracket_task"}
    if task.get("venue_key") and task["venue_key"] != _venue_key(bridge):
        return {"position_key": position_key, "status": "venue_mismatch"}
    stop_leg = next((leg for leg in task["legs"] if leg["name"] == "stop-loss"), None)
    target_legs = [leg for leg in task["legs"] if leg["name"].startswith("take-profit")]
    if stop_leg is None or not target_legs:
        return {"position_key": position_key, "status": "incomplete_bracket"}
    ladder = len(target_legs) > 1
    current_stop = float(stop_leg["params"]["stopLossPrice"])
    current_target = None if ladder else float(target_legs[0]["params"]["takeProfitPrice"])
    decision = evaluate_profit_protection(
        side=side,
        entry=entry,
        initial_stop=initial_stop,
        current_stop=current_stop,
        current_target=current_target,
        mark=mark,
        break_even_r=float(config.get("risk_derivatives_break_even_r", 1.0)),
        lock_profit_r=float(config.get("risk_derivatives_lock_profit_r", 2.0)),
        trail_start_r=float(config.get("risk_derivatives_trail_start_r", 3.0)),
        trail_distance_r=float(config.get("risk_derivatives_trail_distance_r", 1.0)),
        target_extension_r=float(config.get("risk_derivatives_target_extension_r", 1.0)),
        min_stop_move_r=float(config.get("risk_derivatives_min_stop_move_r", 0.1)),
    )
    stop_changed = not _same(decision.stop, current_stop)
    target_changed = decision.target is not None and not _same(decision.target, current_target)
    stop_size_changed = not _same(contracts, stop_leg.get("quantity", task["quantity"]))
    target_size_changed = (
        not ladder and not _same(contracts, target_legs[0].get("quantity", task["quantity"]))
    )
    if stop_changed or stop_size_changed:
        status = manager.replace_leg(
            bracket_task_id, "stop-loss", trigger_price=decision.stop,
            quantity=contracts, allow_mutation=True,
            min_stop_move=float(config.get("risk_derivatives_min_stop_move_r", 0.1)) * abs(entry - initial_stop),
        )
        if status not in {"replaced", "unchanged"}:
            return {"position_key": position_key, "status": status}
    if target_changed or target_size_changed:
        status = manager.replace_leg(
            bracket_task_id, "take-profit", trigger_price=decision.target,
            quantity=contracts, allow_mutation=True,
        )
        if status not in {"replaced", "unchanged"}:
            return {"position_key": position_key, "status": status}

    snapshot = {
        **snapshot,
        "entry": entry,
        "quantity": quantity,
        "contracts": contracts,
        "mark": mark,
        "stop": decision.stop,
        "initial_stop": initial_stop,
        "current_target": decision.target,
        "targets": [float(leg["params"]["takeProfitPrice"]) for leg in target_legs],
        "scale_out": ladder,
        "phase": decision.phase,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "highest_mark": max(float(snapshot.get("highest_mark", mark)), mark) if side == "long" else snapshot.get("highest_mark"),
        "lowest_mark": min(float(snapshot.get("lowest_mark", mark)), mark) if side == "short" else snapshot.get("lowest_mark"),
    }
    if not job_store.save_position_state(
        position_key,
        state["revision"],
        {**state, "snapshot": snapshot},
    ):
        return {"position_key": position_key, "status": "revision_conflict"}
    return {"position_key": position_key, "status": "updated" if stop_changed or target_changed or stop_size_changed or target_size_changed else "unchanged"}


def _fresh_mark(
    bridge: Any, symbol: str, max_age_seconds: float,
) -> tuple[float | None, str | None]:
    """Read the venue mark and reject it unless its own timestamp is fresh."""
    try:
        mark = bridge.client().fetch_mark_price(symbol, params={"subType": "linear"})
    except Exception:
        return None, "mark_unavailable"
    if not isinstance(mark, dict):
        return None, "mark_unavailable"
    try:
        price = float(mark.get("markPrice") or mark.get("last"))
    except (TypeError, ValueError):
        return None, "mark_unavailable"
    if not math.isfinite(price) or price <= 0:
        return None, "mark_unavailable"
    timestamp = mark.get("timestamp")
    if timestamp is None:
        return None, "mark_timestamp_unavailable"
    try:
        age_ms = datetime.now(timezone.utc).timestamp() * 1000 - float(timestamp)
    except (TypeError, ValueError):
        return None, "mark_timestamp_unavailable"
    if age_ms < -5_000 or age_ms > max_age_seconds * 1000:
        return None, "mark_stale"
    return price, None


def _position_identity(position_key: str, snapshot: dict) -> tuple[str, str]:
    parts = position_key.rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError(f"invalid futures position key: {position_key!r}")
    symbol, side_token, _entry_id = parts
    side = str(snapshot.get("side") or ("long" if side_token == "buy" else "short" if side_token == "sell" else ""))
    return symbol, side


def _validate_policy_config(config: dict) -> None:
    values = tuple(float(config.get(key, default)) for key, default in (
        ("risk_derivatives_break_even_r", 1.0),
        ("risk_derivatives_lock_profit_r", 2.0),
        ("risk_derivatives_trail_start_r", 3.0),
        ("risk_derivatives_trail_distance_r", 1.0),
        ("risk_derivatives_target_extension_r", 1.0),
    ))
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise ValueError("profit-protection thresholds must be finite and positive")
    if not values[0] < values[1] < values[2]:
        raise ValueError("profit-protection thresholds must be strictly increasing")
    min_stop_move = float(config.get("risk_derivatives_min_stop_move_r", 0.1))
    if not math.isfinite(min_stop_move) or min_stop_move < 0:
        raise ValueError("minimum stop movement must be finite and non-negative")
    max_mark_age = float(config.get("risk_derivatives_mark_max_age_seconds", 120.0))
    if not math.isfinite(max_mark_age) or max_mark_age <= 0:
        raise ValueError("mark maximum age must be finite and positive")


def _venue_key(bridge: Any) -> str:
    config = bridge.config
    return "|".join((
        str(bridge.exchange_id).lower(),
        str(config.get("exec_base_url") or "").rstrip("/").lower(),
        str(config.get("exec_sandbox") is True),
    ))


def _position_map(positions: tuple[FuturesPosition, ...]) -> dict[tuple[str, str], FuturesPosition]:
    mapped = {}
    for position in positions:
        if position.symbol and position.side:
            key = (position.symbol, position.side)
            if key in mapped:
                raise ValueError(f"ambiguous venue positions for {position.symbol} {position.side}")
            mapped[key] = position
    return mapped


def _valid_position(position: FuturesPosition) -> bool:
    return (
        position.entry_price is not None
        and position.mark_price is not None
        and position.quantity is not None
        and math.isfinite(float(position.entry_price))
        and math.isfinite(float(position.mark_price))
        and math.isfinite(float(position.quantity))
        and float(position.entry_price) > 0
        and float(position.mark_price) > 0
        and abs(float(position.quantity)) > 0
    )


def _same(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left == right
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-12)
