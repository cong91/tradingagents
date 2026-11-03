"""Matching protective orders requires independent, non-conflicting evidence."""

from copy import deepcopy

import pytest

from tradingagents.execution.bracket_legs import matches

pytestmark = pytest.mark.unit

TASK = {"symbol": "BTC/USDT:USDT", "close_side": "sell", "quantity": 1}
LEG = {
    "name": "stop-loss", "params": {"stopLossPrice": 90}, "order_id": "old",
    "replacement_price": 95, "replacement_quantity": 1,
    "replacement_order_id": "new", "replacement_client_id": "client-new",
}
ORDER = {
    "id": "new", "symbol": "BTC/USDT:USDT", "side": "sell", "amount": 1,
    "info": {
        "algoId": "new", "symbol": "BTCUSDT", "side": "SELL", "reduceOnly": True,
        "orderType": "STOP_MARKET", "triggerPrice": 95, "quantity": 1,
        "workingType": "MARK_PRICE", "clientAlgoId": "client-new",
    },
}


def test_matching_replacement_is_accepted():
    assert matches(ORDER, TASK, LEG, require_mark_price=True)


@pytest.mark.parametrize("field,value", [
    ("workingType", "CONTRACT_PRICE"),
    ("workingType", None),
    ("clientAlgoId", "wrong-client"),
    ("clientAlgoId", None),
    ("triggerPrice", 90),
    ("quantity", 2),
    ("reduceOnly", False),
])
def test_replacement_rejects_missing_or_conflicting_evidence(field, value):
    order = deepcopy(ORDER)
    order["info"][field] = value
    assert not matches(order, TASK, LEG, require_mark_price=True)


def test_conflicting_client_ids_are_rejected_even_with_correct_order_id():
    order = deepcopy(ORDER)
    order["clientOrderId"] = "wrong-client"
    assert not matches(order, TASK, LEG, require_mark_price=True)


def test_known_old_order_id_does_not_bypass_order_parameters():
    leg = {"name": "stop-loss", "params": {"stopLossPrice": 90}, "order_id": "old"}
    order = deepcopy(ORDER)
    order["id"] = order["info"]["algoId"] = "old"
    assert not matches(order, TASK, leg, require_mark_price=True)
    order["info"]["triggerPrice"] = 90
    assert matches(order, TASK, leg, require_mark_price=True)


def test_venue_without_mark_price_requirement_still_checks_other_evidence():
    order = deepcopy(ORDER)
    del order["info"]["workingType"]
    assert matches(order, TASK, LEG)
    assert not matches(order, TASK, LEG, require_mark_price=True)
