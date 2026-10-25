import pytest

from tradingagents.execution.profit_protection import evaluate_profit_protection


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
