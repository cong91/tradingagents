"""Offline tests for adopting a pre-existing protective ladder."""

import pytest

from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.execution.position_adoption import plan_adoption


def _position(side="short", contracts=510.3, entry=2.0368):
    return FuturesPosition(
        symbol="ZRO/USDT:USDT", side=side, contracts=contracts,
        quantity=contracts if side == "long" else -contracts,
        entry_price=entry, mark_price=1.8553,
    )


def _leg(order_type, quantity, trigger, order_id):
    return {
        "id": order_id,
        "amount": quantity,
        "type": "market",
        "info": {
            "algoId": order_id,
            "orderType": order_type,
            "quantity": str(quantity),
            "triggerPrice": str(trigger),
        },
    }


def test_ladder_is_collapsed_into_weighted_full_size_legs():
    legs = [
        _leg("STOP_MARKET", 161.9, 2.2782, "s1"),
        _leg("STOP_MARKET", 190.3, 2.3126, "s2"),
        _leg("STOP_MARKET", 158.1, 2.4088, "s3"),
        _leg("TAKE_PROFIT_MARKET", 161.9, 1.55, "t1"),
        _leg("TAKE_PROFIT_MARKET", 190.3, 1.5514, "t2"),
        _leg("TAKE_PROFIT_MARKET", 158.1, 1.6684, "t3"),
    ]

    plan = plan_adoption(_position(), legs)

    assert plan.contracts == pytest.approx(510.3)
    assert plan.close_side == "buy"
    assert plan.stop == pytest.approx(
        (161.9 * 2.2782 + 190.3 * 2.3126 + 158.1 * 2.4088) / 510.3
    )
    assert plan.target == pytest.approx(
        (161.9 * 1.55 + 190.3 * 1.5514 + 158.1 * 1.6684) / 510.3
    )
    assert plan.dropped_legs == ()
    assert plan.stop_shortfall == 0.0


def test_excess_quantity_drops_the_least_favourable_legs():
    legs = [
        _leg("STOP_MARKET", 524.0, 0.7237, "s1"),
        _leg("STOP_MARKET", 506.0, 0.8160, "orphan"),
        _leg("TAKE_PROFIT_MARKET", 524.0, 0.5001, "t1"),
    ]

    plan = plan_adoption(_position(contracts=524.0), legs)

    # The higher (worse) stop is the one dropped for a short.
    assert plan.dropped_legs == ("orphan",)
    assert plan.kept_legs == ("s1", "t1")
    assert plan.stop == pytest.approx(0.7237)


def test_missing_stop_is_reported_as_shortfall_not_invented():
    legs = [_leg("TAKE_PROFIT_MARKET", 868.9, 0.263, "t1")]

    plan = plan_adoption(_position(contracts=868.9, entry=0.3522), legs)

    assert plan.stop is None
    assert plan.stop_shortfall == pytest.approx(868.9)
    assert plan.target == pytest.approx(0.263)


def test_missing_stop_is_restored_from_the_recorded_initial_stop():
    legs = [_leg("TAKE_PROFIT_MARKET", 868.9, 0.263, "t1")]

    plan = plan_adoption(
        _position(contracts=868.9, entry=0.3522), legs, fallback_stop=0.40972573,
    )

    assert plan.stop == pytest.approx(0.40972573)
    assert plan.stop_shortfall == 0.0


def test_recorded_stop_on_the_wrong_side_is_not_used():
    legs = [_leg("TAKE_PROFIT_MARKET", 868.9, 0.263, "t1")]

    plan = plan_adoption(
        _position(contracts=868.9, entry=0.3522), legs, fallback_stop=0.30,
    )

    assert plan.stop is None
    assert plan.stop_shortfall == pytest.approx(868.9)


def test_long_side_mirrors_the_drop_direction():
    legs = [
        _leg("STOP_MARKET", 5.0, 90.0, "low"),
        _leg("STOP_MARKET", 5.0, 95.0, "high"),
        _leg("TAKE_PROFIT_MARKET", 5.0, 130.0, "t1"),
    ]

    plan = plan_adoption(_position(side="long", contracts=5.0), legs)

    assert plan.close_side == "sell"
    # For a long the lower stop is the worse one.
    assert plan.dropped_legs == ("low",)
    assert plan.stop == pytest.approx(95.0)


def test_invalid_position_is_rejected():
    with pytest.raises(ValueError, match="contracts"):
        plan_adoption(_position(contracts=0), [])
