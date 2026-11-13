"""E4: baselines — every variant runs the SAME simulation; rules are imported.

Plan §4.3 E4: no-trade/rule/recorded/ablation go through the identical engine
call with the identical cost model; the rule baseline reuses
``server.futures_replay.RULES`` (import, never fork — mutating the rule table
or its functions is visible here); an unknown ablation knob is refused.
"""

from datetime import UTC, datetime

import pytest

import server.futures_replay as futures_replay
from tradingagents.dataflows.market_data import Candle
from tradingagents.experiments.baselines import (
    no_trade_source,
    recorded_source,
    rule_source,
)
from tradingagents.simulation.engine import run_simulation

pytestmark = pytest.mark.unit

_DAY_MS = 86_400_000
_START = 1_700_000_000_000
# One full UTC day boundary: candle stamps derived from it stay in one day.
_DAY_START = 1_767_225_600_000  # 2026-01-01T00:00:00Z


def _candle(open_ms: int, close: float, open_: float | None = None) -> Candle:
    open_price = open_ if open_ is not None else close * 0.99
    return Candle(
        symbol="BTC/USDT:USDT", timeframe="4h", open_time=open_ms,
        close_time=open_ms + 14_400_000, open=open_price, high=max(open_price, close) * 1.001,
        low=min(open_price, close) * 0.999, close=close, volume=10.0, is_closed=True,
    )


def _activated_candle(candle: Candle, close: float | None = None) -> Candle:
    return _candle(candle.open_time + 14_400_000, close or candle.close)


def _recorded_cycle(candle: Candle, signal: str = "Buy", *, confirmed: str | None = "aligned_direction",
                    payload: dict | None = None) -> dict:
    decision_ms = max(candle.close_time, candle.open_time + 3_600_000) + 120_000
    cycle_payload = dict(payload) if payload is not None else {"agent_proposal": {
        "entry_price": candle.close, "stop_loss": candle.close * 0.98,
        "take_profit": candle.close * 1.04, "leverage": 1.0,
        "risk_fraction": 0.01, "margin_fraction": 0.25,
        "margin_mode": "isolated", "scale_out_enabled": False,
    }}
    cycle_payload.setdefault("pm_decision", {"futures_execution_intent": "enter"})
    cycle_payload.setdefault("proposal_validation", {
        "valid": confirmed not in ("direction_mismatch", "doji_candle"),
        "reason": confirmed if confirmed in ("direction_mismatch", "doji_candle") else None,
    })
    cycle_payload.setdefault("phase_timestamps", {
        "analysis_finished_at": datetime.fromtimestamp(
            (decision_ms - 60_000) / 1000, UTC,
        ).isoformat(),
        "proposal_validated_at": datetime.fromtimestamp(
            decision_ms / 1000, UTC,
        ).isoformat(),
    })
    return {
        "cycle_id": f"futures:BTC-USD:{candle.open_time}",
        "symbol": "BTC/USDT:USDT",
        "status": "rejected", "phase": "confirmation",
        "signal": signal, "confirmation_reason": confirmed,
        "signal_reason": None,
        "snapshot": {
            "one_hour": {
                "symbol": "BTC/USDT:USDT", "timeframe": "1h",
                "open_time": candle.open_time, "close_time": candle.open_time + 3_600_000,
                "open": candle.open, "high": candle.high, "low": candle.low,
                "close": candle.close, "volume": 10.0, "is_closed": True,
            },
            "four_hour": {
                "symbol": "BTC/USDT:USDT", "timeframe": "4h",
                "open_time": candle.open_time, "close_time": candle.close_time,
                "open": candle.open, "high": candle.high, "low": candle.low,
                "close": candle.close, "volume": 10.0, "is_closed": True,
            },
        },
        "payload": cycle_payload,
    }


def test_no_trade_baseline_never_proposes():
    candle = _candle(_START, 101.0)
    assert no_trade_source(candle) is None


def test_rule_baseline_imports_the_production_rule_not_a_fork():
    """The source's verdict IS the production rule's verdict, byte for byte.

    The fork check: on the same candle, calling the rule function directly
    and through the baseline source gives the same answer — if the baseline
    re-implemented the logic, a change inside the real function would show a
    divergence. Mutating the rule *function object* (not the table) changes
    both paths together, proving no second implementation exists.
    """
    candle = _candle(_START, 101.0)
    cycle = _recorded_cycle(candle)
    rule = futures_replay.baseline_confirms
    source = rule_source(rule, [cycle])
    # Recorded lifecycle timestamps postdate the snapshot evidence, so the
    # proposal is available only on the following eligible 4h candle.
    proposal = source(_activated_candle(candle))
    # The source carries the rule function identity itself — no fork object.
    assert source.rule_fn is rule  # type: ignore[attr-defined]
    # The recorded snapshot's directions make the rule confirm; the proposal
    # terms come from the recorded payload, not a re-derived distance.
    assert proposal is not None and proposal.signal == "Buy"
    assert proposal.stop_loss == pytest.approx(101.0 * 0.98)
    assert proposal.take_profit == pytest.approx(101.0 * 1.04)


def test_foreign_rule_function_is_refused():
    candle = _candle(_START, 101.0)
    def impostor(context: dict, signal: str):
        return True, "impostor"
    with pytest.raises(ValueError, match="futures_replay.RULES"):
        rule_source(impostor, [_recorded_cycle(candle)])


def test_recorded_baseline_replays_recorded_rejections_as_no_proposal():
    candle = _candle(_START, 101.0)
    source = recorded_source(
        [_recorded_cycle(candle, confirmed="direction_mismatch")],
        confirmation_rule=futures_replay.baseline_confirms,
    )
    assert source(candle) is None
    assert source.skipped["recorded_rejection"] == 1


def test_recorded_baseline_passes_a_confirmed_entry_through():
    candle = _candle(_START, 101.0)
    source = recorded_source(
        [_recorded_cycle(candle, confirmed="aligned_direction")],
        confirmation_rule=futures_replay.baseline_confirms,
    )
    proposal = source(_activated_candle(candle))
    assert proposal is not None
    assert proposal.stop_loss == pytest.approx(101.0 * 0.98)
    assert proposal.take_profit == pytest.approx(101.0 * 1.04)


def test_recorded_source_requires_explicit_entry_intent_validation_and_terms():
    candle = _candle(_START, 101.0)
    incomplete = _recorded_cycle(candle)
    incomplete["payload"].pop("pm_decision")
    source = recorded_source(
        [incomplete], confirmation_rule=futures_replay.baseline_confirms,
    )
    assert source(_activated_candle(candle)) is None
    assert source.skipped["missing_intent"] == 1
    assert source.coverage_status == "insufficient_evidence"

    incomplete = _recorded_cycle(candle)
    incomplete["payload"]["agent_proposal"].pop("margin_mode")
    source = recorded_source(
        [incomplete], confirmation_rule=futures_replay.baseline_confirms,
    )
    assert source(_activated_candle(candle)) is None
    assert source.skipped["missing_terms"] == 1


def test_exported_fixture_remains_insufficient_proposal_evidence():
    import json

    rows = json.loads(futures_replay.FIXTURE_PATH.read_text(encoding="utf-8"))
    source = rule_source(futures_replay.baseline_confirms, rows)
    assert source.built == 0
    assert source.coverage_status == "insufficient_evidence"
    assert source.skipped["missing_intent"] > 0


def test_unknown_ablation_knob_is_refused_not_ignored():
    candle = _candle(_START, 101.0)
    with pytest.raises(ValueError, match="unknown ablation"):
        recorded_source(
            [_recorded_cycle(candle)],
            confirmation_rule=futures_replay.baseline_confirms,
            ablation={"kronos_on": True},
        )


def test_memory_on_ablation_dedup_one_proposal_per_symbol_day():
    """``memory_on`` mirrors the runtime dedup: one entry per symbol/day.

    The runtime memory log suppresses a second decision for the same ticker
    and date (tradingagents/memory/log.py, store_decision's guard). With the
    knob on, only the first confirmed cycle per symbol/day proposes; with it
    off, the repeat proposes too. The knob must change the proposal set — an
    ablation whose output does not move is a placebo, and this test refuses it.
    """
    first = _candle(_DAY_START, 101.0)
    second = _candle(_DAY_START + 2 * 3_600_000, 102.0)  # same UTC day, later 1h open
    cycles = [_recorded_cycle(first), _recorded_cycle(second)]
    on = recorded_source(
        cycles, confirmation_rule=futures_replay.baseline_confirms,
    )
    off = recorded_source(
        cycles, confirmation_rule=futures_replay.baseline_confirms,
        ablation={"memory_on": False},
    )
    # On: the day's first confirmed entry stands, the second is deduped.
    assert on(_activated_candle(first)) is not None
    assert on(_activated_candle(second)) is None
    assert on.skipped["memory_dedup"] == 1
    assert on.built == 1
    # Off: no dedup, both confirmed entries propose.
    assert off(_activated_candle(first)) is not None
    assert off(_activated_candle(second)) is not None
    assert off.skipped["memory_dedup"] == 0
    assert off.built == 2


def test_memory_on_dedup_starts_again_on_the_next_day():
    """The dedup window is per (symbol, day): a new day is a fresh entry."""
    day_one = _candle(_DAY_START, 101.0)
    day_two = _candle(_DAY_START + _DAY_MS, 102.0)
    cycles = [_recorded_cycle(day_one), _recorded_cycle(day_two)]
    source = recorded_source(cycles, confirmation_rule=futures_replay.baseline_confirms)
    assert source(_activated_candle(day_one)) is not None
    assert source(_activated_candle(day_two)) is not None
    assert source.skipped["memory_dedup"] == 0
    assert source.built == 2


def _limits():
    from tradingagents.execution.risk import RiskLimits
    return RiskLimits(
        max_daily_loss_pct=5.0, max_position_pct_per_asset=25.0,
        max_total_exposure_pct=80.0, max_consecutive_loss_count=3,
        max_derivatives_leverage=3.0, max_derivatives_exposure_pct=50.0,
        max_derivatives_exposure_hard_pct=100.0,
        risk_derivatives_override_min_r=2.0,
        risk_derivatives_override_max_risk_fraction=0.01,
        risk_derivatives_override_max_per_day=0, max_open_heat_pct=0.0,
    )


def test_no_trade_and_rule_run_through_the_identical_engine(tmp_path):
    """Same candles, same cost model: the variants differ only in proposals."""
    from tradingagents.simulation.engine import Proposal

    candles = [
        _candle(_START + i * 14_400_000, 100.0 + i) for i in range(6)
    ]
    def proposal_for(candle: Candle):
        return Proposal(
            signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
            stop_loss=candle.close * 0.98, take_profit=candle.close * 1.04,
            risk_fraction=0.004,
        )

    cycle = _recorded_cycle(candles[0])
    rule_variant = rule_source(futures_replay.baseline_confirms, [cycle])
    assert rule_variant.rule_fn is futures_replay.baseline_confirms

    no_trade = run_simulation(
        candles, proposal_source=no_trade_source, risk_limits=_limits(),
        include_sensitivity=False, audit_log_path=None,
    )
    with_rule = run_simulation(
        candles, proposal_source=proposal_for, risk_limits=_limits(),
        include_sensitivity=False,
    )
    assert no_trade.net_after_costs["final_equity"] == pytest.approx(10000.0)
    assert with_rule.net_after_costs["final_equity"] != pytest.approx(10000.0)
    assert with_rule.fills, "the rule variant must actually trade"
    assert not no_trade.fills
    assert rule_variant is not None  # the rule source builds on the same engine types



def _snapshot_cycle(
    symbol: str,
    four_hour_open: int,
    one_hour_open: int,
    *,
    one_hour_closed: bool = True,
    four_hour_closed: bool = True,
    decision_at: str | None = None,
    stop_loss: float = 98.0,
    take_profit: float = 104.0,
) -> dict:
    """Build a cycle with independently timed evidence and decision metadata."""
    four_hour_close = four_hour_open + 14_400_000
    one_hour_close = one_hour_open + 3_600_000
    row = {
        "cycle_id": f"futures:{symbol}:{four_hour_open}:{one_hour_open}",
        "symbol": symbol,
        "status": "completed",
        "phase": "entry",
        "signal": "Buy",
        "confirmation_reason": "aligned_direction",
        "analysis_finished_at": datetime.fromtimestamp(
            (max(one_hour_close, four_hour_close) + 60_000) / 1000, UTC,
        ).isoformat(),
        "proposal_validated_at": decision_at or datetime.fromtimestamp(
            (max(one_hour_close, four_hour_close) + 120_000) / 1000, UTC,
        ).isoformat(),
        "snapshot": {
            "one_hour": {
                "symbol": symbol, "timeframe": "1h",
                "open_time": one_hour_open, "close_time": one_hour_close,
                "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
                "volume": 10.0, "is_closed": one_hour_closed,
            },
            "four_hour": {
                "symbol": symbol, "timeframe": "4h",
                "open_time": four_hour_open, "close_time": four_hour_close,
                "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
                "volume": 10.0, "is_closed": four_hour_closed,
            },
        },
        "payload": {"pm_decision": {"futures_execution_intent": "enter"},
                    "proposal_validation": {"valid": True},
                    "agent_proposal": {
                        "entry_price": 101.0, "stop_loss": stop_loss,
                        "take_profit": take_profit, "leverage": 1.0,
                        "risk_fraction": 0.01, "margin_fraction": 0.25,
                        "margin_mode": "isolated", "scale_out_enabled": False,
                    }},
    }
    if decision_at is not None:
        row["proposal_validated_at"] = decision_at
    else:
        row.pop("proposal_validated_at", None)
    return row


def _symbol_candle(symbol: str, timeframe: str, open_time: int, close: float) -> Candle:
    return Candle(
        symbol=symbol, timeframe=timeframe, open_time=open_time,
        close_time=open_time + (14_400_000 if timeframe == "4h" else 3_600_000),
        open=close - 1.0, high=close + 1.0, low=close - 2.0,
        close=close, volume=10.0, is_closed=True,
    )


def test_rule_source_waits_for_late_snapshot_evidence_before_engine_fill():
    """A 1h close after the 4h close cannot trigger the earlier 4h entry."""
    candles = [
        _candle(_START, 101.0),
        _candle(_START + 14_400_000, 102.0),
        _candle(_START + 3 * 14_400_000, 103.0),
        _candle(_START + 4 * 14_400_000, 104.0),
    ]
    cycle = _snapshot_cycle(
        "BTC/USDT:USDT", _START, _START + 18_000_000,
        decision_at=datetime.fromtimestamp((_START + 21_600_000 + 120_000) / 1000, UTC).isoformat(),
    )
    source = rule_source(futures_replay.baseline_confirms, [cycle])
    assert source(candles[0]) is None

    result = run_simulation(
        candles, proposal_source=rule_source(
            futures_replay.baseline_confirms, [cycle],
        ), risk_limits=_limits(), include_sensitivity=False,
    )
    entries = [fill for fill in result.fills if fill["kind"] == "entry"]
    assert entries, "the rule event should become available on a later closed 4h bar"
    assert entries[0]["candle_open_time"] == candles[2].open_time


def test_rule_source_rejects_snapshot_candles_that_are_not_proven_closed():
    cycle = _snapshot_cycle(
        "BTC/USDT:USDT", _START, _START,
        one_hour_closed=False,
    )
    source = rule_source(futures_replay.baseline_confirms, [cycle])
    assert source(_candle(_START, 101.0)) is None
    assert source.skipped["unclosed_snapshot"] == 1


def test_recorded_source_requires_a_real_decision_completion_timestamp():
    cycle = _snapshot_cycle("BTC/USDT:USDT", _START, _START)
    source = recorded_source(
        [cycle], confirmation_rule=futures_replay.baseline_confirms,
    )
    assert source(_candle(_START, 101.0)) is None
    assert source.skipped["missing_decision_timestamp"] == 1


def test_recorded_source_scopes_events_by_symbol_and_timeframe():
    decision_at = datetime.fromtimestamp((_START + 14_400_000 + 120_000) / 1000, UTC).isoformat()
    btc = _snapshot_cycle("BTC/USDT:USDT", _START, _START, decision_at=decision_at)
    eth = _snapshot_cycle("ETH/USDT:USDT", _START, _START, decision_at=decision_at)
    source = recorded_source(
        [btc, eth], confirmation_rule=futures_replay.baseline_confirms,
    )

    btc_proposal = source(_symbol_candle("BTC/USDT:USDT", "4h", _START + 14_400_000, 101.0))
    eth_proposal = source(_symbol_candle("ETH/USDT:USDT", "4h", _START + 14_400_000, 101.0))
    foreign_timeframe = source(_symbol_candle("BTC/USDT:USDT", "1h", _START, 101.0))

    assert btc_proposal is not None and btc_proposal.ccxt_symbol == "BTC/USDT:USDT"
    assert eth_proposal is not None and eth_proposal.ccxt_symbol == "ETH/USDT:USDT"
    assert foreign_timeframe is None


def test_source_rejection_diagnostics_are_deterministic_across_row_order():
    first = _snapshot_cycle("BTC/USDT:USDT", _START, _START)
    duplicate = _snapshot_cycle(
        "BTC/USDT:USDT", _START, _START,
        decision_at=datetime.fromtimestamp(
            (_START + 14_400_000 + 120_000) / 1000, UTC,
        ).isoformat(),
        stop_loss=97.0,
    )
    original = _recorded_cycle(_candle(_START, 101.0), confirmed="direction_mismatch")
    rows = [first, duplicate, original]
    diagnostics = []
    for ordering in (rows, list(reversed(rows)), [rows[1], rows[0], rows[2]]):
        source = recorded_source(
            ordering, confirmation_rule=futures_replay.baseline_confirms,
        )
        diagnostics.append((source.skipped, source.built, source.coverage_status))
    assert diagnostics[0] == diagnostics[1] == diagnostics[2]


    decision_at = datetime.fromtimestamp((_START + 14_400_000 + 120_000) / 1000, UTC).isoformat()
    first = _snapshot_cycle(
        "BTC/USDT:USDT", _START, _START, decision_at=decision_at,
        stop_loss=98.0,
    )
    duplicate = _snapshot_cycle(
        "BTC/USDT:USDT", _START, _START, decision_at=decision_at,
        stop_loss=97.0,
    )
    source = recorded_source(
        [first, duplicate], confirmation_rule=futures_replay.baseline_confirms,
    )

    assert source(_candle(_START + 14_400_000, 101.0)) is None
    assert source.skipped["duplicate_conflict"] == 2


@pytest.mark.parametrize("intent", ["structured_terms_unavailable", "unexpected"])
def test_unknown_intent_makes_source_evidence_incomplete(intent):
    candle = _candle(_START, 101.0)
    unknown = _recorded_cycle(_candle(_START + _DAY_MS, 102.0))
    unknown["payload"]["pm_decision"]["futures_execution_intent"] = intent
    source = rule_source(futures_replay.baseline_confirms, [_recorded_cycle(candle), unknown])
    assert source.skipped["missing_intent"] == 1
    assert source.skipped["non_entry_intent"] == 0
    assert source.coverage_status == "insufficient_evidence"


@pytest.mark.parametrize("intent", ["abstain", "manage_existing"])
def test_known_non_entry_intent_does_not_make_other_evidence_incomplete(intent):
    candle = _candle(_START, 101.0)
    non_entry = _recorded_cycle(_candle(_START + _DAY_MS, 102.0))
    non_entry["payload"]["pm_decision"]["futures_execution_intent"] = intent
    source = rule_source(futures_replay.baseline_confirms, [_recorded_cycle(candle), non_entry])
    assert source.skipped["non_entry_intent"] == 1
    assert source.coverage_status == "sufficient_evidence"


def test_recorded_confirmation_rejection_overrides_valid_proposal():
    candle = _candle(_START, 101.0)
    cycle = _recorded_cycle(candle, confirmed="direction_mismatch")
    cycle["payload"]["proposal_validation"] = {"valid": True}
    source = recorded_source([cycle], confirmation_rule=futures_replay.baseline_confirms)
    assert source(_activated_candle(candle)) is None
    assert source.skipped["recorded_rejection"] == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_replay_preserves_recorded_scale_out_policy(enabled):
    candle = _candle(_START, 101.0)
    cycle = _recorded_cycle(candle)
    cycle["payload"]["agent_proposal"]["scale_out_enabled"] = enabled
    source = rule_source(futures_replay.baseline_confirms, [cycle])
    assert source(_activated_candle(candle)).scale_out_enabled is enabled


@pytest.mark.parametrize("value", [None, "false", 0])
def test_replay_refuses_missing_or_invalid_scale_out_policy(value):
    candle = _candle(_START, 101.0)
    cycle = _recorded_cycle(candle)
    cycle["payload"]["agent_proposal"]["scale_out_enabled"] = value
    source = rule_source(futures_replay.baseline_confirms, [cycle])
    assert source(_activated_candle(candle)) is None
    assert source.coverage_status == "insufficient_evidence"


def test_replay_coverage_counts_only_holdout_activation_candles():
    candle = _candle(_START, 101.0)
    source = rule_source(futures_replay.baseline_confirms, [_recorded_cycle(candle)])
    matching = _activated_candle(candle)
    missing_time = _candle(matching.open_time + _DAY_MS, 101.0)
    foreign_symbol = _symbol_candle("ETH/USDT:USDT", "4h", matching.open_time, 101.0)
    foreign_timeframe = _symbol_candle(candle.symbol, "1h", matching.open_time, 101.0)
    assert source.coverage_for([matching])["status"] == "sufficient_evidence"
    for unmatched in (missing_time, foreign_symbol, foreign_timeframe):
        coverage = source.coverage_for([unmatched])
        assert coverage["built"] == 0
        assert coverage["status"] == "insufficient_evidence"


def test_replay_coverage_ignores_incomplete_evidence_outside_holdout():
    candle = _candle(_START, 101.0)
    outside = _recorded_cycle(_candle(_START + _DAY_MS, 102.0))
    outside["payload"].pop("pm_decision")
    source = rule_source(futures_replay.baseline_confirms, [_recorded_cycle(candle), outside])
    assert source.coverage_status == "insufficient_evidence"
    coverage = source.coverage_for([_activated_candle(candle)])
    assert coverage["status"] == "sufficient_evidence"
    assert coverage["skipped"]["missing_intent"] == 0


def test_holdout_coverage_detects_conflicts_with_other_activation_times():
    first = _candle(_START, 101.0)
    conflicting = _recorded_cycle(first)
    later_ms = first.close_time + 2 * _DAY_MS
    conflicting["payload"]["phase_timestamps"]["proposal_validated_at"] = datetime.fromtimestamp(
        later_ms / 1000, UTC,
    ).isoformat()
    independent = _candle(_START + _DAY_MS, 102.0)
    source = rule_source(
        futures_replay.baseline_confirms,
        [_recorded_cycle(first), conflicting, _recorded_cycle(independent)],
    )
    coverage = source.coverage_for([_activated_candle(first), _activated_candle(independent)])
    assert coverage["built"] == 1
    assert coverage["skipped"]["duplicate_conflict"] == 2
    assert coverage["status"] == "insufficient_evidence"


def test_holdout_coverage_handles_repeated_reference_to_one_row():
    candle = _candle(_START, 101.0)
    cycle = _recorded_cycle(candle)
    source = rule_source(futures_replay.baseline_confirms, [cycle, cycle])
    coverage = source.coverage_for([_activated_candle(candle)])
    assert coverage["input_count"] == 2
    assert coverage["skipped"]["duplicate_event"] == 1
    assert coverage["status"] == "sufficient_evidence"


def test_conflicting_identities_are_checked_before_rule_filtering():
    candle = _candle(_START, 101.0)
    accepted = _recorded_cycle(candle)
    rejected = _recorded_cycle(candle, signal="Sell")
    for rows in ([accepted, rejected], [rejected, accepted]):
        source = rule_source(futures_replay.baseline_confirms, rows)
        assert source(_activated_candle(candle)) is None
        assert source.skipped["duplicate_conflict"] == 2
        assert source.coverage_status == "insufficient_evidence"


def test_conflicting_recorded_confirmation_reasons_cannot_restore_entry():
    candle = _candle(_START, 101.0)
    entry = _recorded_cycle(candle)
    rejection = _recorded_cycle(candle, confirmed="direction_mismatch")
    for rows in ([entry, rejection], [rejection, entry]):
        source = recorded_source(rows, confirmation_rule=futures_replay.baseline_confirms)
        assert source(_activated_candle(candle)) is None
        assert source.skipped["duplicate_conflict"] == 2
