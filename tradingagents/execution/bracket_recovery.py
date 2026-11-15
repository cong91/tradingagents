"""Recovery orchestration for durable futures protective brackets."""

from __future__ import annotations

import math
from typing import Any

from server import job_store
from tradingagents.execution import futures_identity
from tradingagents.execution.bracket_legs import TERMINAL_STATUSES
from tradingagents.execution.brackets import BracketManager, stored_account_identity
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.futures_account import observe_futures_positions
from tradingagents.execution.futures_operation_lock import operation_lock, require_operation_lock


def _verified_protection_task(
    bridge: Any, task_id: str, *, expected_symbol: str,
) -> str | None:
    """Require a fresh, bound bracket and one complete open venue position."""
    guard = require_operation_lock(expected_symbol)
    guard.check()
    task = job_store.load_bracket_recovery(task_id)
    if task is None or task.get("task_id") != task_id:
        return "task_unknown"
    if task_id.startswith("entry-intent:"):
        return "entry_reconciliation_required"
    if task.get("status") not in {"pending", "submission_uncertain", "monitoring"}:
        return "manual_reconciliation_required"
    symbol = task.get("symbol")
    if not isinstance(symbol, str) or symbol != expected_symbol:
        return "bracket_identity_mismatch"
    entry_order_id = task.get("entry_order_id")
    if not isinstance(entry_order_id, str) or not entry_order_id or entry_order_id == "unknown":
        return "bracket_identity_mismatch"
    if task_id != f"bracket:{symbol}:{entry_order_id}":
        return "bracket_identity_mismatch"
    if task.get("venue_key") != venue_key(bridge):
        return "venue_mismatch"

    legs = task.get("legs")
    if not isinstance(legs, list) or not legs:
        return "missing_bracket_legs"
    names = []
    for leg in legs:
        if not isinstance(leg, dict) or not isinstance(leg.get("params"), dict):
            return "missing_bracket_legs"
        name = leg.get("name")
        if not isinstance(name, str) or leg.get("status") not in {
            "pending", "uncertain", "placed", "filled", "cancelled",
        }:
            return "missing_bracket_legs"
        param = "stopLossPrice" if name == "stop-loss" else (
            "takeProfitPrice" if name.startswith("take-profit") else None
        )
        if param is None or _positive_number(leg["params"].get(param)) is None:
            return "missing_bracket_legs"
        if leg.get("status") == "placed" and not leg.get("order_id"):
            return "missing_bracket_legs"
        if leg.get("status") == "pending" and not leg.get("client_order_id"):
            return "missing_bracket_legs"
        if leg.get("status") == "uncertain" and not leg.get("order_id") and not any(
            leg.get(key) for key in (
                "client_order_id", "repair_client_id", "replacement_client_id",
            )
        ):
            return "missing_bracket_legs"
        names.append(name)
    if (
        len(names) != len(set(names))
        or "stop-loss" not in names
        or not any(name.startswith("take-profit") for name in names)
    ):
        return "missing_bracket_legs"

    task_identity = stored_account_identity(task)
    if task_identity is None:
        return "account_identity_unknown"
    try:
        client = bridge.client()
        credentials = (getattr(client, "apiKey", None), getattr(client, "secret", None))
        comparison = futures_identity.compare_futures_account_identity(bridge, task_identity)
    except Exception:
        return "account_identity_unknown"
    if comparison is futures_identity.IdentityComparison.MISMATCH:
        return "account_identity_mismatch"
    if comparison is not futures_identity.IdentityComparison.MATCH:
        return "account_identity_unknown"
    if not _protection_client_stable(bridge, client, credentials, task):
        return "account_identity_unknown"

    states = [
        state for state in job_store.active_position_states()
        if state.get("bracket_task_id") == task_id
    ]
    if not states:
        return "missing_position_link"
    if len(states) != 1:
        return "ambiguous_position_link"
    state = states[0]
    if stored_account_identity(state) != task_identity:
        return "account_identity_unknown"
    if state.get("venue_key") != task.get("venue_key"):
        return "venue_mismatch"
    snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    parts = str(state.get("position_key") or "").rsplit(":", 2)
    if len(parts) != 3 or parts[0] != symbol or parts[2] != entry_order_id:
        return "bracket_identity_mismatch"
    expected_side = "long" if parts[1] == "buy" else "short" if parts[1] == "sell" else None
    if expected_side is None or snapshot.get("side") != expected_side:
        return "bracket_identity_mismatch"
    if state.get("entry_order_id") != entry_order_id:
        return "bracket_identity_mismatch"
    if task.get("close_side") != ("sell" if expected_side == "long" else "buy"):
        return "bracket_identity_mismatch"

    try:
        observation = observe_futures_positions(bridge)
    except Exception:
        return "position_unknown"
    if (
        not observation.scope_complete
        or observation.unknown_scope
        or observation.unknown_symbols
        or not _protection_client_stable(bridge, client, credentials, task)
    ):
        return "position_unknown"
    symbol_positions = [position for position in observation.positions if position.symbol == symbol]
    if len(symbol_positions) != 1:
        return "position_unknown" if not symbol_positions else "ambiguous_net_position"
    if not _valid_open_position(symbol_positions[0], expected_side):
        return "position_unknown"
    guard.check()
    return None


def _valid_open_position(position: Any, expected_side: str) -> bool:
    """Require a finite, non-flat current position on the durable side."""
    try:
        signed_quantity = float(getattr(position, "quantity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    magnitude = abs(signed_quantity)
    return (
        getattr(position, "side", None) == expected_side
        and math.isfinite(signed_quantity)
        and math.isfinite(magnitude)
        and magnitude > 0
        and (
            (expected_side == "long" and signed_quantity > 0)
            or (expected_side == "short" and signed_quantity < 0)
        )
        and _positive_number(getattr(position, "contracts", None)) is not None
        and _positive_number(getattr(position, "entry_price", None)) is not None
        and _positive_number(getattr(position, "mark_price", None)) is not None
    )


def _protection_client_stable(bridge: Any, client: Any, credentials: tuple, task: dict) -> bool:
    try:
        return (
            bridge.client() is client
            and (getattr(client, "apiKey", None), getattr(client, "secret", None)) == credentials
            and venue_key(bridge) == task.get("venue_key")
        )
    except Exception:
        return False


def recover_bracket(bridge: Any, config: dict, task_id: str, *, confirm: bool = True) -> str:
    """Repair exits under live mode or verified protection recovery."""
    if confirm is not True:
        raise PermissionError("bracket recovery requires confirm=True")
    task = job_store.load_bracket_recovery(task_id)
    if task is None:
        raise KeyError(task_id)
    if task_id.startswith("entry-intent:"):
        return monitor_bracket(bridge, config, task_id)
    if not task_id.startswith("bracket:"):
        raise PermissionError("only regular bracket tasks may be repaired")
    if not bridge._runtime_gate("exec_live") and config.get(
        "risk_derivatives_profit_protection_enabled"
    ) is not True:
        raise PermissionError("bracket recovery requires profit protection")
    with operation_lock(task["symbol"]) as guard:
        guard.check()
        current = job_store.load_bracket_recovery(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.get("symbol") != task.get("symbol"):
            return "bracket_identity_mismatch"
        if current.get("status") not in {"pending", "submission_uncertain", "monitoring"}:
            return "manual_reconciliation_required"
        verdict = _verified_protection_task(
            bridge, task_id, expected_symbol=current["symbol"],
        )
        if verdict is not None:
            return verdict
        guard.check()
        return BracketManager(bridge).reconcile(
            task_id, repair_missing=True, allow_sibling_cancel=True,
        )


def monitor_bracket(bridge: Any, config: dict, task_id: str) -> str:
    """Inspect protective exits; entry intents use read-only recovery first."""
    task = job_store.load_bracket_recovery(task_id)
    if task is None:
        raise KeyError(task_id)
    with operation_lock(task["symbol"]) as guard:
        guard.check()
        current = job_store.load_bracket_recovery(task_id)
        if current is None:
            raise KeyError(task_id)
        if current.get("symbol") != task.get("symbol"):
            return "bracket_identity_mismatch"
        if current["task_id"].startswith("entry-intent:"):
            from tradingagents.execution.futures_entry_recovery import reconcile_entry

            return reconcile_entry(bridge, task_id)
        if current.get("status") in TERMINAL_STATUSES:
            return current["status"]
        if current.get("status") not in {"pending", "submission_uncertain", "monitoring"}:
            return "manual_reconciliation_required"
        allow_cancel = (
            (
                bridge._runtime_gate("exec_live")
                or config.get("risk_derivatives_profit_protection_enabled") is True
            )
            and current["task_id"].startswith("bracket:")
            and current.get("status") in {"pending", "submission_uncertain", "monitoring"}
            and _verified_protection_task(
                bridge, task_id, expected_symbol=current["symbol"],
            ) is None
        )
        guard.check()
        return BracketManager(bridge).reconcile(
            task_id, repair_missing=False, allow_sibling_cancel=allow_cancel,
        )


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
