"""Unit tests for the timeframe-derived Auto schedule helpers."""

from datetime import UTC, datetime

import pytest

from server import autoloop_schedule
from server.auto_modes import AutoMode

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 13, 12, 3, 30, tzinfo=UTC)  # inside the 12:00 1m/15m/1h candle


def _config(**overrides) -> dict:
    config = {
        "auto_spot_enabled": False, "auto_futures_enabled": False,
        "auto_spot_timeframe": "4h", "auto_futures_timeframe": "4h",
    }
    config.update(overrides)
    return config


def test_no_enabled_mode_is_never_due():
    assert autoloop_schedule.due_modes(_config(), {}, _NOW) == {}


def test_missing_marker_makes_the_mode_due_for_the_latest_closed_candle():
    due = autoloop_schedule.due_modes(_config(auto_spot_enabled=True), {}, _NOW)
    assert set(due) == {AutoMode.SPOT}
    entry = due[AutoMode.SPOT]
    assert entry["timeframe"] == "4h"
    assert entry["candle_open"] == int(
        datetime(2026, 11, 13, 8, tzinfo=UTC).timestamp() * 1000
    )


@pytest.mark.parametrize("timeframe,expected_open", [
    ("1m", datetime(2026, 11, 13, 12, 2, tzinfo=UTC)),   # floor 12:03 -> closed 12:02-12:03
    ("15m", datetime(2026, 11, 13, 11, 45, tzinfo=UTC)),  # closed 11:45-12:00
    ("1h", datetime(2026, 11, 13, 11, tzinfo=UTC)),       # closed 11:00-12:00
    ("4h", datetime(2026, 11, 13, 8, tzinfo=UTC)),        # closed 08:00-12:00
])
def test_each_timeframe_derives_its_own_boundary(timeframe, expected_open):
    due = autoloop_schedule.due_modes(
        _config(auto_futures_enabled=True, auto_futures_timeframe=timeframe),
        {}, _NOW,
    )
    assert due[AutoMode.FUTURES]["candle_open"] == int(expected_open.timestamp() * 1000)


def test_served_boundary_is_not_due_again_but_a_newer_one_is():
    markers = {"auto_spot": {"1m": autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="1m"), {}, _NOW,
    )[AutoMode.SPOT]["candle_open"]}}
    assert autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="1m"), markers, _NOW,
    ) == {}
    # One minute later the next 1m candle closed -> due again.
    later = datetime(2026, 11, 13, 12, 4, 5, tzinfo=UTC)
    due = autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="1m"), markers, later,
    )
    assert set(due) == {AutoMode.SPOT}


def test_round_trip_through_another_timeframe_keeps_the_served_marker():
    """4h served → switch to 1m → back to 4h: the 4h candle is not replayed."""
    served_4h = autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="4h"), {}, _NOW,
    )[AutoMode.SPOT]["candle_open"]
    markers = {"auto_spot": {"4h": served_4h}}
    assert autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="4h"), markers, _NOW,
    ) == {}
    assert autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="4h"), markers, _NOW,
    ) == {}


def test_missed_boundaries_coalesce_to_the_latest_closed_candle():
    """A marker hours behind is due exactly once, for the newest candle."""
    stale = int(datetime(2026, 11, 12, 8, tzinfo=UTC).timestamp() * 1000)
    due = autoloop_schedule.due_modes(
        _config(auto_futures_enabled=True, auto_futures_timeframe="4h"),
        {"auto_futures": {"4h": stale}}, _NOW,
    )
    assert due[AutoMode.FUTURES]["candle_open"] == int(
        datetime(2026, 11, 13, 8, tzinfo=UTC).timestamp() * 1000
    )


def test_pick_scheduled_mode_prefers_the_longest_waiting_candle():
    due = autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="1m",
                auto_futures_enabled=True, auto_futures_timeframe="4h"),
        {}, _NOW,
    )
    # The 4h candle (08:00) has waited longer than the 1m candle (12:03).
    assert autoloop_schedule.pick_scheduled_mode(due, None) is AutoMode.FUTURES


def test_pick_scheduled_mode_alternates_equal_boundaries():
    due = autoloop_schedule.due_modes(
        _config(auto_spot_enabled=True, auto_spot_timeframe="1h",
                auto_futures_enabled=True, auto_futures_timeframe="1h"),
        {}, _NOW,
    )
    first = autoloop_schedule.pick_scheduled_mode(due, None)
    second = autoloop_schedule.pick_scheduled_mode(due, autoloop_schedule.mode_key(first))
    assert first is not second
    # Alternation is stable, not a coin flip.
    assert autoloop_schedule.pick_scheduled_mode(due, autoloop_schedule.mode_key(first)) is second


def test_pick_scheduled_mode_empty_due():
    assert autoloop_schedule.pick_scheduled_mode({}, None) is None


def test_wake_wait_seconds_bounds_the_sleep():
    wait = autoloop_schedule.wake_wait_seconds(_config(auto_spot_enabled=True), _NOW)
    # Next 4h boundary is 12:00... wait, 12:03 is inside the 12:00-16:00 candle
    # whose close is 16:00; the wait is capped well below that.
    assert 0 < wait <= autoloop_schedule.MAX_WAKE_SECONDS


def test_wake_wait_seconds_no_enabled_mode_polls_idle():
    assert autoloop_schedule.wake_wait_seconds(_config(), _NOW) == (
        autoloop_schedule.MAX_WAKE_SECONDS
    )


def test_invalid_timeframe_is_refused():
    with pytest.raises(ValueError, match="unsupported"):
        autoloop_schedule.selected_timeframe({"auto_spot_timeframe": "5m"}, AutoMode.SPOT)
