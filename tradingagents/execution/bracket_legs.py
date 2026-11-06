"""Pure bracket-leg status parsing and matching, without venue or store I/O.

Split from :mod:`tradingagents.execution.brackets` so the protective-exit
lifecycle owns only venue I/O, persistence and coordination, while leg status
semantics (accepted conditional statuses, order matching, cancel confirmation)
stay a pure seam that tests can exercise without any client.
"""

from __future__ import annotations

import math
from typing import Any

TERMINAL_STATUSES = {
    "emergency_closed", "reconciled_take_profit",
    "reconciled_stop_loss", "cancelled", "completed", "time_stop_closed",
}


def info(item: dict) -> dict:
    value = item.get("info")
    return value if isinstance(value, dict) else item


def order_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("id") or info(item).get("algoId")
    return str(value) if value else None


def state(item: dict) -> str | None:
    raw_info = info(item)
    algo_status = raw_info.get("algoStatus")
    raw = str(algo_status or item.get("status") or "").upper()
    if raw in {"FINISHED", "FILLED"}:
        # Conditional status alone cannot prove the child order filled.
        return "transitioning" if algo_status else "filled"
    if raw == "CLOSED" and not algo_status:
        return "filled"
    if raw in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
        return "cancelled"
    if raw in {"NEW", "ACCEPTED", "OPEN"}:
        return "placed"
    if raw in {"TRIGGERING", "TRIGGERED", "PARTIALLY_FILLED"}:
        return "transitioning"
    return None


def same_number(expected: Any, actual: Any) -> bool:
    try:
        left, right = float(expected), float(actual)
    except (TypeError, ValueError):
        return False
    return math.isfinite(left) and math.isfinite(right) and math.isclose(
        left, right, rel_tol=1e-9, abs_tol=1e-12,
    )


def matches(item: dict, task: dict, leg: dict, *, require_mark_price: bool = False) -> bool:
    item_id = order_id(item)
    if not item_id:
        return False
    if leg.get("replacement_price") is None:
        if leg.get("order_id") and item_id != str(leg["order_id"]):
            return False
    elif item_id == str(leg.get("order_id")):
        return False
    if leg.get("replacement_order_id") and item_id != str(leg["replacement_order_id"]):
        return False
    expected_client_id = (
        leg.get("replacement_client_id")
        or leg.get("repair_client_id")
        or (
            leg.get("client_order_id")
            if not leg.get("order_id") and not leg.get("replacement_order_id")
            else None
        )
    )
    if expected_client_id:
        client_ids = [value for value in (
            item.get("clientOrderId"), info(item).get("clientAlgoId"),
            info(item).get("clientOrderId"),
        ) if value is not None]
        if not client_ids or any(value != expected_client_id for value in client_ids):
            return False
    raw_info = info(item)
    if (
        item.get("id") is not None and raw_info.get("algoId") is not None
        and str(item["id"]) != str(raw_info["algoId"])
    ):
        return False
    unified_symbol = item.get("symbol")
    raw_symbol = raw_info.get("symbol")
    expected_symbol = task["symbol"].split(":", 1)[0].replace("/", "")
    if unified_symbol is None and raw_symbol is None:
        return False
    if unified_symbol is not None and unified_symbol != task["symbol"]:
        return False
    if raw_symbol is not None and raw_symbol != expected_symbol:
        return False
    sides = [value for value in (raw_info.get("side"), item.get("side")) if value is not None]
    if not sides or any(str(value).lower() != task["close_side"] for value in sides):
        return False
    flags = [value for value in (raw_info.get("reduceOnly"), item.get("reduceOnly")) if value is not None]
    if not flags or any(str(value).lower() not in {"true", "1"} for value in flags):
        return False
    working_types = [value for value in (raw_info.get("workingType"), item.get("workingType"))
                     if value is not None]
    if require_mark_price and (
        not working_types or any(str(value).upper() != "MARK_PRICE" for value in working_types)
    ):
        return False
    expected_type = "STOP_MARKET" if leg["name"] == "stop-loss" else "TAKE_PROFIT_MARKET"
    raw_types = [value for value in (raw_info.get("orderType"), raw_info.get("origType")) if value is not None]
    if raw_types:
        if any(str(value).upper() != expected_type for value in raw_types):
            return False
    elif str(item.get("type") or "").upper() != expected_type:
        return False
    expected_quantity = leg.get(
        "replacement_quantity", leg.get("repair_quantity", leg.get("quantity", task["quantity"]))
    )
    quantities = [value for value in (raw_info.get("quantity"), item.get("amount")) if value is not None]
    triggers = [value for value in (raw_info.get("triggerPrice"), raw_info.get("stopPrice"), item.get("triggerPrice")) if value is not None]
    price = leg.get("replacement_price", leg["params"].get("stopLossPrice", leg["params"].get("takeProfitPrice")))
    return (
        bool(quantities) and all(same_number(expected_quantity, value) for value in quantities)
        and bool(triggers) and all(same_number(price, value) for value in triggers)
    )


def cancel_confirmed(response: Any, order_id_value: str) -> bool:
    if not isinstance(response, dict) or order_id(response) != order_id_value:
        return False
    if state(response) == "cancelled":
        return True
    raw_info = info(response)
    return str(raw_info.get("code")) == "200" and raw_info.get("msg") == "success"


def conditional_params(exchange_id: str, params: dict) -> dict:
    """Trigger on the venue's mark price so policy and venue agree."""
    if exchange_id == "binance":
        return {**params, "workingType": "MARK_PRICE"}
    return params


def is_ladder(task: dict) -> bool:
    return sum(1 for leg in task["legs"] if leg["name"].startswith("take-profit")) > 1
