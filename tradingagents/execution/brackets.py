"""Durable lifecycle of the protective exits attached to a futures entry."""

from __future__ import annotations

import hashlib
import math
import threading
from datetime import datetime, timezone
from typing import Any

import ccxt

from server import job_store
from tradingagents.execution.sizing import PlannedOrder

_TASK_LOCK = threading.RLock()
_TERMINAL_STATUSES = {
    "emergency_closed", "reconciled_take_profit",
    "reconciled_stop_loss", "cancelled", "completed",
}


def _venue_key(bridge: Any) -> str:
    config = bridge.config
    return "|".join((
        str(bridge.exchange_id).lower(),
        str(config.get("exec_base_url") or "").rstrip("/").lower(),
        str(config.get("exec_sandbox") is True),
    ))


def _info(item: dict) -> dict:
    value = item.get("info")
    return value if isinstance(value, dict) else item


def _order_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("id") or _info(item).get("algoId")
    return str(value) if value else None


def _state(item: dict) -> str | None:
    info = _info(item)
    raw = str(info.get("algoStatus") or item.get("status") or "").upper()
    if raw in {"FINISHED", "FILLED"}:
        return "filled"
    if raw == "CLOSED" and not info.get("algoStatus"):
        return "filled"
    if raw in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
        return "cancelled"
    if raw in {"NEW", "ACCEPTED", "TRIGGERING", "TRIGGERED", "PARTIALLY_FILLED", "OPEN"}:
        return "placed"
    return None


def _same_number(expected: Any, actual: Any) -> bool:
    try:
        left, right = float(expected), float(actual)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left) and math.isfinite(right) and math.isclose(
        left, right, rel_tol=1e-9, abs_tol=1e-12,
    )


def _matches(item: dict, task: dict, leg: dict) -> bool:
    item_id = _order_id(item)
    if not item_id:
        return False
    if leg.get("order_id") and leg.get("replacement_price") is None:
        return item_id == str(leg["order_id"])
    info = _info(item)
    if item.get("symbol") and item["symbol"] != task["symbol"]:
        return False
    if not item.get("symbol"):
        raw_symbol = task["symbol"].split(":", 1)[0].replace("/", "")
        if info.get("symbol") != raw_symbol:
            return False
    if str(info.get("side") or item.get("side") or "").lower() != task["close_side"]:
        return False
    if str(info.get("reduceOnly", item.get("reduceOnly"))).lower() not in {"true", "1"}:
        return False
    expected_type = "STOP_MARKET" if leg["name"] == "stop-loss" else "TAKE_PROFIT_MARKET"
    actual_type = str(info.get("orderType") or info.get("origType") or item.get("type") or "").upper()
    if expected_type != actual_type:
        return False
    expected_quantity = leg.get("replacement_quantity", leg.get("quantity", task["quantity"]))
    actual_quantity = info.get("quantity", item.get("amount"))
    trigger = info.get("triggerPrice") or info.get("stopPrice") or item.get("triggerPrice")
    price = leg.get("replacement_price", leg["params"].get("stopLossPrice", leg["params"].get("takeProfitPrice")))
    return _same_number(expected_quantity, actual_quantity) and _same_number(price, trigger)


def _cancel_confirmed(response: Any, order_id: str) -> bool:
    if not isinstance(response, dict) or _order_id(response) != order_id:
        return False
    if _state(response) == "cancelled":
        return True
    info = _info(response)
    return str(info.get("code")) == "200" and info.get("msg") == "success"


def conditional_params(exchange_id: str, params: dict) -> dict:
    """Trigger on the venue's mark price so policy and venue agree."""
    if exchange_id == "binance":
        return {**params, "workingType": "MARK_PRICE"}
    return params


def _is_ladder(task: dict) -> bool:
    return sum(1 for leg in task["legs"] if leg["name"].startswith("take-profit")) > 1


class BracketManager:
    """Protective-exit persistence and reconciliation, without an entry-order path."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge

    @property
    def _trigger_params(self) -> dict:
        return {"trigger": True} if self.bridge.exchange_id == "binance" else {}

    def _conditional_params(self, params: dict) -> dict:
        return conditional_params(self.bridge.exchange_id, params)

    def _save(self, task: dict, status: str, error: str | None = None) -> str:
        ids = [str(leg["order_id"]) for leg in task["legs"] if leg.get("order_id")]
        job_store.update_bracket_recovery(
            task["task_id"], status=status, placed_ids=ids, attempts=task["attempts"],
            last_error=self.bridge._redact(error) if error else None,
            now=datetime.now(timezone.utc).isoformat(), legs=task["legs"],
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

    def _place_legs(
        self,
        order: PlannedOrder,
        legs: list[dict],
        confirm: bool,
        entry_order_id: str | None,
    ) -> None:
        close_side = "sell" if order.side == "buy" else "buy"
        task_id = f"bracket:{order.ccxt_symbol}:{entry_order_id or 'unknown'}"
        with _TASK_LOCK:
            job_store.create_bracket_recovery(
                task_id, symbol=order.ccxt_symbol, entry_order_id=entry_order_id or "unknown",
                close_side=close_side, quantity=float(order.quantity), legs=legs,
                now=datetime.now(timezone.utc).isoformat(), venue_key=_venue_key(self.bridge),
            )
            task = job_store.load_bracket_recovery(task_id)
            position_key = f"{order.ccxt_symbol}:{order.side}:{entry_order_id or 'unknown'}"
            position_state = job_store.load_position_state(position_key)
            if position_state is not None and position_state.get("bracket_task_id") != task_id:
                position_state["bracket_task_id"] = task_id
                position_state["venue_key"] = _venue_key(self.bridge)
                job_store.save_position_state(
                    position_key, position_state["revision"], position_state,
                )
            exchange = self.bridge.client()
            try:
                for leg in task["legs"]:
                    if leg["status"] != "pending":
                        continue
                    quantity = float(leg.get("quantity", task["quantity"]))
                    response = exchange.create_order(
                        task["symbol"], "market", close_side, quantity,
                        None, self._conditional_params({**leg["params"], "reduceOnly": True}),
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
            self._save(task, "monitoring")
            self.bridge.audit_plan(
                order, action="bracket_placed", confirmed=confirm,
                order_id=",".join(str(leg["order_id"]) for leg in task["legs"]),
                reason="exchange-side conditional stopLoss/takeProfit attached after derivatives fill",
                phase="execute",
            )

    def reconcile(self, task_id: str, *, repair_missing: bool, allow_sibling_cancel: bool) -> str:
        with _TASK_LOCK:
            task = job_store.load_bracket_recovery(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["status"] in _TERMINAL_STATUSES:
                return task["status"]
            if task.get("venue_key") and task["venue_key"] != _venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
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
                for leg in task["legs"]:
                    if leg["status"] == "uncertain" and leg.get("order_id"):
                        leg["status"] = "placed"
                task["status"] = "pending"
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
            if task.get("venue_key") and task["venue_key"] != _venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
            leg = next((item for item in task["legs"] if item["name"] == leg_name), None)
            if leg is None:
                raise ValueError(f"unknown bracket leg {leg_name!r}")
            param = "stopLossPrice" if leg_name == "stop-loss" else "takeProfitPrice"
            old_id = leg.get("order_id")
            old_price = leg["params"].get(param)
            exchange = self.bridge.client()
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
            leg.update(
                status="uncertain", replacement_price=float(trigger_price),
                replacement_quantity=float(quantity), price_key=param,
            )
            self._save(task, task["status"])
            try:
                response = exchange.create_order(
                    task["symbol"], "market", task["close_side"], quantity,
                    None, self._conditional_params({param: trigger_price, "reduceOnly": True}),
                )
                new_id = _order_id(response)
                if not new_id:
                    raise RuntimeError(f"{leg_name} replacement returned no id")
            except ccxt.NetworkError as exc:
                self._save(task, "monitoring", f"replacement submission uncertain: {exc!r}")
                return "submission_uncertain"
            except Exception as exc:
                leg.pop("replacement_price", None)
                leg.pop("replacement_quantity", None)
                leg.pop("price_key", None)
                leg["status"] = "placed"
                self._save(task, "monitoring", f"replacement failed; existing leg retained: {exc!r}")
                return "replacement_failed"
            leg["retired_ids"] = list(dict.fromkeys([*leg.get("retired_ids", []), old_id])) if old_id else list(leg.get("retired_ids", []))
            leg.update(
                order_id=new_id, status="placed", params={param: float(trigger_price)},
                quantity=float(quantity),
            )
            leg.pop("replacement_price", None)
            leg.pop("replacement_quantity", None)
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
            if task.get("venue_key") and task["venue_key"] != _venue_key(self.bridge):
                self._save(task, task["status"], "configured venue/profile does not match the venue that placed this bracket")
                return "venue_mismatch"
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
                        response = exchange.cancel_order(
                            order_id, task["symbol"], params=self._trigger_params,
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

    def _cleanup_retired(self, exchange: Any, task: dict, allow_cancel: bool) -> list[str]:
        errors = []
        for leg in task["legs"]:
            pending = list(leg.get("retired_ids", []))
            for order_id in pending:
                if not allow_cancel:
                    errors.append(f"retired conditional cancellation gated off: {order_id}")
                    continue
                try:
                    response = exchange.cancel_order(
                        order_id, task["symbol"], params=self._trigger_params,
                    )
                    if not _cancel_confirmed(response, str(order_id)):
                        raise ValueError("conditional cancel returned unconfirmed status")
                    leg["retired_ids"].remove(order_id)
                    self._save(task, "monitoring")
                except Exception as exc:
                    errors.append(f"cancel retired {order_id}: {exc!r}")
        return errors

    def _inspect(self, exchange: Any, task: dict, existing: list) -> list[str]:
        observed = {_order_id(item): item for item in existing if isinstance(item, dict) and _order_id(item)}
        errors = []
        for leg in task["legs"]:
            if leg["status"] in {"filled", "cancelled"}:
                continue
            if leg["status"] == "uncertain":
                replacement_price = leg.get("replacement_price")
                if replacement_price is not None and not leg.get("replacement_order_id"):
                    candidates = [
                        item for item in existing
                        if isinstance(item, dict) and _matches(item, task, leg)
                    ]
                    if len(candidates) != 1:
                        errors.append(f"uncertain replacement for {leg['name']}")
                        continue
                    replacement_id = _order_id(candidates[0])
                    leg["replacement_order_id"] = replacement_id
                    leg["retired_ids"] = list(dict.fromkeys(
                        [*leg.get("retired_ids", []), leg["order_id"]]
                    ))
                    leg["order_id"] = replacement_id
                    leg["params"] = {leg["price_key"]: replacement_price}
                    leg["quantity"] = leg["replacement_quantity"]
                    leg.pop("replacement_price", None)
                    leg.pop("replacement_quantity", None)
                    leg.pop("replacement_order_id", None)
                    leg.pop("price_key", None)
                    leg["status"] = "placed"
                elif not leg.get("order_id"):
                    candidates = [item for item in existing if isinstance(item, dict) and _matches(item, task, leg)]
                    if len(candidates) != 1:
                        errors.append(f"uncertain {leg['name']} conditional submission")
                        continue
                    leg["order_id"] = _order_id(candidates[0])
                    leg["status"] = "placed"
                else:
                    leg["status"] = "placed"
            if not leg.get("order_id"):
                candidates = [item for item in observed.values() if _matches(item, task, leg)]
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
                if _order_id(item) != order_id or _state(item) is None:
                    raise ValueError(f"unconfirmed status for conditional order {order_id}")
                leg["status"] = _state(item)
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
                return self._save(task, "monitoring", f"ladder stop resize pending: {status}")
        # replace_leg already persisted the new stop; saving this stale copy
        # would write the old order id and quantity back over it.
        return "monitoring"

    def _remaining_contracts(self, task: dict) -> float:
        try:
            from tradingagents.execution.futures_account import read_futures_positions

            side = "long" if task["close_side"] == "sell" else "short"
            for position in read_futures_positions(self.bridge):
                if position.symbol == task["symbol"] and position.side == side:
                    return abs(float(position.contracts or 0.0))
        except Exception:
            return float(task["quantity"])
        return 0.0

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
                response = exchange.cancel_order(leg["order_id"], task["symbol"], params=self._trigger_params)
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
        try:
            for leg in task["legs"]:
                if leg["status"] != "pending":
                    continue
                response = exchange.create_order(
                    task["symbol"], "market", task["close_side"], task["quantity"],
                    None, self._conditional_params({**leg["params"], "reduceOnly": True}),
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
            self._save(task, "submission_uncertain", f"protective submission is uncertain; inspect before retry: {exc!r}")
            return "reconcile_incomplete"
        except Exception as exc:
            return self._emergency_close(exchange, task, exc)
        return self._save(task, "monitoring")

    def _cancel_resting_legs(self, exchange: Any, task: dict, allow_cancel: bool) -> str:
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
                response = exchange.cancel_order(
                    order_id, task["symbol"], params=self._trigger_params
                )
                if not _cancel_confirmed(response, order_id):
                    raise ValueError("conditional cancel returned unconfirmed status")
                leg["status"] = "cancelled"
            except Exception as exc:
                errors.append(f"cancel {order_id}: {exc!r}")
        if errors:
            return self._save(task, "emergency_cleanup_pending", "; ".join(errors))
        return self._save(task, "emergency_closed")

    def _emergency_close(self, exchange: Any, task: dict, error: Exception) -> str:
        client_order_id = "emergency-" + hashlib.sha256(task["task_id"].encode()).hexdigest()[:20]
        try:
            response = exchange.create_order(
                task["symbol"], "market", task["close_side"], task["quantity"],
                None, {"reduceOnly": True, "clientOrderId": client_order_id},
            )
            if not isinstance(response, dict) or _state(response) != "filled":
                raise ValueError("emergency close was not confirmed filled")
        except Exception as exc:
            return self._save(task, "emergency_close_failed", f"protective recovery failed: {error!r}; emergency close failed: {exc!r}")
        return self._cancel_resting_legs(exchange, task, allow_cancel=True)
