"""Deterministic replay of futures confirmation rejections.

The fixture (tests/fixtures/futures_cycles_replay.json) is a verbatim export
of the production futures_cycles rows (551 rows, 77 with closed-candle
snapshots as of 2026-10-27). Replays are offline: they read only the
snapshotted closed candle pair and never fetch, so a run dated in the past
decides exactly as it stood that day (point-in-time integrity).

The counterfactual rule is analysis-only: the evidence so far (see
test_counterfactual_flips_would_not_have_reached_entry and
tests/fixtures/replay_report.json) does not justify changing the production
gate in server.autoloop._confirmation_state, and this suite does not assert
that it should.
"""

import pytest

from server import autoloop, futures_replay as replay

pytestmark = pytest.mark.unit


def test_fixture_loads_with_expected_shape():
    cycles = replay.load_cycles()
    assert len(cycles) == 551
    with_context = [cycle for cycle in cycles if cycle.had_snapshot]
    assert len(with_context) == 77
    # Every rebuilt context must carry a closed candle pair with the
    # directions the snapshot itself recorded.
    for cycle in with_context:
        context = cycle.context
        assert context["one_hour"].is_closed and context["four_hour"].is_closed
        assert context["one_hour_direction"] == replay._candle_direction(context["one_hour"])
        assert context["four_hour_direction"] == replay._candle_direction(context["four_hour"])


def test_baseline_rule_matches_production_confirmation_state():
    """The replay baseline must be exactly the production gate, not a copy."""
    cycles = [cycle for cycle in replay.load_cycles() if cycle.had_snapshot]
    for cycle in cycles:
        signal = cycle.signal or "Hold"
        assert replay.baseline_confirms(cycle.context, signal) == autoloop._confirmation_state(
            cycle.context, signal,
        )


def test_counterfactual_never_confirms_more_than_baseline_allows():
    """Fail-closed property: the counterfactual rejects a superset of baseline
    rejections except for bounded pullbacks, and never flips a doji or a
    4h-opposed cycle into a confirmation."""
    cycles = [cycle for cycle in replay.load_cycles() if cycle.had_snapshot]
    for cycle in cycles:
        signal = cycle.signal or "Hold"
        baseline_ok, baseline_reason = replay.baseline_confirms(cycle.context, signal)
        counterfactual_ok, counterfactual_reason = replay.htf_dominance_pullback_confirms(
            cycle.context, signal,
        )
        if counterfactual_ok and not baseline_ok:
            # The only sanctioned widening is a bounded pullback on an
            # agreeing higher timeframe.
            assert counterfactual_reason == "bounded_pullback"
            assert baseline_reason == "direction_mismatch"
        if baseline_ok:
            assert counterfactual_ok, counterfactual_reason


def test_counterfactual_flips_would_not_have_reached_entry():
    """All cycles the counterfactual would confirm (the baseline rejected) were
    recorded as proposal-phase rejections for intent reasons (agent_abstained,
    position_management_only, structured_terms_unavailable): even with a
    confirmed direction they would not have entered. This is the evidence that
    widening the gate changes no executed order on this sample."""
    outcomes = replay.replay_rule(
        [c for c in replay.load_cycles() if c.had_snapshot],
        replay.htf_dominance_pullback_confirms,
    )
    flips = [
        outcome for outcome in outcomes
        if outcome["rule_result"]
        and outcome["recorded_confirmation_reason"] == "direction_mismatch"
    ]
    assert len(flips) == 3
    entry_attempts = [
        outcome for outcome in flips if outcome["phase"] == "confirmation"
    ]
    assert entry_attempts == []
    assert all(outcome["phase"] == "proposal" for outcome in flips)


def test_counterfactual_rejects_fail_closed_on_missing_context():
    # Missing candle directions must reject with a named reason, never confirm.
    assert replay.htf_dominance_pullback_confirms({}, "Buy") == (
        False, "missing_candle_direction",
    )


def _candle(timeframe: str, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "symbol": "X/USDT:USDT", "timeframe": timeframe, "open_time": 0,
        "close_time": 0, "open": open_, "high": high, "low": low,
        "close": close, "volume": 0.0, "is_closed": True,
    }


def test_counterfactual_unit_edges():
    from server.futures_replay import htf_dominance_pullback_confirms as rule

    def context_from(one, four):
        context = {"one_hour": replay._candle_from_payload(one, "1h", "X"),
                   "four_hour": replay._candle_from_payload(four, "4h", "X")}
        context["one_hour_direction"] = replay._candle_direction(context["one_hour"])
        context["four_hour_direction"] = replay._candle_direction(context["four_hour"])
        return context

    # Aligned both timeframes.
    aligned = context_from(
        _candle("1h", 100, 105, 99, 104), _candle("4h", 95, 106, 94, 103),
    )
    assert rule(aligned, "Buy") == (True, "ltf_aligned")
    # Doji anywhere never confirms.
    doji = context_from(
        _candle("1h", 100, 105, 99, 100), _candle("4h", 95, 106, 94, 103),
    )
    assert rule(doji, "Buy") == (False, "doji_candle")
    # Higher timeframe against the signal never confirms (for a Buy).
    htf_bull_for_short = context_from(
        _candle("1h", 100, 105, 99, 104), _candle("4h", 95, 106, 94, 103),
    )
    assert rule(htf_bull_for_short, "Sell") == (False, "htf_direction_mismatch")
    # Bounded pullback: bullish 1h bounce inside a bearish 4h, retracement
    # within limits -> confirmed for a short. 1h close 99.4 vs 4h low 98.0
    # over an 8-point 4h range is a 0.175 retracement; the 1h body (0.5)
    # dominates the upper wick (0.1).
    bounded = context_from(
        _candle("1h", 98.9, 99.5, 98.6, 99.4), _candle("4h", 104.0, 106.0, 98.0, 99.5),
    )
    assert rule(bounded, "Sell") == (True, "bounded_pullback")
    # Too-deep pullback: 1h close retraces most of the 4h range.
    deep = context_from(
        _candle("1h", 100.0, 104.9, 99.5, 104.5), _candle("4h", 104.0, 105.0, 98.0, 99.5),
    )
    assert rule(deep, "Sell") == (False, "ltf_pullback_too_deep")
    # Wick-dominant pullback candle: shallow retracement but the upper wick
    # overwhelms the body (0.9 / 0.3 > 1.5).
    wicky = context_from(
        _candle("1h", 98.3, 99.5, 98.1, 98.6), _candle("4h", 104, 105, 98, 99.5),
    )
    assert rule(wicky, "Sell") == (False, "ltf_pullback_wick_dominant")


def test_replay_grouping_and_summary():
    cycles = [c for c in replay.load_cycles() if c.had_snapshot]
    outcomes = replay.replay_rule(cycles, replay.baseline_confirms)
    assert len(outcomes) == 77
    by_signal = replay.group_counts(outcomes, "signal")
    assert sum(by_signal.values()) == 77
    # Short signals dominate this sample; there is no long-side evidence.
    # group_counts keys are tuples of the requested fields.
    assert by_signal.get(("Underweight",), 0) == 55
    assert by_signal.get(("Sell",), 0) == 21
    assert by_signal.get(("Hold",), 0) == 1
    summary = replay.summary(outcomes)
    assert summary["confirmed_by_rule"] == 24
    assert summary["rejected_by_rule"] == 53
    assert summary["flips_to_reject"] == []
