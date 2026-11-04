"""Durable lifecycle of the protective exits attached to a futures entry."""

from __future__ import annotations

import hashlib
import logging
import math
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

import ccxt

from server import job_store, snapshot_observer
from tradingagents.execution import futures_identity
from tradingagents.execution.bracket_legs import (
    TERMINAL_STATUSES as _TERMINAL_STATUSES,
    cancel_confirmed as _cancel_confirmed,
    conditional_params,
    info as _info,
    is_ladder as _is_ladder,
    matches as _matches,
    order_id as _order_id,
    same_number as _same_number,
    state as _state,
)
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.sizing import PlannedOrder

logger = logging.getLogger(__name__)
_TASK_LOCK = threading.RLock()

# Non-terminal time-stop close statuses: the close either never confirmed a
# fill or never read the venue. The background poll re-drives them through
# close_position (idempotent: fresh venue read first), so the retry is fenced
# by the persisted status instead of living only with the protection loop.
TIME_STOP_RETRY_STATUSES = frozenset({
    "time_stop_submission_uncertain", "time_stop_unknown_position", "time_stop_failed",
})


def reconcile_leg_rounding(exchange: Any, task: dict, leg: dict, item: Any, matches_leg) -> bool:
    """Accept a leg when ccxt's own precision functions explain the difference.

    ``matches_leg`` compares persisted planned values verbatim, but the venue
    acknowledges tick-rounded triggers and step-rounded quantities. The rounded
    values are recomputed by ccxt itself — nothing here invents a tolerance —
    then the leg record is aligned to them and the FULL contract is re-verified
    against the same venue item, so an unexplained field anywhere still fails.
    Aligned values stay on the record; any failure restores the original.
    """
    if not isinstance(item, dict):
        return False
    raw_info = _info(item)
    if not isinstance(raw_info, dict):
        return False
    param = "stopLossPrice" if leg["name"] == "stop-loss" else "takeProfitPrice"
    planned_trigger = leg.get("params", {}).get(param)
    planned_quantity = leg.get(
        "replacement_quantity", leg.get("repair_quantity", leg.get("quantity", task["quantity"])),
    )
    if planned_trigger is None or planned_quantity is None:
        return False
    venue_trigger = raw_info.get("triggerPrice")
    venue_quantity = raw_info.get("quantity")
    try:
        if not _same_number(venue_trigger, exchange.price_to_precision(task["symbol"], planned_trigger)):
            return False
        if not _same_number(venue_quantity, exchange.amount_to_precision(task["symbol"], planned_quantity)):
            return False
    except Exception:
        return False
    had_quantity = "quantity" in leg
    saved = {"trigger": planned_trigger, "quantity": leg.get("quantity")}
    for key in ("replacement_quantity", "repair_quantity"):
        if key in leg:
            saved[key] = leg[key]
    leg["params"][param] = float(venue_trigger)
    leg["quantity"] = float(venue_quantity)
    for key in ("replacement_quantity", "repair_quantity"):
        if key in leg:
            leg[key] = float(venue_quantity)
    if matches_leg(item, task, leg):
        return True
    leg["params"][param] = saved["trigger"]
    if had_quantity:
        leg["quantity"] = saved["quantity"]
    else:
        leg.pop("quantity", None)
    for key in ("replacement_quantity", "repair_quantity"):
        if key in saved:
            leg[key] = saved[key]
    return False



def stored_account_identity(record: dict) -> futures_identity.FuturesAccountIdentity | None:
    """The binding stored on a task or position row, or None for a legacy row.

    The database CHECK keeps the four columns all-NULL or all-set; any partial
    read is therefore treated as unbound (fail-closed).
    """
    values = tuple(
        record.get(f"account_identity_{name}") for name in ("exchange", "profile", "type", "hash")
    )
    if any(value is None for value in values):
        return None
    exchange, profile, account_type, identity_hash = values
    return futures_identity.FuturesAccountIdentity(
        exchange=exchange, profile=profile, account_type=account_type,
        identity_hash=identity_hash,
    )


class BracketManager:
    """Protective-exit persistence and reconciliation, without an entry-order path."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    @property
    def _trigger_params(self) -> dict:
        return {"trigger": True} if self.bridge.exchange_id == "binance" else {}

    def _conditional_params(self, params: dict) -> dict:
        return conditional_params(self.bridge.exchange_id, params)

    def _matches_leg(self, item: dict, task: dict, leg: dict) -> bool:
        return _matches(item, task, leg, require_mark_price=self.bridge.exchange_id == "binance")

    def _matches_leg_or_rounded(self, exchange: Any, item: dict, task: dict, leg: dict) -> bool:
        """The leg contract, with venue tick/step rounding reconciled first."""
        if self._matches_leg(item, task, leg):
            return True
        return reconcile_leg_rounding(exchange, task, leg, item, self._matches_leg)


    def _save(self, task: dict, status: str, error: str | None = None) -> str:
        ids = [str(leg["order_id"]) for leg in task["legs"] if leg.get("order_id")]
        job_store.update_bracket_recovery(
            task["task_id"], status=status, placed_ids=ids, attempts=task["attempts"],
            last_error=self.bridge._redact(error) if error else None,
            now=datetime.now(UTC).isoformat(), legs=task["legs"],
        )
        return status

    def place(self, order: PlannedOrder, confirm: bool, entry_order_id: str | None) -> None:
        legs = [
            {"name": name, "params": {param: value}, "status": "pending", "order_id": None}
            for name, param, value in (
                ("stop-loss", "stopLossPrice", order.stop_loss),
                ("take-profit", "takeProfitPrice", order.take_profit),
            ) if value is not None
        ]
        if not legs:
            return
        self._place_legs(order, legs, confirm, entry_order_id)

    def place_ladder(
        self,
        order: PlannedOrder,
        levels: list[tuple[float, float]],
        confirm: bool,
        entry_order_id: str | None,
    ) -> None:
        """Place one full-size stop plus deliberate profit tranches.

        ``levels`` are ``(price, quantity)`` pairs ordered by distance; the
        quantities must leave a runner that the trailing stop manages.
        """
        if order.stop_loss is None or not levels:
            return
        legs = [{"name": "stop-loss", "params": {"stopLossPrice": order.stop_loss},
                 "status": "pending", "order_id": None}]
        for index, (price, quantity) in enumerate(levels, start=1):
            legs.append({
                "name": f"take-profit-{index}",
                "params": {"takeProfitPrice": price},
                "status": "pending",
                "order_id": None,
                "quantity": quantity,
            })
        self._place_legs(order, legs, confirm, entry_order_id)

    def _identity_verdict(self, task: dict) -> str | None:
        """The deferred status when the stored binding does not verify, else None.

        A legacy row (no stored binding) can never be verified against current
        evidence, and unknown evidence is never treated as a match.
        """
        stored = stored_account_identity(task)
        if stored is None:
            logger.warning("Bracket operation deferred: account identity is unverified")
            return "account_identity_unknown"
        comparison = futures_identity.compare_futures_account_identity(self.bridge, stored)
        if comparison is futures_identity.IdentityComparison.MISMATCH:
            logger.warning(
                "Bracket operation deferred: stored account identity does not match the venue client",
            )
            return "account_identity_mismatch"
        if comparison is not futures_identity.IdentityComparison.MATCH:
            logger.warning("Bracket operation deferred: account identity is unverified")
            return "account_identity_unknown"
        return None

    def _place_legs(
        self,
        order: PlannedOrder,
        legs: list[dict],
        confirm: bool,
        entry_order_id: str | None,
    ) -> None:
        close_side = "sell" if order.side == "buy" else "buy"
        task_id = f"bracket:{order.ccxt_symbol}:{entry_order_id or 'unknown'}"
        identity = futures_identity.read_futures_account_identity(self.bridge)
        if identity is None:
            # Raising before the task row exists keeps an unbindable bracket
            # from blocking future entries via "existing active task blocks entry".
            raise RuntimeError("account identity unavailable for protective bracket; nothing placed")
        with _TASK_LOCK:
            existing_task = job_store.load_bracket_recovery(task_id)
            if existing_task is not None:
                logger.warning("Bracket placement deferred: existing task ownership is unverified")
                return
            created = job_store.create_bracket_recovery(
                task_id, symbol=order.ccxt_symbol, entry_order_id=entry_order_id or "unknown",
                close_side=close_side, quantity=float(order.quantity), legs=legs,
                now=datetime.now(UTC).isoformat(), venue_key=venue_key(self.bridge),
                identity=identity,
            )
            if not created:
                logger.warning("Bracket placement deferred: existing task ownership is unverified")
                return
            task = job_store.load_bracket_recovery(task_id)
            position_key = f"{order.ccxt_symbol}:{order.side}:{entry_order_id or 'unknown'}"
            position_state = job_store.load_position_state(position_key)
            if position_state is not None and position_state.get("bracket_task_id") != task_id:
                position_state["bracket_task_id"] = task_id
                position_state["venue_key"] = venue_key(self.bridge)
                job_store.save_position_state(
                    position_key, position_state["revision"], position_state,
                )
            exchange = self.bridge.client()
            try:
                for leg in task["legs"]:
                    if leg["status"] != "pending":
                        continue
                    quantity = float(leg.get("quantity", task["quantity"]))
                    order_params = self._conditional_params({**leg["params"], "reduceOnly": True})
                    response = snapshot_observer.run(
                        self.bridge, exchange,
                        operation="place_bracket_leg", symbol=task["symbol"],
                        call=lambda q=quantity, p=order_params: exchange.create_order(
                            task["symbol"], "market", close_side, q, None, p,
                        ),
                    )
                    leg_id = _order_id(response)
                    if not leg_id:
                        raise RuntimeError(f"{leg['name']} conditional order returned no id")
                    leg.update(status="placed", order_id=leg_id)
                    self._save(task, "pending")
            except ccxt.NetworkError as exc:
                for leg in task["legs"]:
                    if leg["status"] == "pending":
                        leg["status"] = "uncertain"
                self._save(task, "submission_uncertain", f"conditional submission uncertain; inspect before retry: {exc!r}")
                self.bridge.audit_plan(
                    order, action="bracket_failed", confirmed=confirm, order_id=entry_order_id,
                    reason=f"protective exits need inspection after uncertain submit; recovery task {task_id}",
                    phase="execute",
                )
                return
            except Exception as exc:  # the entry already exists on the venue
                self._save(task, "pending", repr(exc))
                self.bridge.audit_plan(
                    order, action="bracket_failed", confirmed=confirm, order_id=entry_order_id,
                    reason=f"protective exits failed after entry fill; recovery task {task_id}: {exc!r}",
                    phase="execute",
                )
                return
            self._save(task, "submission_uncertain", "conditional ACKs require independent resting verification")
            self.bridge.audit_plan(
                order, action="bracket_verification_required", confirmed=confirm,
                order_id=",".join(str(leg["order_id"]) for leg in task["legs"]),
                reason="conditional orders acknowledged; resting status is unverified",
                phase="execute",
            )

    def reconcile(self, task_id: str, *, repair_missing: bool, allow_sibling_cancel: bool) -> str:
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                return "venue_mismatch"
            if task["task_id"].startswith("entry-intent:"):
                return "entry_reconciliation_required"
            verdict = self._identity_verdict(task)
            if verdict is not None:
                return verdict
            return self._reconcile_for_verified_account(
                task_id, repair_missing=repair_missing, allow_sibling_cancel=allow_sibling_cancel,
            )

    def _reconcile_for_verified_account(self, task_id: str, *, repair_missing: bool, allow_sibling_cancel: bool) -> str:
        """Unwired until the caller can establish the task's account identity."""
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["task_id"].startswith("entry-intent:"):
                return "entry_reconciliation_required"
            if task["status"] in _TERMINAL_STATUSES:
                return task["status"]
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
            if task["status"] in TIME_STOP_RETRY_STATUSES:
                # allow_sibling_cancel is reconcile's only mutation gate: the
                # monitor passes the live-gate verdict, recovery passes True.
                return self.close_position(
                    task_id, reason="time_stop", allow_mutation=allow_sibling_cancel,
                )
            exchange = self.bridge.client()
            try:
                existing = exchange.fetch_open_orders(task["symbol"], params=self._trigger_params)
                if not isinstance(existing, list):
                    raise ValueError("conditional order list is invalid")
            except Exception as exc:
                self._save(task, task["status"], f"conditional order inspection failed: {exc!r}")
                return "inspection_failed"
            errors = self._inspect(exchange, task, existing)
            if errors:
                self._save(task, task["status"], "; ".join(errors))
                return "reconcile_incomplete"
            if task["status"] == "submission_uncertain":
                if any(leg["status"] == "uncertain" for leg in task["legs"]):
                    return self._save(task, task["status"], "conditional leg remains uncertain")
                task["status"] = "pending" if any(
                    leg["status"] == "pending" for leg in task["legs"]
                ) else "monitoring"
            filled = [leg for leg in task["legs"] if leg["status"] == "filled"]
            if filled:
                if _is_ladder(task) and filled[0]["name"] != "stop-loss":
                    return self._settle_ladder(task, filled[0], allow_sibling_cancel)
                return self._cleanup(exchange, task, filled[0], allow_sibling_cancel)
            if all(leg["status"] == "cancelled" for leg in task["legs"]):
                return self._save(task, "cancelled")
            if any(leg["status"] == "cancelled" for leg in task["legs"]):
                return self._save(task, "monitoring", "conditional leg canceled without a confirmed fill")
            cleanup_errors = self._cleanup_retired(exchange, task, allow_sibling_cancel)
            if cleanup_errors:
                status = "replacement_cleanup_pending" if any(
                    leg.get("retired_ids") for leg in task["legs"]
                ) else "monitoring"
                return self._save(task, status, "; ".join(cleanup_errors))
            if task["status"] == "replacement_cleanup_pending":
                task["status"] = "monitoring"
            if repair_missing and any(leg["status"] == "pending" for leg in task["legs"]):
                return self._repair(exchange, task)
            return self._save(task, task["status"] if task["status"] == "pending" else "monitoring")

    def replace_leg(
        self,
        task_id: str,
        leg_name: str,
        *,
        trigger_price: float,
        quantity: float,
        allow_mutation: bool,
        min_stop_move: float = 0.0,
    ) -> str:
        """Place a replacement conditional leg before retiring its predecessor."""
        if not math.isfinite(trigger_price) or trigger_price <= 0:
            raise ValueError("replacement trigger must be finite and positive")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("replacement quantity must be finite and positive")
        if not math.isfinite(min_stop_move) or min_stop_move < 0:
            raise ValueError("minimum stop movement must be finite and non-negative")
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                return "venue_mismatch"
            verdict = self._identity_verdict(task)
            if verdict is not None:
                return verdict
            return self._replace_leg_for_verified_account(
                task_id, leg_name, trigger_price=trigger_price, quantity=quantity,
                allow_mutation=allow_mutation, min_stop_move=min_stop_move,
            )

    def _replace_leg_for_verified_account(
        self, task_id: str, leg_name: str, *, trigger_price: float, quantity: float,
        allow_mutation: bool, min_stop_move: float = 0.0,
    ) -> str:
        """Replacement logic; not reachable until account ownership is proven."""
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
            leg = next((item for item in task["legs"] if item["name"] == leg_name), None)
            if leg is None:
                raise ValueError(f"unknown bracket leg {leg_name!r}")
            param = "stopLossPrice" if leg_name == "stop-loss" else "takeProfitPrice"
            old_id = leg.get("order_id")
            old_price = leg["params"].get(param)
            exchange = self.bridge.client()
            if not old_id:
                return "leg_state_unknown"
            if old_id:
                try:
                    current = exchange.fetch_order(old_id, task["symbol"], params=self._trigger_params)
                    current_state = _state(current)
                except Exception:
                    return "leg_state_unknown"
                info = _info(current)
                raw_status = str(info.get("algoStatus") or current.get("status") or "").upper()
                if current_state == "filled" or raw_status in {"TRIGGERING", "TRIGGERED"}:
                    return "leg_triggering"
                if current_state not in {"placed", "cancelled"}:
                    return "leg_state_unknown"
                if current_state == "cancelled":
                    return "leg_cancelled"
            if isinstance(current, dict) and _order_id(current) != old_id:
                return "leg_state_unknown"
            if old_price is not None:
                improvement = trigger_price - float(old_price) if task["close_side"] == "sell" else float(old_price) - trigger_price
                if improvement < 0:
                    return "non_monotonic_replacement"
                if leg_name == "stop-loss" and 0 < improvement < min_stop_move:
                    return "below_minimum_stop_move"
            if leg.get("replacement_price") is not None:
                return self.reconcile(
                    task_id, repair_missing=False, allow_sibling_cancel=True,
                )
            if leg["params"].get(param) == trigger_price and leg.get("quantity", task["quantity"]) == quantity:
                return "unchanged"
            replacement_client_id = "br-" + secrets.token_hex(12)
            leg.update(
                status="uncertain", replacement_price=float(trigger_price),
                replacement_quantity=float(quantity), price_key=param,
                replacement_client_id=replacement_client_id,
            )
            self._save(task, task["status"])
            try:
                response = snapshot_observer.run(
                    self.bridge, exchange,
                    operation="replace_bracket_leg", symbol=task["symbol"],
                    call=lambda: exchange.create_order(
                        task["symbol"], "market", task["close_side"], quantity,
                        None, self._conditional_params({
                            param: trigger_price, "reduceOnly": True,
                            "clientOrderId": replacement_client_id,
                        }),
                    ),
                )
                new_id = _order_id(response)
                if not new_id:
                    raise RuntimeError(f"{leg_name} replacement returned no id")
            except ccxt.NetworkError as exc:
                self._save(task, "monitoring", f"replacement submission uncertain: {exc!r}")
                return "submission_uncertain"
            except Exception as exc:
                self._save(task, "monitoring", f"replacement submission unverified: {exc!r}")
                return "submission_uncertain"
            leg["replacement_order_id"] = new_id
            self._save(task, "monitoring")
            # The create ACK does not prove the replacement is resting. Retiring
            # the old leg on an unverified ACK could leave the position without
            # any stop, so "replaced" is claimed only after an independent
            # venue read confirms the new conditional order is working.
            try:
                confirmed = exchange.fetch_order(
                    new_id, task["symbol"], params=self._trigger_params
                )
                confirmed_state = _state(confirmed) if isinstance(confirmed, dict) else None
            except Exception as exc:
                self._save(
                    task, "monitoring",
                    f"replacement {new_id} resting state unverified; existing leg retained: {exc!r}",
                )
                return "submission_uncertain"
            if (
                confirmed_state == "cancelled"
                and isinstance(confirmed, dict)
                and self._matches_leg_or_rounded(exchange, confirmed, task, leg)
            ):
                for key in ("replacement_price", "replacement_quantity", "price_key",
                            "replacement_order_id", "replacement_client_id"):
                    leg.pop(key, None)
                leg["status"] = "placed"
                self._save(
                    task, "monitoring",
                    f"replacement {new_id} not resting on the venue; existing leg retained",
                )
                return "replacement_failed"
            if confirmed_state != "placed" or not self._matches_leg_or_rounded(exchange, confirmed, task, leg):
                # filled or an unmapped status: claim neither resting nor dead;
                # keep the old leg and let reconcile plus the flat-position
                # monitor settle the outcome before anything is retired.
                self._save(
                    task, "monitoring",
                    f"replacement {new_id} venue state {confirmed_state!r} is not resting; existing leg retained",
                )
                return "submission_uncertain"
            leg["retired_ids"] = list(dict.fromkeys([*leg.get("retired_ids", []), old_id])) if old_id else list(leg.get("retired_ids", []))
            leg.update(
                order_id=new_id, status="placed", params={param: float(trigger_price)},
                quantity=float(quantity),
            )
            leg.pop("replacement_price", None)
            leg.pop("replacement_quantity", None)
            leg.pop("replacement_order_id", None)
            leg.pop("replacement_client_id", None)
            leg.pop("price_key", None)
            task["status"] = "monitoring"
            self._save(task, "monitoring")
            errors = self._cleanup_retired(exchange, task, True)
            if errors:
                return self._save(task, "replacement_cleanup_pending", "; ".join(errors))
            return "replaced"

    def cancel_all(self, task_id: str, *, allow_mutation: bool) -> str:
        """Cancel every current and retired leg after the venue reports a flat position."""
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                return "venue_mismatch"
            verdict = self._identity_verdict(task)
            if verdict is not None:
                return verdict
            return self._cancel_all_for_verified_account(task_id, allow_mutation=allow_mutation)

    def _cancel_all_for_verified_account(self, task_id: str, *, allow_mutation: bool) -> str:
        """Cleanup logic; not reachable until account ownership is proven."""
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
            if any(leg.get("replacement_price") is not None for leg in task["legs"]):
                return self._save(task, "position_cleanup_pending", "replacement intent is unresolved")
            exchange = self.bridge.client()
            errors = []
            try:
                existing = exchange.fetch_open_orders(task["symbol"], params=self._trigger_params)
                if not isinstance(existing, list):
                    raise ValueError("conditional order list is invalid")
            except Exception as exc:
                return self._save(task, "position_cleanup_pending", f"conditional order inspection failed: {exc!r}")
            observed = {_order_id(item): item for item in existing if isinstance(item, dict) and _order_id(item)}
            for leg in task["legs"]:
                ids = list(dict.fromkeys(
                    ([leg["order_id"]] if leg.get("order_id") else []) + leg.get("retired_ids", [])
                ))
                for order_id in ids:
                    item = observed.get(order_id)
                    try:
                        if item is None:
                            item = exchange.fetch_order(order_id, task["symbol"], params=self._trigger_params)
                        state = _state(item) if isinstance(item, dict) else None
                        if state == "cancelled" or state == "filled":
                            continue
                        if state != "placed":
                            raise ValueError(f"unconfirmed status for conditional order {order_id}")
                        response = snapshot_observer.run(
                            self.bridge, exchange,
                            operation="cancel_bracket_leg", symbol=task["symbol"],
                            call=lambda oid=order_id: exchange.cancel_order(
                                oid, task["symbol"], params=self._trigger_params,
                            ),
                        )
                        if not _cancel_confirmed(response, str(order_id)):
                            raise ValueError("conditional cancel returned unconfirmed status")
                    except Exception as exc:
                        errors.append(f"cancel {order_id}: {exc!r}")
                if not errors:
                    leg["status"] = "cancelled"
                    leg["retired_ids"] = []
            if errors:
                return self._save(task, "position_cleanup_pending", "; ".join(errors))
            return self._save(task, "completed")

    def close_position(self, task_id: str, *, reason: str, allow_mutation: bool) -> str:
        """Flatten the venue position with a reduce-only market order, then cancel the legs.

        The order size is the contracts the venue still reports, never the
        bracket's opening size: a partially closed position must not be closed
        twice for the original amount.
        """
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                return "venue_mismatch"
            verdict = self._identity_verdict(task)
            if verdict is not None:
                return verdict
            return self._close_position_for_verified_account(
                task_id, reason=reason, allow_mutation=allow_mutation,
            )

    def _close_position_for_verified_account(self, task_id: str, *, reason: str, allow_mutation: bool) -> str:
        """Close logic; not reachable until account ownership is proven."""
        if not allow_mutation:
            return "mutation_gated"
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.get("venue_key") and task["venue_key"] != venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
            exchange = self.bridge.client()
            remaining = self._remaining_contracts(task)
            if remaining is None:
                self._save(task, task["status"], "venue position read failed; no close submitted")
                return "time_stop_unknown_position"
            if remaining <= 0:
                return self._cancel_resting_legs(
                    exchange, task, allow_cancel=True, terminal_status="time_stop_closed",
                )
            client_order_id = "timestop-" + hashlib.sha256(
                f"{task_id}:{reason}".encode()
            ).hexdigest()[:20]
            try:
                response = snapshot_observer.run(
                    self.bridge, exchange,
                    operation="time_stop_close", symbol=task["symbol"],
                    call=lambda: exchange.create_order(
                        task["symbol"], "market", task["close_side"], remaining,
                        None, {"reduceOnly": True, "clientOrderId": client_order_id},
                    ),
                )
            except ccxt.NetworkError as exc:
                self._save(task, "time_stop_submission_uncertain", f"close submission uncertain; inspect before retry: {exc!r}")
                return "time_stop_submission_uncertain"
            except Exception as exc:
                self._save(task, "time_stop_failed", f"close order failed: {exc!r}")
                return "time_stop_failed"
            if not isinstance(response, dict) or _state(response) != "filled":
                self._save(task, "time_stop_submission_uncertain", "close order was not confirmed filled; inspect before retry")
                return "time_stop_submission_uncertain"
            return self._cancel_resting_legs(
                exchange, task, allow_cancel=True, terminal_status="time_stop_closed",
            )

    def _cleanup_retired(self, exchange: Any, task: dict, allow_cancel: bool) -> list[str]:
        errors = []
        for leg in task["legs"]:
            pending = list(leg.get("retired_ids", []))
            for order_id in pending:
                try:
                    current = exchange.fetch_order(
                        leg["order_id"], task["symbol"], params=self._trigger_params,
                    )
                    evidence = {**leg, "replacement_price": leg["params"].get(
                        "stopLossPrice", leg["params"].get("takeProfitPrice")
                    ), "replacement_quantity": leg.get("quantity", task["quantity"]),
                        "replacement_order_id": leg["order_id"], "order_id": None}
                    if not isinstance(current, dict) or _state(current) != "placed" or not self._matches_leg(current, task, evidence):
                        errors.append(f"replacement {leg['order_id']} is not verified resting")
                        continue
                except Exception as exc:
                    errors.append(f"replacement inspection: {exc!r}")
                    continue
                if not allow_cancel:
                    errors.append(f"retired conditional cancellation gated off: {order_id}")
                    continue
                try:
                    response = snapshot_observer.run(
                        self.bridge, exchange,
                        operation="cancel_retired_bracket_leg", symbol=task["symbol"],
                        call=lambda oid=order_id: exchange.cancel_order(
                            oid, task["symbol"], params=self._trigger_params,
                        ),
                    )
                    if not _cancel_confirmed(response, str(order_id)):
                        raise ValueError("conditional cancel returned unconfirmed status")
                    leg["retired_ids"].remove(order_id)
                    self._save(task, "monitoring")
                except Exception as exc:
                    errors.append(f"cancel retired {order_id}: {exc!r}")
        return errors

    def _leg_state(self, exchange: Any, item: dict, task: dict, leg: dict) -> str | None:
        state = _state(item)
        raw_info = _info(item)
        if str(raw_info.get("algoStatus") or "").upper() not in {"FILLED", "FINISHED"}:
            return state

        child_id = raw_info.get("actualOrderId")
        quantity = leg.get("replacement_quantity", leg.get("repair_quantity", leg.get("quantity", task["quantity"])))
        try:
            positive_quantity = math.isfinite(float(quantity)) and float(quantity) > 0
        except (TypeError, ValueError):
            positive_quantity = False
        if (
            not child_id or str(child_id) == "0" or not positive_quantity
            or not _same_number(raw_info.get("actualQty"), quantity)
        ):
            return "transitioning"
        try:
            child = exchange.fetch_order(str(child_id), task["symbol"], params={})
        except Exception:
            return "transitioning"
        if not isinstance(child, dict):
            return "transitioning"
        child_info = _info(child)
        ids = [value for value in (child.get("id"), child_info.get("orderId")) if value is not None]
        symbols = [value for value in (child.get("symbol"), child_info.get("symbol")) if value is not None]
        sides = [value for value in (child.get("side"), child_info.get("side")) if value is not None]
        amounts = [value for value in (child.get("amount"), child_info.get("origQty")) if value is not None]
        filled = [value for value in (child.get("filled"), child_info.get("executedQty")) if value is not None]
        statuses = [value for value in (child.get("status"), child_info.get("status")) if value is not None]
        if (
            not ids or any(str(value) != str(child_id) for value in ids)
            or not symbols
            or (child.get("symbol") is not None and child["symbol"] != task["symbol"])
            or (child_info.get("symbol") is not None and child_info["symbol"] != task["symbol"].split(":", 1)[0].replace("/", ""))
            or not sides or any(str(value).lower() != task["close_side"] for value in sides)
            or not amounts or any(not _same_number(value, quantity) for value in amounts)
            or not filled or any(not _same_number(value, quantity) for value in filled)
            or not statuses or any(str(value).upper() not in {"FILLED", "CLOSED"} for value in statuses)
            or str(child_info.get("status") or "").upper() != "FILLED"
            or not _same_number(child_info.get("executedQty"), quantity)
        ):
            return "transitioning"
        return "filled"

    def _inspect(self, exchange: Any, task: dict, existing: list) -> list[str]:
        observed = {_order_id(item): item for item in existing if isinstance(item, dict) and _order_id(item)}
        errors = []
        for leg in task["legs"]:
            if leg["status"] == "cancelled":
                continue
            if leg["status"] == "filled":
                order_id = leg.get("order_id")
                if not order_id:
                    errors.append(f"filled {leg['name']} conditional order id is unknown")
                    continue
                try:
                    item = exchange.fetch_order(order_id, task["symbol"], params=self._trigger_params)
                    if (
                        not isinstance(item, dict) or not self._matches_leg_or_rounded(exchange, item, task, leg)
                        or self._leg_state(exchange, item, task, leg) != "filled"
                    ):
                        raise ValueError("filled conditional order is unconfirmed")
                except Exception as exc:
                    errors.append(f"{order_id}: {exc!r}")
                continue
            if leg["status"] == "uncertain":
                replacement_price = leg.get("replacement_price")
                repair_client_id = leg.get("repair_client_id")
                if repair_client_id:
                    repair_id = leg.get("order_id")
                    if repair_id:
                        try:
                            candidate = exchange.fetch_order(
                                repair_id, task["symbol"], params=self._trigger_params,
                            )
                        except Exception as exc:
                            errors.append(f"repair {repair_id}: {exc!r}")
                            continue
                    else:
                        candidates = [
                            item for item in existing
                            if isinstance(item, dict) and self._matches_leg(item, task, leg)
                        ]
                        if len(candidates) != 1:
                            errors.append(f"uncertain repair for {leg['name']}")
                            continue
                        candidate = candidates[0]
                        repair_id = _order_id(candidate)
                        leg["order_id"] = repair_id
                        self._save(task, task["status"])
                        try:
                            candidate = exchange.fetch_order(
                                repair_id, task["symbol"], params=self._trigger_params,
                            )
                        except Exception as exc:
                            errors.append(f"repair {repair_id}: {exc!r}")
                            continue
                    if not isinstance(candidate, dict) or not self._matches_leg_or_rounded(exchange, candidate, task, leg):
                        errors.append(f"repair {repair_id} identity mismatch")
                        continue
                    repair_state = self._leg_state(exchange, candidate, task, leg)
                    if repair_state == "cancelled":
                        leg.update(status="pending", order_id=None)
                        leg.pop("repair_client_id", None)
                        leg.pop("repair_quantity", None)
                        self._save(task, "pending", "cancelled repair may be retried")
                        continue
                    if repair_state in {"placed", "filled"}:
                        leg.update(
                            status=repair_state,
                            order_id=repair_id,
                            quantity=leg["repair_quantity"],
                        )
                        leg.pop("repair_client_id", None)
                        leg.pop("repair_quantity", None)
                        self._save(task, task["status"])
                        # The earlier open-order snapshot cannot supersede this read.
                        continue
                    else:
                        errors.append(f"repair {repair_id} is not confirmed resting")
                        continue
                elif replacement_price is not None:
                    replacement_id = leg.get("replacement_order_id")
                    if replacement_id:
                        try:
                            candidate = exchange.fetch_order(
                                replacement_id, task["symbol"], params=self._trigger_params,
                            )
                        except Exception as exc:
                            errors.append(f"replacement {replacement_id}: {exc!r}")
                            continue
                    else:
                        candidates = [
                            item for item in existing
                            if isinstance(item, dict) and self._matches_leg(item, task, leg)
                        ]
                        if len(candidates) != 1 or not leg.get("replacement_client_id"):
                            errors.append(f"uncertain replacement for {leg['name']}")
                            continue
                        candidate = candidates[0]
                        replacement_id = _order_id(candidate)
                        leg["replacement_order_id"] = replacement_id
                        self._save(task, task["status"])
                        try:
                            candidate = exchange.fetch_order(
                                replacement_id, task["symbol"], params=self._trigger_params,
                            )
                        except Exception as exc:
                            errors.append(f"replacement {replacement_id}: {exc!r}")
                            continue
                    if not isinstance(candidate, dict) or not self._matches_leg_or_rounded(exchange, candidate, task, leg):
                        errors.append(f"replacement {replacement_id} identity mismatch")
                        continue
                    candidate_state = _state(candidate)
                    if candidate_state == "cancelled":
                        for key in ("replacement_price", "replacement_quantity", "price_key",
                                    "replacement_order_id", "replacement_client_id"):
                            leg.pop(key, None)
                        leg["status"] = "placed"
                    elif candidate_state != "placed":
                        errors.append(f"replacement {replacement_id} is not resting")
                        continue
                    else:
                        old_id = leg.get("order_id")
                        if old_id:
                            leg["retired_ids"] = list(dict.fromkeys(
                                [*leg.get("retired_ids", []), old_id]
                            ))
                        leg["order_id"] = replacement_id
                        leg["params"] = {leg["price_key"]: replacement_price}
                        leg["quantity"] = leg["replacement_quantity"]
                        for key in ("replacement_price", "replacement_quantity", "price_key",
                                    "replacement_order_id", "replacement_client_id"):
                            leg.pop(key, None)
                        leg["status"] = "placed"
                        self._save(task, task["status"])
                        continue
                elif not leg.get("order_id"):
                    candidates = [item for item in existing if isinstance(item, dict) and self._matches_leg(item, task, leg)]
                    if len(candidates) != 1:
                        errors.append(f"uncertain {leg['name']} conditional submission")
                        continue
                    leg["order_id"] = _order_id(candidates[0])
                    self._save(task, task["status"])
                    try:
                        fetched = exchange.fetch_order(
                            leg["order_id"], task["symbol"], params=self._trigger_params,
                        )
                        if not isinstance(fetched, dict) or not self._matches_leg_or_rounded(exchange, fetched, task, leg):
                            raise ValueError("conditional order identity is unconfirmed")
                        fetched_state = self._leg_state(exchange, fetched, task, leg)
                        if fetched_state not in {"placed", "filled"}:
                            raise ValueError("conditional order not independently confirmed")
                    except Exception as exc:
                        errors.append(f"uncertain {leg['name']} conditional submission: {exc!r}")
                        continue
                    leg["status"] = fetched_state
                    continue
                else:
                    try:
                        fetched = exchange.fetch_order(
                            leg["order_id"], task["symbol"], params=self._trigger_params,
                        )
                        if not isinstance(fetched, dict) or not self._matches_leg_or_rounded(exchange, fetched, task, leg):
                            raise ValueError("conditional order identity is unconfirmed")
                        fetched_state = self._leg_state(exchange, fetched, task, leg)
                        if fetched_state not in {"placed", "filled"}:
                            raise ValueError("conditional order not independently confirmed")
                    except Exception as exc:
                        errors.append(f"uncertain {leg['name']} conditional submission: {exc!r}")
                        continue
                    leg["status"] = fetched_state
                    continue
            if not leg.get("order_id"):
                candidates = [item for item in observed.values() if self._matches_leg(item, task, leg)]
                if len(candidates) > 1:
                    errors.append(f"ambiguous {leg['name']} conditional orders")
                    continue
                if candidates:
                    leg["order_id"] = _order_id(candidates[0])
            order_id = leg.get("order_id")
            if not order_id:
                continue
            try:
                item = observed.get(order_id)
                if item is None:
                    item = exchange.fetch_order(order_id, task["symbol"], params=self._trigger_params)
                if not isinstance(item, dict) or not self._matches_leg_or_rounded(exchange, item, task, leg):
                    raise ValueError(f"unconfirmed identity for conditional order {order_id}")
                item_state = self._leg_state(exchange, item, task, leg)
                if item_state in {None, "transitioning"}:
                    raise ValueError(f"unconfirmed status for conditional order {order_id}")
                leg["status"] = item_state
            except Exception as exc:
                errors.append(f"{order_id}: {exc!r}")
        return errors

    def _settle_ladder(self, task: dict, winner: dict, allow_cancel: bool) -> str:
        """One tranche filled: keep the rest of the ladder and resize the stop.

        Cancelling the siblings here would throw away a deliberate scale-out
        plan, so only the stop is resized to the remaining contracts.
        """
        self._save(task, "monitoring")
        remaining = self._remaining_contracts(task)
        if remaining is None:
            return self._save(task, "monitoring", "venue position unknown; ladder resize deferred")
        if remaining <= 0:
            return self._save(task, "completed")
        stop = next((leg for leg in task["legs"] if leg["name"] == "stop-loss"), None)
        if stop is None:
            return self._save(task, "monitoring", "ladder has no stop to resize")
        price = stop["params"].get("stopLossPrice")
        current = float(stop.get("quantity", task["quantity"]))
        if not _same_number(current, remaining):
            status = self.replace_leg(
                task["task_id"], "stop-loss", trigger_price=float(price),
                quantity=remaining, allow_mutation=allow_cancel,
            )
            if status not in {"replaced", "unchanged"}:
                return status
        # replace_leg already persisted the new stop; saving this stale copy
        # would write the old order id and quantity back over it.
        return "monitoring"

    def _remaining_contracts(self, task: dict) -> float | None:
        """Return a uniquely observed positive position, never infer flat."""
        try:
            from tradingagents.execution.futures_account import observe_futures_positions

            if task.get("venue_key") != venue_key(self.bridge):
                return None
            if task.get("close_side") not in {"sell", "buy"}:
                return None
            side = "long" if task["close_side"] == "sell" else "short"
            observation = observe_futures_positions(self.bridge)
            if observation.unknown_scope or task["symbol"] in observation.unknown_symbols:
                return None
            matches = [
                position for position in observation.positions
                if position.symbol == task["symbol"] and position.side == side
            ]
            if len(matches) != 1 or matches[0].contracts is None:
                return None
            return float(matches[0].contracts)
        except Exception:
            return None

    def _cleanup(self, exchange: Any, task: dict, winner: dict, allow_cancel: bool) -> str:
        errors = []
        for leg in task["legs"]:
            if leg is winner or leg["status"] in {"filled", "cancelled"}:
                continue
            if leg["status"] == "pending" and not leg.get("order_id"):
                leg["status"] = "cancelled"
                continue
            if not allow_cancel:
                errors.append("sibling cancellation is gated off")
                continue
            try:
                response = snapshot_observer.run(
                    self.bridge, exchange,
                    operation="cancel_bracket_leg", symbol=task["symbol"],
                    call=lambda oid=leg["order_id"]: exchange.cancel_order(
                        oid, task["symbol"], params=self._trigger_params,
                    ),
                )
                if not _cancel_confirmed(response, leg["order_id"]):
                    raise ValueError("conditional cancel returned unconfirmed status")
                leg["status"] = "cancelled"
                self._save(task, "monitoring")
            except Exception as exc:
                errors.append(f"cancel {leg['order_id']}: {exc!r}")
        if errors:
            self._save(task, "monitoring", "; ".join(errors))
            return "reconcile_incomplete"
        return self._save(task, f"reconciled_{winner['name'].replace('-', '_')}")

    def _repair(self, exchange: Any, task: dict) -> str:
        task["attempts"] += 1
        # Legs submit at their own recorded size, capped to the contracts the
        # venue still reports: a tranche that already filled makes the original
        # bracket size a reduce-only over-close the venue rejects (-2022).
        remaining = self._remaining_contracts(task)
        if remaining is None:
            # A failed venue read leaves the position size unknown; sizing the
            # close from the opening quantity could over-close. The next poll
            # retries with a real read.
            return self._save(
                task, task["status"], "venue position read failed; protective repair deferred",
            )
        try:
            for leg in task["legs"]:
                if leg["status"] != "pending":
                    continue
                quantity = min(float(leg.get("quantity", task["quantity"])), float(remaining))
                repair_client_id = leg.get("repair_client_id") or "br-" + secrets.token_hex(12)
                leg.update(
                    status="uncertain", repair_client_id=repair_client_id,
                    repair_quantity=quantity,
                )
                self._save(
                    task, "submission_uncertain",
                    f"{leg['name']} repair intent requires venue inspection",
                )
                leg_params = self._conditional_params({
                    **leg["params"], "reduceOnly": True,
                    "clientOrderId": repair_client_id,
                })
                response = snapshot_observer.run(
                    self.bridge, exchange,
                    operation="repair_bracket_leg", symbol=task["symbol"],
                    call=lambda q=quantity, p=leg_params: exchange.create_order(
                        task["symbol"], "market", task["close_side"], q, None, p,
                    ),
                )
                leg_id = _order_id(response)
                if not leg_id:
                    leg.update(status="uncertain", order_id=None)
                    self._save(
                        task, "submission_uncertain",
                        f"{leg['name']} repair ACK has no id; inspect before retry",
                    )
                    return "reconcile_incomplete"
                leg.update(status="uncertain", order_id=leg_id, repair_quantity=quantity)
                self._save(
                    task, "submission_uncertain",
                    f"{leg['name']} repair ACK requires independent resting verification",
                )
                try:
                    confirmed = exchange.fetch_order(
                        leg_id, task["symbol"], params=self._trigger_params,
                    )
                except Exception as exc:
                    self._save(
                        task, "submission_uncertain",
                        f"{leg['name']} repair resting state unverified; inspect before retry: {exc!r}",
                    )
                    return "reconcile_incomplete"
                if not self._matches_leg_or_rounded(exchange, confirmed, task, leg):
                    self._save(
                        task, "submission_uncertain",
                        f"{leg['name']} repair read does not match its persisted intent",
                    )
                    return "reconcile_incomplete"
                # The venue acknowledges rounded values; the journal keeps what
                # the exchange itself reports so later polls match directly.
                if leg.get("quantity") is not None:
                    quantity = float(leg["quantity"])
                confirmed_state = self._leg_state(exchange, confirmed, task, leg)
                if confirmed_state == "cancelled":
                    leg.update(status="pending", order_id=None)
                    leg.pop("repair_client_id", None)
                    leg.pop("repair_quantity", None)
                    self._save(
                        task, "pending",
                        f"{leg['name']} repair was not resting; a later poll may retry",
                    )
                    return "reconcile_incomplete"
                if confirmed_state == "filled":
                    leg.update(status="filled", quantity=quantity)
                    leg.pop("repair_client_id", None)
                    leg.pop("repair_quantity", None)
                    self._save(
                        task, "submission_uncertain",
                        f"{leg['name']} repair is filled; sibling reconciliation is required",
                    )
                    return "reconcile_incomplete"
                if confirmed_state != "placed":
                    self._save(
                        task, "submission_uncertain",
                        f"{leg['name']} repair venue state {confirmed_state!r} is not resting",
                    )
                    return "reconcile_incomplete"
                leg.update(status="placed", quantity=quantity)
                leg.pop("repair_client_id", None)
                leg.pop("repair_quantity", None)
                self._save(task, "pending")
        except ccxt.NetworkError as exc:
            self._save(task, "submission_uncertain", f"protective submission is uncertain; inspect before retry: {exc!r}")
            return "reconcile_incomplete"
        except Exception as exc:
            # The submit failed for certain (venue rejection or local error, not
            # a lost ACK): the abandoned repair intent has no order to adopt, so
            # the legs return to pending before the emergency path runs.
            for leg in task["legs"]:
                if leg["status"] == "uncertain" and not leg.get("order_id") \
                        and leg.pop("repair_client_id", None) is not None:
                    leg.pop("repair_quantity", None)
                    leg["status"] = "pending"
            return self._emergency_close(exchange, task, exc)
        return self._save(task, "monitoring")

    def _cancel_resting_legs(
        self, exchange: Any, task: dict, allow_cancel: bool, *,
        terminal_status: str = "emergency_closed",
    ) -> str:
        if any(leg.get("replacement_price") is not None or leg.get("retired_ids") for leg in task["legs"]):
            return self._save(task, "emergency_cleanup_pending", "replacement cleanup is unresolved")
        errors = []
        for leg in task["legs"]:
            if leg["status"] in {"filled", "cancelled"}:
                continue
            order_id = leg.get("order_id")
            if not order_id:
                if leg["status"] == "pending":
                    leg["status"] = "cancelled"
                else:
                    errors.append(f"{leg['name']} conditional order id is unknown")
                continue
            if not allow_cancel:
                errors.append("conditional cleanup is gated off")
                continue
            try:
                response = snapshot_observer.run(
                    self.bridge, exchange,
                    operation="cancel_bracket_leg", symbol=task["symbol"],
                    call=lambda oid=order_id: exchange.cancel_order(
                        oid, task["symbol"], params=self._trigger_params,
                    ),
                )
                if not _cancel_confirmed(response, order_id):
                    raise ValueError("conditional cancel returned unconfirmed status")
                leg["status"] = "cancelled"
            except Exception as exc:
                errors.append(f"cancel {order_id}: {exc!r}")
        if errors:
            return self._save(task, "emergency_cleanup_pending", "; ".join(errors))
        return self._save(task, terminal_status)

    def _emergency_close(self, exchange: Any, task: dict, error: Exception) -> str:
        """Flatten with a reduce-only market order sized to the venue's remaining contracts.

        Like ``close_position``, the order size is what the venue still
        reports, never the bracket's opening size: after a filled tranche the
        original size is a reduce-only over-close the venue rejects (-2022),
        which would leave the position without any protection.
        """
        remaining = self._remaining_contracts(task)
        if remaining is None:
            self._save(
                task, "emergency_close_failed",
                f"protective recovery failed: {error!r}; venue position read failed, no close submitted",
            )
            return "emergency_close_failed"
        if remaining <= 0:
            return self._cancel_resting_legs(exchange, task, allow_cancel=True)
        client_order_id = "emergency-" + hashlib.sha256(task["task_id"].encode()).hexdigest()[:20]
        try:
            response = snapshot_observer.run(
                self.bridge, exchange,
                operation="emergency_close", symbol=task["symbol"],
                call=lambda: exchange.create_order(
                    task["symbol"], "market", task["close_side"], remaining,
                    None, {"reduceOnly": True, "clientOrderId": client_order_id},
                ),
            )
            if not isinstance(response, dict) or _state(response) != "filled":
                raise ValueError("emergency close was not confirmed filled")
        except Exception as exc:
            return self._save(task, "emergency_close_failed", f"protective recovery failed: {error!r}; emergency close failed: {exc!r}")
        return self._cancel_resting_legs(exchange, task, allow_cancel=True)
