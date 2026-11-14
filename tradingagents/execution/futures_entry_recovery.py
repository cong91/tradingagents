"""Read-only futures entry reconciliation with fail-closed evidence checks.

This module deliberately owns no exchange mutations. The caller must already hold
``futures_operation_lock`` for the task symbol; this module only inspects venue
state and commits a verified recovery result through that same PostgreSQL guard
connection.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_TERMINAL_INTENT_STATUSES = frozenset({
    "completed",
    "cancelled",
    "rejected",
    "failed",
    "interrupted",
})
_ACTIVE_INTENT_STATUSES = frozenset({"pending", "submission_uncertain"})
_TERMINAL_BRACKET_STATUSES = frozenset({
    "reconciled_stop_loss",
    "reconciled_take_profit",
    "completed",
})


@dataclass(frozen=True, slots=True)
class _ClientEvidence:
    client: Any
    api_key: str
    secret: str
    profile: str
    venue_key: str


@dataclass(frozen=True, slots=True)
class _EntryEvidence:
    order_id: str
    client_order_id: str
    quantity: Decimal
    filled: Decimal
    average: Decimal
    time_ms: int
    item: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _TradeEvidence:
    opened_at: str
    net_quantity: Decimal
    fill_count: int
    entry_quantity: Decimal
    entry_average: Decimal


@dataclass(frozen=True, slots=True)
class _PositionEvidence:
    state: str
    position: Any | None


class _Rollback(RuntimeError):
    """Internal sentinel used to roll back a guarded transaction."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, str) and "_" in value:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _same(left: Any, right: Any) -> bool:
    left_decimal = _decimal(left)
    right_decimal = _decimal(right)
    if left_decimal is None or right_decimal is None:
        return False
    try:
        return math.isclose(float(left_decimal), float(right_decimal), rel_tol=1e-9, abs_tol=1e-12)
    except (OverflowError, ValueError):
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _info(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    value = item.get("info")
    return value if isinstance(value, dict) else {}


def _symbol_id(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").upper()


def _normal_symbol(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text is not None else None


def _raw_status(item: dict[str, Any]) -> str:
    raw = _info(item)
    return str(raw.get("status") or item.get("status") or "").upper()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_venue_key(bridge: Any) -> str | None:
    try:
        from tradingagents.execution.exchange_client import venue_key

        return venue_key(bridge)
    except (AttributeError, TypeError, ValueError):
        return None


def _profile_and_validate_client(bridge: Any, client: Any) -> tuple[str, str] | None:
    if str(getattr(bridge, "exchange_id", "")).lower() != "binance":
        return None
    config = getattr(bridge, "config", None)
    if not isinstance(config, dict):
        return None
    try:
        from server import binance_futures_positions
        from tradingagents.execution.exchange_client import _binance_profile

        base_url = config.get("exec_base_url")
        profile = _binance_profile(base_url)
        binance_futures_positions.validate_client(client, profile, base_url)
    except Exception:
        return None
    if getattr(client, "verify", None) is not True:
        return None
    if getattr(client, "validateServerSsl", None) is not True:
        return None
    venue = _safe_venue_key(bridge)
    if venue is None:
        return None
    return profile, venue


def _client_stable(bridge: Any, evidence: _ClientEvidence) -> bool:
    try:
        current = bridge.client()
    except Exception:
        return False
    if current is not evidence.client:
        return False
    if (
        getattr(current, "apiKey", None) != evidence.api_key
        or getattr(current, "secret", None) != evidence.secret
    ):
        return False
    selected = _profile_and_validate_client(bridge, current)
    return selected is not None and selected == (evidence.profile, evidence.venue_key)


def _stored_identity(record: dict[str, Any]):
    # Keep the identity import inside the function: brackets imports this module
    # indirectly through execution orchestration, so a module-level import cycles.
    from tradingagents.execution.brackets import stored_account_identity

    return stored_account_identity(record)


def _identity_verdict(bridge: Any, record: dict[str, Any]) -> str | None:
    stored = _stored_identity(record)
    if stored is None:
        return "account_identity_unknown"
    from tradingagents.execution import futures_identity

    try:
        comparison = futures_identity.compare_futures_account_identity(bridge, stored)
    except Exception:
        return "account_identity_unknown"
    if comparison is futures_identity.IdentityComparison.MISMATCH:
        return "account_identity_mismatch"
    if comparison is not futures_identity.IdentityComparison.MATCH:
        return "account_identity_unknown"
    return None


def _guard_for(symbol: str):
    from tradingagents.execution.futures_operation_lock import require_operation_lock

    return require_operation_lock(symbol)


def _task_ids(task: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (actual venue id, client id, diagnostic) from persisted intent IDs."""
    if not str(task.get("task_id") or "").startswith("entry-intent:"):
        return None, None, "task_not_entry_intent"
    symbol = _text(task.get("symbol"))
    client_ids: set[str] = set()
    actual_ids: set[str] = set()
    entry_order_id = _text(task.get("entry_order_id"))
    if entry_order_id:
        client_ids.add(entry_order_id)
    placed_ids = task.get("placed_ids")
    if placed_ids is not None and not isinstance(placed_ids, list):
        return None, None, "entry_ids_ambiguous"
    for value in placed_ids or []:
        text = _text(value)
        if text:
            actual_ids.add(text)
    legs = task.get("legs")
    if not isinstance(legs, list):
        return None, None, "entry_ids_ambiguous"
    entry_legs = [leg for leg in legs if isinstance(leg, dict) and leg.get("name") == "entry-intent"]
    if len(entry_legs) != 1:
        return None, None, "entry_ids_ambiguous"
    entry_leg = entry_legs[0]
    for key in ("client_order_id", "clientOrderId"):
        value = _text(entry_leg.get(key))
        if value:
            client_ids.add(value)
    value = _text(entry_leg.get("order_id"))
    if value:
        actual_ids.add(value)
    if len(client_ids) > 1 or len(actual_ids) > 1:
        return None, None, "entry_ids_ambiguous"
    client_id = next(iter(client_ids), None)
    actual_id = next(iter(actual_ids), None)
    if symbol is None or client_id is None and actual_id is None:
        return None, None, "pending_submission"
    expected_task_id = f"entry-intent:{symbol}:{client_id}" if client_id else None
    if expected_task_id is not None and task.get("task_id") != expected_task_id:
        return None, None, "entry_ids_ambiguous"
    return actual_id, client_id, None


def _fetch_entry(client: Any, symbol: str, actual_id: str | None, client_id: str | None):
    try:
        if actual_id:
            return client.fetch_order(actual_id, symbol, params={})
        if client_id:
            return client.fetch_order(client_id, symbol, params={"origClientOrderId": client_id})
    except Exception:
        return None
    return None


def _validate_entry_order(
    client: Any,
    task: dict[str, Any],
    item: Any,
    expected_actual_id: str | None,
    expected_client_id: str | None,
) -> _EntryEvidence | None:
    if not isinstance(item, dict):
        return None
    raw = _info(item)
    if not raw:
        return None
    unified_id = _text(item.get("id"))
    raw_id = _text(raw.get("orderId"))
    actual_id = unified_id or raw_id
    if actual_id is None or (unified_id and raw_id and unified_id != raw_id):
        return None
    if expected_actual_id and actual_id != expected_actual_id:
        return None
    unified_client = _text(item.get("clientOrderId"))
    raw_client = _text(raw.get("clientOrderId"))
    if unified_client and raw_client and unified_client != raw_client:
        return None
    actual_client = unified_client or raw_client
    if expected_client_id and actual_client != expected_client_id:
        return None
    if actual_client is None:
        return None
    symbol = _normal_symbol(task.get("symbol"))
    unified_symbol = _normal_symbol(item.get("symbol"))
    raw_symbol = _normal_symbol(raw.get("symbol"))
    if symbol is None or unified_symbol != symbol or raw_symbol != _symbol_id(symbol):
        return None
    entry_side = "buy" if str(task.get("close_side") or "").lower() == "sell" else "sell"
    unified_side = str(item.get("side") or "").lower()
    raw_side = str(raw.get("side") or "").lower()
    if unified_side != entry_side or raw_side != entry_side.upper().lower():
        return None
    if str(raw.get("type") or "").upper() != "MARKET":
        return None
    if str(item.get("type") or "").lower() != "market":
        return None
    if str(raw.get("positionSide") or "").upper() != "BOTH":
        return None
    if raw.get("reduceOnly") not in (False, 0, "false", "FALSE", "0"):
        return None
    raw_status = str(raw.get("status") or "").upper()
    unified_status = str(item.get("status") or "").lower()
    expected_statuses = {
        "FILLED": {"closed", "filled"},
        "CANCELED": {"canceled"},
        "EXPIRED": {"expired"},
        "EXPIRED_IN_MATCH": {"expired"},
    }
    if unified_status not in expected_statuses.get(raw_status, set()):
        return None

    original_quantity = _positive(raw.get("origQty"))
    executed = _decimal(raw.get("executedQty"))
    unified_amount = _positive(item.get("amount"))
    unified_filled = _decimal(item.get("filled"))
    expected_quantity = _positive(task.get("quantity"))
    if (
        original_quantity is None
        or executed is None
        or unified_amount is None
        or unified_filled is None
        or expected_quantity is None
        or executed <= 0
        or executed > original_quantity
        or not _same(unified_amount, original_quantity)
        or not _same(unified_filled, executed)
        or (raw_status == "FILLED" and not _same(executed, original_quantity))
        or (raw_status != "FILLED" and executed >= original_quantity)
    ):
        return None
    try:
        if callable(getattr(client, "load_markets", None)):
            client.load_markets()
        amount_precision = client.amount_to_precision(task["symbol"], float(expected_quantity))
    except Exception:
        return None
    if not _same(amount_precision, original_quantity):
        return None

    average = _positive(raw.get("avgPrice"))
    unified_average = _positive(item.get("average"))
    if average is None or unified_average is None or not _same(average, unified_average):
        return None
    unified_price = item.get("price")
    if unified_price not in (None, "") and not _same(unified_price, average):
        return None
    cum_quote = _positive(raw.get("cumQuote"))
    if cum_quote is None or not math.isclose(
        float(cum_quote), float(average * executed), rel_tol=1e-6, abs_tol=1e-8,
    ):
        return None
    time_value = _decimal(raw.get("time"))
    update_value = _decimal(raw.get("updateTime"))
    if (
        time_value is None
        or time_value <= 0
        or update_value is None
        or update_value < time_value
        or time_value != time_value.to_integral_value()
    ):
        return None
    return _EntryEvidence(
        order_id=actual_id,
        client_order_id=actual_client,
        quantity=original_quantity,
        filled=executed,
        average=average,
        time_ms=int(time_value),
        item=item,
    )


def _read_trade_evidence(client: Any, symbol: str, entry: _EntryEvidence, entry_side: str) -> _TradeEvidence | None:
    reader = getattr(client, "fapiPrivateGetUserTrades", None)
    if not callable(reader):
        return None
    market_id = _symbol_id(symbol)
    try:
        rows = reader({"symbol": market_id, "startTime": entry.time_ms, "limit": 1000})
    except Exception:
        return None
    if not isinstance(rows, list) or not rows or len(rows) >= 1000:
        return None
    seen_ids: set[str] = set()
    total_quantity = Decimal("0")
    total_quote = Decimal("0")
    first_time: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            return None
        trade_id = _text(row.get("id") or row.get("tradeId"))
        order_id = _text(row.get("orderId"))
        raw_symbol = _normal_symbol(row.get("symbol"))
        side = str(row.get("side") or "").lower()
        position_side = str(row.get("positionSide") or "").upper()
        quantity = _positive(row.get("qty") or row.get("quantity"))
        price = _positive(row.get("price"))
        timestamp = _decimal(row.get("time"))
        if (
            trade_id is None
            or trade_id in seen_ids
            or order_id != entry.order_id
            or raw_symbol != market_id
            or side != entry_side
            or position_side != "BOTH"
            or quantity is None
            or price is None
            or timestamp is None
            or timestamp <= 0
            or timestamp != timestamp.to_integral_value()
            or int(timestamp) < entry.time_ms
        ):
            return None
        seen_ids.add(trade_id)
        total_quantity += quantity
        total_quote += quantity * price
        timestamp_ms = int(timestamp)
        first_time = timestamp_ms if first_time is None else min(first_time, timestamp_ms)
    if first_time is None or not _same(total_quantity, entry.filled):
        return None
    average = total_quote / total_quantity
    if not math.isclose(float(average), float(entry.average), rel_tol=1e-6, abs_tol=1e-8):
        return None
    opened_at = datetime.fromtimestamp(first_time / 1000, UTC).isoformat()
    return _TradeEvidence(
        opened_at=opened_at,
        net_quantity=total_quantity,
        fill_count=len(rows),
        entry_quantity=total_quantity,
        entry_average=average,
    )



def _position_same(left: _PositionEvidence, right: _PositionEvidence) -> bool:
    if left.state != right.state:
        return False
    if left.state != "open" or left.position is None or right.position is None:
        return True
    for field in ("symbol", "side"):
        if getattr(left.position, field, None) != getattr(right.position, field, None):
            return False
    for field in ("contracts", "quantity", "entry_price"):
        left_value = getattr(left.position, field, None)
        right_value = getattr(right.position, field, None)
        if left_value is None or right_value is None or not _same(left_value, right_value):
            return False
    return True


def _position_evidence(bridge: Any, symbol: str, entry_side: str) -> _PositionEvidence | None:
    try:
        from tradingagents.execution.futures_account import observe_futures_positions

        observation = observe_futures_positions(bridge)
    except Exception:
        return None
    if (
        not observation.scope_complete
        or observation.unknown_scope
        or symbol in observation.unknown_symbols
    ):
        return None
    matches = [position for position in observation.positions if position.symbol == symbol]
    expected_side = "long" if entry_side == "buy" else "short"
    if any(position.side != expected_side for position in matches):
        return _PositionEvidence("mismatch", None)
    if len(matches) > 1:
        return _PositionEvidence("ambiguous", None)
    if not matches:
        return _PositionEvidence("flat", None)
    position = matches[0]
    contracts = _positive(getattr(position, "contracts", None))
    if contracts is None:
        contracts = _positive(abs(float(getattr(position, "quantity", 0.0))))
    if contracts is None:
        return _PositionEvidence("mismatch", None)
    return _PositionEvidence("open", position)


def _prepare_precision(client: Any, symbol: str, quantity: Decimal) -> tuple[str, str] | None:
    try:
        if callable(getattr(client, "load_markets", None)):
            client.load_markets()
        amount = client.amount_to_precision(symbol, float(quantity))
        return str(amount), ""
    except Exception:
        return None


def _leg_trigger(leg: dict[str, Any]) -> Any:
    params = leg.get("params")
    if not isinstance(params, dict):
        return None
    if leg.get("name") == "stop-loss":
        return params.get("stopLossPrice")
    return params.get("takeProfitPrice")


def _conditional_identity(
    item: Any,
    task: dict[str, Any],
    leg: dict[str, Any],
    client: Any,
    expected_trigger: Any,
    expected_quantity: Decimal,
    expected_state: str,
) -> bool:
    if not isinstance(item, dict):
        return False
    raw = _info(item)
    if not raw:
        return False
    order_id = _text(leg.get("order_id"))
    unified_id = _text(item.get("id"))
    raw_id = _text(raw.get("algoId"))
    if not order_id or not unified_id or not raw_id or unified_id != raw_id or raw_id != order_id:
        return False
    if _normal_symbol(item.get("symbol")) != _normal_symbol(task.get("symbol")):
        return False
    if _normal_symbol(raw.get("symbol")) != _symbol_id(task["symbol"]):
        return False
    if str(item.get("side") or "").lower() != str(task.get("close_side") or "").lower():
        return False
    if str(raw.get("side") or "").lower() != str(task.get("close_side") or "").lower():
        return False
    if raw.get("reduceOnly") not in (True, 1, "true", "TRUE", "1"):
        return False
    expected_type = "STOP_MARKET" if leg.get("name") == "stop-loss" else "TAKE_PROFIT_MARKET"
    if str(raw.get("orderType") or raw.get("origType") or "").upper() != expected_type:
        return False
    if str(raw.get("workingType") or "").upper() != "MARK_PRICE":
        return False
    quantity = _positive(raw.get("quantity"))
    amount = _positive(item.get("amount"))
    if quantity is None or amount is None or not _same(quantity, expected_quantity) or not _same(amount, quantity):
        return False
    trigger = raw.get("triggerPrice")
    if trigger is None:
        trigger = raw.get("stopPrice")
    item_trigger = item.get("triggerPrice")
    if trigger is None or item_trigger is None or not _same(trigger, expected_trigger) or not _same(item_trigger, trigger):
        return False
    raw_state = str(raw.get("algoStatus") or "").upper()
    unified_state = str(item.get("status") or "").lower()
    if expected_state == "placed":
        return raw_state in {"NEW", "ACCEPTED", "OPEN"} and unified_state in {"open", "new"}
    if expected_state == "cancelled":
        return raw_state in {"CANCELED", "CANCELLED"} and unified_state in {"canceled", "cancelled", "closed"}
    return raw_state in {"FILLED", "FINISHED"} and unified_state in {"closed", "filled"}


def _child_fill(
    client: Any,
    symbol: str,
    close_side: str,
    child_id: str,
    quantity: Decimal,
) -> bool:
    try:
        child = client.fetch_order(child_id, symbol, params={})
    except Exception:
        return False
    if not isinstance(child, dict):
        return False
    raw = _info(child)
    unified_id = _text(child.get("id"))
    raw_id = _text(raw.get("orderId"))
    if not unified_id or not raw_id or unified_id != raw_id or unified_id != child_id:
        return False
    if _normal_symbol(child.get("symbol")) != _normal_symbol(symbol):
        return False
    if _normal_symbol(raw.get("symbol")) != _symbol_id(symbol):
        return False
    if str(child.get("side") or "").lower() != close_side or str(raw.get("side") or "").lower() != close_side:
        return False
    if str(child.get("type") or "").lower() != "market" or str(raw.get("type") or "").upper() != "MARKET":
        return False
    if str(raw.get("status") or "").upper() != "FILLED" or str(child.get("status") or "").lower() not in {"closed", "filled"}:
        return False
    if raw.get("reduceOnly") not in (True, 1, "true", "TRUE", "1"):
        return False
    if str(raw.get("positionSide") or "").upper() != "BOTH":
        return False
    executed = _decimal(raw.get("executedQty"))
    filled = _decimal(child.get("filled"))
    amount = _positive(raw.get("origQty"))
    unified_amount = _positive(child.get("amount"))
    return (
        executed is not None
        and filled is not None
        and amount is not None
        and unified_amount is not None
        and _same(executed, quantity)
        and _same(filled, executed)
        and _same(amount, executed)
        and _same(unified_amount, amount)
    )


def _read_audit(path_value: Any, evidence: _EntryEvidence, bridge: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    path = Path(str(path_value or "")).expanduser()
    try:
        with open(path, encoding="utf-8") as handle:
            events = [
                json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                for line in handle if line.strip()
            ]
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    expected_side = "buy" if str(task.get("close_side") or "").lower() == "sell" else "sell"
    authoritative_actions = {"entry_uncertain", expected_side}
    matching = []
    for event in events:
        if not isinstance(event, dict):
            return None
        if (
            _text(event.get("order_id")) == evidence.order_id
            and event.get("action") in authoritative_actions
        ):
            matching.append(event)
    if len(matching) != 1:
        return None
    matched_event = matching[0]
    venue = _safe_venue_key(bridge)
    if venue is None:
        return None
    if (
        matched_event.get("phase") != "execute"
        or matched_event.get("confirmed") is not True
        or matched_event.get("market") != "derivatives"
        or matched_event.get("mode") != "live"
        or _text(matched_event.get("ccxt_symbol")) != task.get("symbol")
        or _text(matched_event.get("venue_key")) != venue
        or _text(matched_event.get("client_order_id")) not in {None, evidence.client_order_id}
    ):
        return None
    if _positive(matched_event.get("stop_loss")) is None or _positive(matched_event.get("take_profit")) is None:
        return None
    raw_entry_leg = next(
        (leg for leg in task.get("legs", [])
         if isinstance(leg, dict) and leg.get("name") == "entry-intent"),
        {},
    )
    journal_stop = _positive(raw_entry_leg.get("original_stop_loss"))
    journal_target = _positive(raw_entry_leg.get("take_profit"))
    if journal_stop is not None and not _same(matched_event.get("stop_loss"), journal_stop):
        return None
    if journal_target is not None and not _same(matched_event.get("take_profit"), journal_target):
        return None
    if matched_event.get("action") == "entry_uncertain":
        return matched_event
    if journal_stop is None or journal_target is None:
        return None
    if matched_event.get("action") != expected_side:
        return None
    if not _same(matched_event.get("qty"), evidence.filled):
        return None
    if not _same(matched_event.get("price_est"), evidence.average):
        return None
    return matched_event


def _verify_bracket(
    bridge: Any,
    client: Any,
    bracket: dict[str, Any],
    entry: _EntryEvidence,
    *,
    open_position: bool,
    audit_event: dict[str, Any] | None,
    entry_task: dict[str, Any],
) -> tuple[str, list[float]]:
    legs = bracket.get("legs")
    entry_leg = next(
        (leg for leg in entry_task.get("legs", []) if leg.get("name") == "entry-intent"),
        {},
    )
    if not isinstance(legs, list):
        return "bracket_evidence_incomplete", []
    stop_legs = [leg for leg in legs if isinstance(leg, dict) and leg.get("name") == "stop-loss"]
    target_legs = [leg for leg in legs if isinstance(leg, dict) and str(leg.get("name") or "").startswith("take-profit")]
    if len(stop_legs) != 1 or not target_legs:
        return "bracket_evidence_incomplete", []
    if not all(
        isinstance(leg, dict)
        and (leg.get("name") == "stop-loss" or leg.get("name") == "take-profit" or str(leg.get("name") or "").startswith("take-profit-"))
        for leg in legs
    ):
        return "bracket_evidence_incomplete", []
    planned_targets = {}
    if len(target_legs) > 1:
        policy = entry_leg.get("scale_out_policy")
        if not isinstance(policy, dict) or policy.get("enabled") is not True:
            return "bracket_evidence_incomplete", []
        try:
            from tradingagents.execution.scale_out import plan_scale_out

            plan = plan_scale_out(
                side="long" if bracket["close_side"] == "sell" else "short",
                entry=float(entry.average), initial_stop=float(entry_leg["original_stop_loss"]),
                contracts=float(entry.filled), first_r=float(policy["first_r"]),
                first_fraction=float(policy["first_fraction"]), second_r=float(policy["second_r"]),
                second_fraction=float(policy["second_fraction"]),
            )
        except (KeyError, TypeError, ValueError):
            return "bracket_evidence_incomplete", []
        planned_targets = {f"take-profit-{index}": level for index, level in enumerate(plan.levels, 1)}
        if {leg.get("name") for leg in target_legs} != set(planned_targets):
            return "bracket_evidence_incomplete", []
        target_legs.sort(key=lambda leg: leg["name"])
    if _positive(bracket.get("quantity")) is None or not _same(bracket.get("quantity"), entry.filled):
        return "bracket_evidence_incomplete", []
    try:
        amount_precision = client.amount_to_precision(bracket["symbol"], float(entry.filled))
    except Exception:
        return "bracket_evidence_incomplete", []
    if not _same(amount_precision, entry.filled):
        return "bracket_evidence_incomplete", []
    target_values: list[float] = []
    closed_quantity = Decimal("0")
    for leg in [*stop_legs, *target_legs]:
        expected_quantity = entry.filled
        planned_target = planned_targets.get(leg.get("name"))
        if planned_target is not None:
            try:
                expected_quantity = Decimal(client.amount_to_precision(bracket["symbol"], planned_target.quantity))
            except Exception:
                return "bracket_evidence_incomplete", []
        if not isinstance(leg, dict) or not _text(leg.get("order_id")):
            return "bracket_evidence_incomplete", []
        if open_position and (
            leg.get("retired_ids")
            or any(key in leg for key in ("replacement_price", "replacement_order_id", "replacement_quantity"))
        ):
            return "bracket_evidence_incomplete", []
        source_trigger = _leg_trigger(leg)
        if _positive(source_trigger) is None:
            return "bracket_evidence_incomplete", []
        if open_position and audit_event is not None:
            audit_trigger = (
                planned_target.price if planned_target is not None else
                audit_event.get("stop_loss") if leg.get("name") == "stop-loss" else audit_event.get("take_profit")
            )
            if _positive(audit_trigger) is None:
                return "bracket_evidence_incomplete", []
            try:
                expected_trigger = client.price_to_precision(bracket["symbol"], float(audit_trigger))
            except Exception:
                return "bracket_evidence_incomplete", []
            if not _same(source_trigger, expected_trigger):
                return "bracket_evidence_incomplete", []
        else:
            try:
                expected_trigger = client.price_to_precision(bracket["symbol"], float(source_trigger))
            except Exception:
                return "bracket_evidence_incomplete", []
        try:
            item = client.fetch_order(leg["order_id"], bracket["symbol"], params={"trigger": True})
        except Exception:
            return "bracket_evidence_incomplete", []
        expected_state = "placed" if open_position else ("filled" if leg.get("status") == "filled" else "cancelled")
        if expected_state == "filled":
            if not _conditional_identity(item, bracket, leg, client, expected_trigger, expected_quantity, "filled"):
                return "bracket_evidence_incomplete", []
            raw = _info(item)
            child_id = _text(raw.get("actualOrderId"))
            actual_qty = _positive(raw.get("actualQty"))
            if not child_id or child_id == "0" or actual_qty is None or not _same(actual_qty, expected_quantity):
                return "bracket_evidence_incomplete", []
            if not _child_fill(client, bracket["symbol"], bracket["close_side"], child_id, expected_quantity):
                return "bracket_evidence_incomplete", []
            closed_quantity += expected_quantity
        elif not _conditional_identity(item, bracket, leg, client, expected_trigger, expected_quantity, expected_state):
            return "bracket_evidence_incomplete", []
        if leg.get("name") != "stop-loss":
            try:
                target_values.append(float(expected_trigger))
            except (TypeError, ValueError):
                return "bracket_evidence_incomplete", []
    if open_position:
        if bracket.get("status") not in {"monitoring", "submission_uncertain", "pending"}:
            return "bracket_evidence_incomplete", []
        return "ok", target_values
    if bracket.get("status") not in _TERMINAL_BRACKET_STATUSES:
        return "bracket_evidence_incomplete", []
    if not _same(closed_quantity, entry.filled):
        return "bracket_evidence_incomplete", []
    return "ok", target_values


def _find_bracket(task: dict[str, Any], entry: _EntryEvidence, current_venue: str):
    try:
        from server import job_store

        rows = job_store.find_bracket_recoveries(task["symbol"], entry.order_id)
        active = job_store.active_bracket_recoveries()
    except Exception:
        return None, "bracket_unavailable"
    if not isinstance(rows, list) or len(rows) != 1:
        return None, "bracket_ambiguous"
    linked_id = f"bracket:{task['symbol']}:{entry.order_id}"
    competing = [
        item for item in active
        if isinstance(item, dict)
        and item.get("symbol") == task.get("symbol")
        and item.get("task_id") not in {task.get("task_id"), linked_id}
    ]
    if competing:
        return None, "bracket_ambiguous"
    bracket = rows[0]
    if not isinstance(bracket, dict):
        return None, "bracket_ambiguous"
    if bracket.get("task_id") != f"bracket:{task['symbol']}:{entry.order_id}":
        return None, "bracket_ambiguous"
    if bracket.get("symbol") != task.get("symbol") or bracket.get("entry_order_id") != entry.order_id:
        return None, "bracket_ambiguous"
    if task.get("venue_key") not in (None, current_venue):
        return None, "venue_mismatch"
    if bracket.get("venue_key") != current_venue:
        return None, "venue_mismatch"
    task_identity = _stored_identity(task)
    bracket_identity = _stored_identity(bracket)
    if task_identity is None or bracket_identity is None or bracket_identity != task_identity:
        return None, "account_identity_unknown"
    if bracket.get("close_side") != task.get("close_side"):
        return None, "bracket_evidence_incomplete"
    return bracket, None


def entry_allows_exit_cleanup(bridge: Any, bracket: dict[str, Any]) -> bool:
    """Prove a linked entry cannot fill again before its exits are retired."""
    from server import job_store

    try:
        symbol = bracket["symbol"]
        guard = _guard_for(symbol)
        guard.check()
        intents = [
            task for task in job_store.active_bracket_recoveries()
            if task.get("symbol") == symbol
            and str(task.get("task_id") or "").startswith("entry-intent:")
        ]
        if not intents:
            return True
        if len(intents) != 1:
            return False
        task = intents[0]
        client = bridge.client()
        selected = _profile_and_validate_client(bridge, client)
        if selected is None or _identity_verdict(bridge, task) is not None:
            return False
        profile, venue = selected
        evidence = _ClientEvidence(
            client, str(getattr(client, "apiKey", "")), str(getattr(client, "secret", "")),
            profile, venue,
        )
        actual_id, client_id, diagnostic = _task_ids(task)
        if diagnostic is not None:
            return False
        item = _fetch_entry(client, symbol, actual_id, client_id)
        entry = _validate_entry_order(client, task, item, actual_id, client_id)
        if entry is None:
            return False
        linked, error = _find_bracket(task, entry, venue)
        if (
            error is not None or linked is None
            or linked["task_id"] != bracket["task_id"]
            or not _same(linked.get("quantity"), entry.filled)
            or not _same(bracket.get("quantity"), entry.filled)
        ):
            return False
        entry_side = "buy" if task["close_side"] == "sell" else "sell"
        position = _position_evidence(bridge, symbol, entry_side)
        guard.check()
        return (
            position is not None and position.state == "flat"
            and _client_stable(bridge, evidence)
            and _identity_verdict(bridge, task) is None
        )
    except Exception:
        return False


def _state_payload(
    symbol: str,
    entry_side: str,
    entry: _EntryEvidence,
    position: Any,
    stop: float,
    targets: list[float],
    bracket_task_id: str,
    venue: str,
    opened_at: str,
) -> tuple[str, dict[str, Any]]:
    position_key = f"{symbol}:{entry_side}:{entry.order_id}"
    mark = _positive(getattr(position, "mark_price", None))
    if mark is None:
        raise _Rollback("position mark is unavailable")
    snapshot = {
        "position_key": position_key,
        "side": "long" if entry_side == "buy" else "short",
        "entry": float(entry.average),
        "quantity": float(entry.filled),
        "stop": float(stop),
        "targets": list(targets),
        "mark": float(mark),
        "revision": 0,
        "break_even_armed": False,
        "trail_anchor": None,
        "completed_targets": [],
        "initial_stop": float(stop),
        "current_target": targets[0] if targets else None,
        "phase": "initial",
        "opened_at": opened_at,
        "observed_at": opened_at,
        "bracket_task_id": bracket_task_id,
        "venue_key": venue,
    }
    return position_key, {
        "snapshot": snapshot,
        "orders": [],
        "entry_order_id": entry.order_id,
        "bracket_task_id": bracket_task_id,
        "venue_key": venue,
    }


def _state_matches(row: dict[str, Any], expected: dict[str, Any], identity: Any) -> bool:
    if row.get("account_identity_exchange") != identity.exchange:
        return False
    if row.get("account_identity_profile") != identity.profile:
        return False
    if row.get("account_identity_type") != identity.account_type:
        return False
    if row.get("account_identity_hash") != identity.identity_hash:
        return False
    state = row.get("state")
    if not isinstance(state, dict):
        return False
    expected_snapshot = expected["snapshot"]
    actual_snapshot = state.get("snapshot")
    if not isinstance(actual_snapshot, dict):
        return False
    for key in ("side", "initial_stop", "targets"):
        actual = actual_snapshot.get(key)
        expected_value = expected_snapshot.get(key)
        if key == "initial_stop":
            if not _same(actual, expected_value):
                return False
        elif actual != expected_value:
            return False
    return (
        state.get("entry_order_id") == expected.get("entry_order_id")
        and state.get("bracket_task_id") == expected.get("bracket_task_id")
        and state.get("venue_key") == expected.get("venue_key")
        and _same(actual_snapshot.get("entry"), expected_snapshot.get("entry"))
        and _same(actual_snapshot.get("quantity"), expected_snapshot.get("quantity"))
    )


def _ensure_state(conn: Any, position_key: str, state: dict[str, Any], identity: Any) -> None:
    columns = (
        identity.exchange,
        identity.profile,
        identity.account_type,
        identity.identity_hash,
    )
    cursor = conn.execute(
        """INSERT INTO position_states(
               position_key, revision, state, updated_at,
               account_identity_exchange, account_identity_profile,
               account_identity_type, account_identity_hash)
           VALUES (%s, 0, %s::jsonb, %s, %s, %s, %s, %s)
           ON CONFLICT (position_key) DO NOTHING""",
        (position_key, _json(state), _now(), *columns),
    )
    if cursor.rowcount == 1:
        return
    row = conn.execute(
        """SELECT revision, state, account_identity_exchange, account_identity_profile,
                  account_identity_type, account_identity_hash
           FROM position_states WHERE position_key = %s""",
        (position_key,),
    ).fetchone()
    if row is None or not _state_matches(dict(row), state, identity):
        raise _Rollback("position state already exists with different evidence")


def _cas_complete(
    conn: Any,
    task: dict[str, Any],
    status: str,
    entry: _EntryEvidence,
    identity: Any,
    venue: str,
) -> None:
    legs = task.get("legs")
    if not isinstance(legs, list):
        raise _Rollback("entry intent legs are unavailable")
    entry_legs = [
        leg for leg in legs
        if isinstance(leg, dict) and leg.get("name") == "entry-intent"
    ]
    if len(entry_legs) != 1:
        raise _Rollback("entry intent leg is ambiguous")
    existing_order_id = _text(entry_legs[0].get("order_id"))
    if existing_order_id not in (None, entry.order_id):
        raise _Rollback("entry order ID changed during reconciliation")
    placed_ids = task.get("placed_ids")
    if placed_ids is not None and not isinstance(placed_ids, list):
        raise _Rollback("entry placed IDs are ambiguous")

    terminal_status = str(_info(entry.item).get("status") or "").upper()
    status_by_raw = {
        "CANCELED": "cancelled",
        "CANCELLED": "cancelled",
        "EXPIRED": "expired",
        "EXPIRED_IN_MATCH": "expired_in_match",
    }
    normalized_status = status_by_raw.get(terminal_status)
    entry_order_final = None
    if normalized_status is not None:
        account_identity = {
            "exchange": identity.exchange,
            "profile": identity.profile,
            "type": identity.account_type,
            "hash": identity.identity_hash,
        }
        entry_order_final = {
            "status": normalized_status,
            "order_id": entry.order_id,
            "client_order_id": entry.client_order_id,
            "symbol": task["symbol"],
            "venue_key": venue,
            "account_id": (
                f"{identity.exchange}:{identity.profile}:{identity.identity_hash}"
            ),
            "account_identity": account_identity,
            "original_quantity": str(entry.quantity),
            "filled_quantity": str(entry.filled),
        }

    completed_legs = []
    for leg in legs:
        completed_leg = dict(leg)
        if completed_leg.get("name") == "entry-intent":
            completed_leg["order_id"] = entry.order_id
            if entry_order_final is not None:
                completed_leg["entry_order_final"] = entry_order_final
        completed_legs.append(completed_leg)
    completed_placed_ids = list(placed_ids or [])
    if not any(_text(value) == entry.order_id for value in completed_placed_ids):
        completed_placed_ids.append(entry.order_id)

    cursor = conn.execute(
        """UPDATE bracket_recovery_tasks
           SET status = 'completed', last_error = NULL, updated_at = %s,
               legs = %s::jsonb, placed_ids = %s::jsonb
           WHERE task_id = %s AND symbol = %s AND entry_order_id = %s
             AND status = %s AND quantity = %s AND updated_at = %s
             AND legs = %s::jsonb AND placed_ids = %s::jsonb
             AND account_identity_exchange = %s
             AND account_identity_profile = %s
             AND account_identity_type = %s
             AND account_identity_hash = %s
             AND (venue_key IS NULL OR venue_key = %s)""",
        (
            _now(), _json(completed_legs), _json(completed_placed_ids),
            task["task_id"], task["symbol"], task["entry_order_id"], status,
            task["quantity"], task["updated_at"], _json(task["legs"]),
            _json(task.get("placed_ids") or []), task["account_identity_exchange"],
            task["account_identity_profile"], task["account_identity_type"],
            task["account_identity_hash"], _safe_venue_key_from_task(task),
        ),
    )
    if cursor.rowcount != 1:
        raise _Rollback("entry intent changed during reconciliation")


def _safe_venue_key_from_task(task: dict[str, Any]) -> str:
    return str(task.get("venue_key") or "")


def _append_recovery_audit(conn: Any, task: dict[str, Any], entry: _EntryEvidence, result: str, venue: str) -> None:
    event = {
        "timestamp": _now(),
        "action": "futures_entry_reconciled",
        "phase": "execute",
        "recovery_key": task["task_id"],
        "task_id": task["task_id"],
        "ccxt_symbol": task["symbol"],
        "order_id": entry.order_id,
        "client_order_id": entry.client_order_id,
        "venue_key": venue,
        "market": "derivatives",
        "result": result,
    }
    conn.execute(
        """INSERT INTO control_server_audit(payload)
           SELECT %s::jsonb
           WHERE NOT EXISTS (
               SELECT 1 FROM control_server_audit
               WHERE payload->>'action' = 'futures_entry_reconciled'
                 AND payload->>'recovery_key' = %s
           )""",
        (_json(event), task["task_id"]),
    )


def _finalize(
    bridge: Any,
    guard: Any,
    task: dict[str, Any],
    client_evidence: _ClientEvidence,
    entry: _EntryEvidence,
    bracket: dict[str, Any],
    state_data: tuple[str, dict[str, Any]] | None,
    identity: Any,
    result: str,
) -> str:
    if not _client_stable(bridge, client_evidence):
        return "client_changed"
    identity_verdict = _identity_verdict(bridge, task)
    if identity_verdict is not None:
        return identity_verdict
    try:
        with guard.connection.transaction():
            guard.check()
            if not _client_stable(bridge, client_evidence):
                raise _Rollback("client changed during reconciliation")
            if _identity_verdict(bridge, task) is not None:
                raise _Rollback("account identity changed during reconciliation")
            current = guard.connection.execute(
                "SELECT * FROM bracket_recovery_tasks WHERE task_id = %s", (task["task_id"],)
            ).fetchone()
            if current is None:
                raise _Rollback("entry intent disappeared during reconciliation")
            current_task = dict(current)
            if current_task.get("status") != task.get("status"):
                raise _Rollback("entry intent changed during reconciliation")
            bracket_id = f"bracket:{task['symbol']}:{entry.order_id}"
            current_bracket = guard.connection.execute(
                "SELECT * FROM bracket_recovery_tasks WHERE task_id = %s FOR UPDATE", (bracket_id,)
            ).fetchone()
            if current_bracket is None or dict(current_bracket) != bracket:
                raise _Rollback("linked bracket changed during reconciliation")
            if state_data is not None:
                guard.check()
                _ensure_state(guard.connection, state_data[0], state_data[1], identity)
            guard.check()
            _cas_complete(
                guard.connection, task, current_task["status"], entry, identity,
                client_evidence.venue_key,
            )
            guard.check()
            _append_recovery_audit(guard.connection, task, entry, result, client_evidence.venue_key)
    except _Rollback:
        return "stale_state"
    except Exception:
        return "lock_or_database_lost"
    return result


def reconcile_entry(bridge: Any, task_id: str) -> str:
    """Reconcile one persisted futures entry intent without venue mutation."""
    try:
        from server import job_store

        task = job_store.load_bracket_recovery(task_id)
    except Exception:
        return "task_unavailable"
    if task is None:
        return "task_not_found"
    if task.get("task_id") != task_id:
        return "task_not_found"
    symbol = _text(task.get("symbol"))
    if symbol is None:
        return "task_symbol_unknown"
    try:
        guard = _guard_for(symbol)
    except Exception:
        return "lock_required"
    if task.get("status") == "completed":
        return "already_reconciled"
    if task.get("status") not in _ACTIVE_INTENT_STATUSES:
        return str(task.get("status") or "task_status_unknown")
    if not task_id.startswith("entry-intent:"):
        return "task_not_entry_intent"

    venue = _safe_venue_key(bridge)
    if venue is None:
        return "venue_mismatch"
    if task.get("venue_key") not in (None, venue):
        return "venue_mismatch"
    selected = None
    original_client = None
    try:
        original_client = bridge.client()
        selected = _profile_and_validate_client(bridge, original_client)
    except Exception:
        original_client = None
    if selected is None or original_client is None:
        return "venue_mismatch"
    try:
        from server import binance_futures_positions

        client = binance_futures_positions.isolated_client(original_client)
        binance_futures_positions.validate_client(client, selected[0], bridge.config.get("exec_base_url"))
    except Exception:
        return "venue_mismatch"
    identity_verdict = _identity_verdict(bridge, task)
    if identity_verdict is not None:
        return identity_verdict
    identity = _stored_identity(task)
    if identity is None:
        return "account_identity_unknown"
    client_evidence = _ClientEvidence(
        client=original_client,
        api_key=str(getattr(client, "apiKey", "")),
        secret=str(getattr(client, "secret", "")),
        profile=selected[0],
        venue_key=selected[1],
    )
    actual_id, client_id, diagnostic = _task_ids(task)
    if diagnostic is not None:
        return diagnostic
    item = _fetch_entry(client, symbol, actual_id, client_id)
    if item is None:
        return "entry_order_unavailable"
    entry = _validate_entry_order(client, task, item, actual_id, client_id)
    if entry is None:
        return "entry_order_unverified"
    if not _client_stable(bridge, client_evidence):
        return "client_changed"
    bracket, bracket_error = _find_bracket(task, entry, venue)
    if bracket_error is not None:
        return bracket_error
    if bracket is None:
        return "bracket_ambiguous"
    entry_side = "buy" if task.get("close_side") == "sell" else "sell"
    first_position = _position_evidence(bridge, symbol, entry_side)
    if first_position is None:
        return "position_scope_unknown"
    if first_position.state in {"mismatch", "ambiguous"}:
        return "position_mismatch"
    audit_event = None
    state_data = None
    if first_position.state == "open":
        audit_event = _read_audit(getattr(bridge, "config", {}).get("exec_log_path"), entry, bridge, task)
        if audit_event is None:
            return "open_position_provenance_ambiguous"
        try:
            trade_evidence = _read_trade_evidence(client, symbol, entry, entry_side)
        except Exception:
            trade_evidence = None
        if trade_evidence is None:
            return "open_position_provenance_ambiguous"
        position_quantity = _positive(getattr(first_position.position, "contracts", None))
        position_entry = _positive(getattr(first_position.position, "entry_price", None))
        if (
            position_quantity is None
            or not _same(position_quantity, entry.filled)
            or position_entry is None
            or not math.isclose(
                float(position_entry), float(entry.average), rel_tol=1e-6, abs_tol=1e-8,
            )
            or not _same(trade_evidence.net_quantity, first_position.position.contracts)
        ):
            return "position_mismatch"
        status, target_values = _verify_bracket(
            bridge, client, bracket, entry, open_position=True, audit_event=audit_event, entry_task=task,
        )
        if status != "ok":
            return status
        try:
            stop = client.price_to_precision(symbol, float(audit_event["stop_loss"]))
            targets = [float(value) for value in target_values]
            opened_at = trade_evidence.opened_at
        except Exception:
            return "bracket_evidence_incomplete"
        try:
            state_data = _state_payload(
                symbol, entry_side, entry, first_position.position, float(stop), targets,
                bracket["task_id"], venue, opened_at,
            )
        except _Rollback:
            return "position_state_unavailable"

    else:
        status, _ = _verify_bracket(
            bridge, client, bracket, entry, open_position=False, audit_event=None, entry_task=task,
        )
        if status != "ok":
            return status
    final_position = _position_evidence(bridge, symbol, entry_side)
    if final_position is None:
        return "position_scope_unknown"
    if not _position_same(first_position, final_position):
        return "position_changed"
    if final_position.state == "open" and state_data is None:
        return "position_state_unavailable"
    return _finalize(
        bridge, guard, task, client_evidence, entry, bracket, state_data,
        identity, "completed",
    )
