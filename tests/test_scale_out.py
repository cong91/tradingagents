"""Offline tests for the deliberate scale-out planner."""

import pytest

from tradingagents.execution.scale_out import plan_scale_out


def test_long_scale_out_splits_by_position_size_and_leaves_a_runner():
    plan = plan_scale_out(
        side="long", entry=100.0, initial_stop=90.0, contracts=1.0,
        first_r=1.0, first_fraction=0.33, second_r=2.0, second_fraction=0.33,
    )

    assert [level.price for level in plan.levels] == pytest.approx([110.0, 120.0])
    assert [level.quantity for level in plan.levels] == pytest.approx([0.33, 0.33])
    assert plan.runner_quantity == pytest.approx(0.34)
    assert plan.levels[0].quantity + plan.levels[1].quantity + plan.runner_quantity == pytest.approx(1.0)


def test_short_scale_out_mirrors_the_levels():
    plan = plan_scale_out(
        side="short", entry=100.0, initial_stop=110.0, contracts=2.0,
        first_r=1.0, first_fraction=0.25, second_r=3.0, second_fraction=0.25,
    )

    assert [level.price for level in plan.levels] == pytest.approx([90.0, 70.0])
    assert [level.quantity for level in plan.levels] == pytest.approx([0.5, 0.5])
    assert plan.runner_quantity == pytest.approx(1.0)


def test_fractions_that_consume_the_whole_position_are_rejected():
    with pytest.raises(ValueError, match="runner"):
        plan_scale_out(
            side="long", entry=100.0, initial_stop=90.0, contracts=1.0,
            first_fraction=0.5, second_fraction=0.5,
        )


def test_r_levels_must_increase():
    with pytest.raises(ValueError, match="increasing"):
        plan_scale_out(
            side="long", entry=100.0, initial_stop=90.0, contracts=1.0,
            first_r=2.0, second_r=2.0,
        )


def test_stop_on_the_wrong_side_is_rejected():
    with pytest.raises(ValueError, match="protect"):
        plan_scale_out(side="long", entry=100.0, initial_stop=110.0, contracts=1.0)
