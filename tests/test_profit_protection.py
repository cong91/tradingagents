import pytest

from tradingagents.execution.profit_protection import (
    ProfitProtectionDecision,
    evaluate_profit_protection,
)


def test_decision_keeps_positional_order_and_defaults_exit_reason_to_none():
    decision = ProfitProtectionDecision(90.0, 130.0, "unchanged")

    assert decision.stop == pytest.approx(90.0)
    assert decision.target == pytest.approx(130.0)
    assert decision.phase == "unchanged"
    assert decision.exit_reason is None


def test_long_position_locks_profit_and_trails_after_three_r():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=135.0,
    )

    assert result.stop == pytest.approx(125.0)
    assert result.target == pytest.approx(140.0)
    assert result.phase == "trailing"


def test_short_position_uses_mirrored_profit_lock_levels():
    result = evaluate_profit_protection(
        side="short",
        entry=100.0,
        initial_stop=110.0,
        current_stop=110.0,
        current_target=70.0,
        mark=65.0,
    )

    assert result.stop == pytest.approx(75.0)
    assert result.target == pytest.approx(60.0)
    assert result.phase == "trailing"


@pytest.mark.parametrize(
    ("side", "entry", "initial_stop", "mark", "expected_stop", "phase"),
    [
        ("long", 100.0, 90.0, 110.0, 100.0, "break_even"),
        ("long", 100.0, 90.0, 120.0, 110.0, "profit_lock"),
        ("short", 100.0, 110.0, 90.0, 100.0, "break_even"),
        ("short", 100.0, 110.0, 80.0, 90.0, "profit_lock"),
    ],
)
def test_profit_protection_advances_by_r_thresholds(
    side, entry, initial_stop, mark, expected_stop, phase
):
    result = evaluate_profit_protection(
        side=side,
        entry=entry,
        initial_stop=initial_stop,
        current_stop=initial_stop,
        current_target=130.0 if side == "long" else 70.0,
        mark=mark,
    )

    assert result.stop == pytest.approx(expected_stop)
    assert result.phase == phase


def test_pullback_never_loosens_an_existing_trailing_stop():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=125.0,
        current_target=140.0,
        mark=130.0,
    )

    assert result.stop == pytest.approx(125.0)
    assert result.target == pytest.approx(140.0)


@pytest.mark.parametrize("mark", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_mark_does_not_change_protection(mark):
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=95.0,
        current_target=130.0,
        mark=mark,
    )

    assert result.stop == pytest.approx(95.0)
    assert result.target == pytest.approx(130.0)
    assert result.phase == "unchanged"


def test_invalid_geometry_is_rejected():
    with pytest.raises(ValueError, match="initial stop"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=105.0,
            current_stop=105.0,
            current_target=130.0,
            mark=120.0,
        )


def test_small_trailing_updates_wait_for_minimum_stop_advance():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=121.0,
        current_target=140.0,
        mark=131.2,
        min_stop_move_r=0.1,
    )

    assert result.stop == pytest.approx(121.0)


def test_zero_minimum_stop_move_disables_the_filter():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=121.0,
        current_target=140.0,
        mark=131.2,
        min_stop_move_r=0.0,
    )

    assert result.stop == pytest.approx(121.2)


def test_time_stop_fires_past_the_deadline_below_minimum_r():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=105.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
    )

    assert result.stop == pytest.approx(90.0)
    assert result.target == pytest.approx(130.0)
    assert result.phase == "time_stop"
    assert result.exit_reason == "time_stop"


def test_time_stop_does_not_fire_once_minimum_r_is_reached():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=115.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
    )

    assert result.phase == "break_even"
    assert result.exit_reason is None


def test_time_stop_needs_a_configured_maximum_hold():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=105.0,
        held_hours=10_000.0,
        max_hold_hours=None,
        time_stop_min_r=1.0,
    )

    assert result.phase == "unchanged"
    assert result.exit_reason is None


def test_time_stop_needs_a_known_held_duration():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=105.0,
        held_hours=None,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
    )

    assert result.phase == "unchanged"
    assert result.exit_reason is None


@pytest.mark.parametrize(
    ("side", "initial_stop", "mark", "expected_stop"),
    [
        ("long", 90.0, 115.0, 100.2),
        ("short", 110.0, 85.0, 99.8),
    ],
)
def test_round_trip_fee_rate_shifts_break_even_threshold(
    side, initial_stop, mark, expected_stop
):
    result = evaluate_profit_protection(
        side=side,
        entry=100.0,
        initial_stop=initial_stop,
        current_stop=initial_stop,
        current_target=130.0 if side == "long" else 70.0,
        mark=mark,
        round_trip_fee_rate=0.002,
        min_stop_move_r=0.0,
    )

    assert result.stop == pytest.approx(expected_stop)
    assert result.phase == "break_even"
    assert result.exit_reason is None


@pytest.mark.parametrize(
    ("side", "initial_stop", "mark", "expected_stop", "expected_phase"),
    [
        ("long", 90.0, 110.0, 100.0, "break_even"),
        ("long", 90.0, 120.0, 110.0, "profit_lock"),
        ("short", 110.0, 90.0, 100.0, "break_even"),
        ("short", 110.0, 80.0, 90.0, "profit_lock"),
    ],
)
def test_zero_fee_rate_reproduces_fee_free_levels(
    side, initial_stop, mark, expected_stop, expected_phase
):
    result = evaluate_profit_protection(
        side=side,
        entry=100.0,
        initial_stop=initial_stop,
        current_stop=initial_stop,
        current_target=130.0 if side == "long" else 70.0,
        mark=mark,
        round_trip_fee_rate=0.0,
    )

    assert result.stop == pytest.approx(expected_stop)
    assert result.phase == expected_phase
    assert result.exit_reason is None


@pytest.mark.parametrize("rate", [-0.01, 1.0, 1.5, float("nan"), float("inf")])
def test_invalid_round_trip_fee_rate_is_rejected(rate):
    with pytest.raises(ValueError, match="fee rate"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=90.0,
            current_stop=90.0,
            current_target=130.0,
            mark=110.0,
            round_trip_fee_rate=rate,
        )


@pytest.mark.parametrize("max_hold_hours", [0, -1, float("nan"), float("inf")])
def test_invalid_max_hold_hours_is_rejected(max_hold_hours):
    with pytest.raises(ValueError, match="maximum hold duration"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=90.0,
            current_stop=90.0,
            current_target=130.0,
            mark=110.0,
            max_hold_hours=max_hold_hours,
        )


@pytest.mark.parametrize("held_hours", [-0.5, float("nan"), float("inf")])
def test_invalid_held_hours_is_rejected(held_hours):
    with pytest.raises(ValueError, match="held duration"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=90.0,
            current_stop=90.0,
            current_target=130.0,
            mark=110.0,
            held_hours=held_hours,
        )


@pytest.mark.parametrize("time_stop_min_r", [0, -1, float("nan"), float("inf")])
def test_invalid_time_stop_minimum_r_is_rejected(time_stop_min_r):
    with pytest.raises(ValueError, match="time-stop minimum R"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=90.0,
            current_stop=90.0,
            current_target=130.0,
            mark=110.0,
            time_stop_min_r=time_stop_min_r,
        )


@pytest.mark.parametrize(
    ("mark", "expected_phase"),
    [
        (95.0, "unchanged"),
        (110.0, "break_even"),
        (120.0, "profit_lock"),
        (135.0, "trailing"),
    ],
)
def test_exit_reason_stays_none_on_existing_branches(mark, expected_phase):
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=mark,
    )

    assert result.phase == expected_phase
    assert result.exit_reason is None


def test_positive_funding_holds_a_short_past_the_time_stop():
    result = evaluate_profit_protection(
        side="short",
        entry=100.0,
        initial_stop=110.0,
        current_stop=110.0,
        current_target=70.0,
        mark=105.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
        cumulative_funding_r=2.0,
        round_trip_fee_r=0.05,
    )

    # Favorable R is -0.5; funding adds +2.0 and the fee takes 0.05, so the
    # effective R of 1.45 holds the position past the deadline.
    assert result.exit_reason is None
    assert result.phase == "time_stop_blocked_by_funding"
    assert result.stop == pytest.approx(110.0)
    assert result.target == pytest.approx(70.0)


def test_negative_funding_closes_a_long_earlier_than_price_r_alone():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=111.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
        cumulative_funding_r=-0.5,
        round_trip_fee_r=0.05,
    )

    # Price R alone is 1.1 and would survive the deadline; funding paid and
    # the exit fee drag the effective R down to 0.55, below the minimum.
    assert result.exit_reason == "time_stop"
    assert result.phase == "time_stop"


def test_the_same_long_without_funding_survives_the_deadline():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=111.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
    )

    assert result.exit_reason is None
    assert result.phase == "break_even"


@pytest.mark.parametrize(
    ("cumulative_funding_r", "round_trip_fee_r", "expected"),
    [
        (1.0, 0.9, "time_stop"),      # fee sinks the effective R to 0.3
        (1.0, 0.0, "time_stop_blocked_by_funding"),
        (0.7, 0.0, "time_stop"),      # funding alone leaves effective R at 0.9
    ],
)
def test_round_trip_fee_r_drags_the_effective_r_down(
    cumulative_funding_r, round_trip_fee_r, expected
):
    result = evaluate_profit_protection(
        side="short",
        entry=100.0,
        initial_stop=110.0,
        current_stop=110.0,
        current_target=70.0,
        mark=98.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
        cumulative_funding_r=cumulative_funding_r,
        round_trip_fee_r=round_trip_fee_r,
    )

    # Favorable R is 0.2; with +1.0R funding the fee is what decides whether
    # the position survives the deadline.
    assert result.phase == expected
    assert result.exit_reason == ("time_stop" if expected == "time_stop" else None)


def test_break_even_level_stays_price_based_when_funding_blocks_the_time_stop():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=107.0,
        break_even_r=0.5,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
        cumulative_funding_r=0.5,
        round_trip_fee_r=0.05,
    )

    # Favorable R 0.7 tripped the raw boundary; funding rescued it. The stop
    # still advances to the price-based break-even level, untouched by funding.
    assert result.phase == "time_stop_blocked_by_funding"
    assert result.stop == pytest.approx(100.0)

    # Without the rescue the same position fires the time stop instead.
    stopped = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=107.0,
        break_even_r=0.5,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
    )

    assert stopped.exit_reason == "time_stop"


def test_trailing_level_stays_price_based_and_well_earned_profit_is_not_blocked():
    result = evaluate_profit_protection(
        side="long",
        entry=100.0,
        initial_stop=90.0,
        current_stop=90.0,
        current_target=130.0,
        mark=133.0,
        held_hours=80.0,
        max_hold_hours=72.0,
        time_stop_min_r=1.0,
        cumulative_funding_r=-0.2,
        round_trip_fee_r=0.1,
    )

    # Effective R is 3.0 at the trail start; the trailing stop keeps its
    # price-based distance and the phase stays trailing, not blocked.
    assert result.phase == "trailing"
    assert result.stop == pytest.approx(123.0)
    assert result.exit_reason is None


@pytest.mark.parametrize(
    "adjustment",
    [
        {"cumulative_funding_r": float("nan")},
        {"cumulative_funding_r": float("inf")},
        {"cumulative_funding_r": float("-inf")},
        {"round_trip_fee_r": float("nan")},
        {"round_trip_fee_r": float("inf")},
        {"round_trip_fee_r": float("-inf")},
        {"cumulative_funding_r": "junk"},
    ],
)
def test_non_finite_funding_adjustments_are_rejected(adjustment):
    with pytest.raises(ValueError, match="finite"):
        evaluate_profit_protection(
            side="long",
            entry=100.0,
            initial_stop=90.0,
            current_stop=90.0,
            current_target=130.0,
            mark=110.0,
            **adjustment,
        )
