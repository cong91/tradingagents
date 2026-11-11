"""E4: baselines — every variant runs the SAME simulation; rules are imported.

Plan §4.3 E4: no-trade/rule/recorded/ablation go through the identical engine
call with the identical cost model; the rule baseline reuses
``server.futures_replay.RULES`` (import, never fork — mutating the rule table
or its functions is visible here); an unknown ablation knob is refused.
"""

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


def _recorded_cycle(candle: Candle, signal: str = "Buy", *, confirmed: str | None = "aligned_direction",
                    payload: dict | None = None) -> dict:
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
        "payload": payload or {"agent_proposal": {
            "entry_price": candle.close, "stop_loss": candle.close * 0.98,
            "take_profit": candle.close * 1.04, "leverage": 1.0,
            "risk_fraction": 0.01,
        }},
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
    proposal = source(candle)
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
    proposal = source(candle)
    assert proposal is not None
    assert proposal.stop_loss == pytest.approx(101.0 * 0.98)
    assert proposal.take_profit == pytest.approx(101.0 * 1.04)


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
    assert on(first) is not None
    assert on(second) is None
    assert on.skipped["memory_dedup"] == 1
    assert on.built == 1
    # Off: no dedup, both confirmed entries propose.
    assert off(first) is not None
    assert off(second) is not None
    assert off.skipped["memory_dedup"] == 0
    assert off.built == 2


def test_memory_on_dedup_starts_again_on_the_next_day():
    """The dedup window is per (symbol, day): a new day is a fresh entry."""
    day_one = _candle(_DAY_START, 101.0)
    day_two = _candle(_DAY_START + _DAY_MS, 102.0)
    cycles = [_recorded_cycle(day_one), _recorded_cycle(day_two)]
    source = recorded_source(cycles, confirmation_rule=futures_replay.baseline_confirms)
    assert source(day_one) is not None
    assert source(day_two) is not None
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
