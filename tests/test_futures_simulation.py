"""L6: futures simulation parity, ambiguity policy, costs, offline purity.

Parity contract (plan §3.2): the same proposal through the engine and through
the runtime's own functions must give the same verdicts; OHLC ambiguity is
published and resolved by policy, never interpolated; slippage moves fill
prices only (fees computed once on the slipped price); the recorded-decision
replay reports unavailable financial parts instead of zeros.

The engine's default guard carries RiskLimits' own defaults, which block all
derivatives notional (the runtime's fail-closed default) — parity tests pass
explicit runtime-style limits, exactly like a configured runtime would.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.execution.profit_protection import evaluate_profit_protection
from tradingagents.execution.risk import RiskLimits
from tradingagents.simulation.costs import CostModel, FundingSeries
from tradingagents.simulation.engine import (
    DISCLAIMER,
    SIMULATION_LABEL,
    Proposal,
    SizingCaps,
    futures_plan,
    replay_recorded_decisions,
    run_simulation,
)
from tradingagents.simulation.fills import MarketRules

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parents[1] / "tests" / "fixtures" / "futures_cycles_replay.json"


def _runtime_limits() -> RiskLimits:
    """Runtime-style limits: fail-closed defaults with the caps raised, the
    same keys ExchangeBridge._build_risk_guard maps from config."""
    return RiskLimits(
        max_daily_loss_pct=5.0,
        max_position_pct_per_asset=25.0,
        max_total_exposure_pct=80.0,
        max_consecutive_loss_count=3,
        max_derivatives_leverage=3.0,
        max_derivatives_exposure_pct=50.0,
        max_derivatives_exposure_hard_pct=100.0,
        risk_derivatives_override_min_r=2.0,
        risk_derivatives_override_max_risk_fraction=0.01,
        risk_derivatives_override_max_per_day=0,
        max_open_heat_pct=0.0,
    )


def _candle(open_time: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        symbol="BTC/USDT:USDT", timeframe="1h", open_time=open_time,
        close_time=open_time + 3_600_000, open=open_, high=high, low=low,
        close=close, volume=1000.0, is_closed=True,
    )


def _proposal(price: float, risk: float = 0.004) -> Proposal:
    return Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=price * 0.98, take_profit=price * 1.04,
        leverage=1.0, risk_fraction=risk, margin_fraction=0.25,
    )


def _rising_candles(count: int = 6, base: float = 100.0) -> list[Candle]:
    candles = []
    price = base
    for index in range(count):
        open_price = price
        close_price = price * 1.01
        candles.append(_candle(
            1_700_000_000_000 + index * 3_600_000,
            open_price, max(open_price, close_price) * 1.002,
            min(open_price, close_price) * 0.999, close_price,
        ))
        price = close_price
    return candles


def _run(candles, *, proposal_source=None, **kwargs) -> "object":
    kwargs.setdefault("proposal_source", proposal_source or (lambda candle: _proposal(candle.close)))
    kwargs.setdefault("risk_limits", _runtime_limits())
    kwargs.setdefault("include_sensitivity", False)
    return run_simulation(candles, **kwargs)


def test_engine_plan_matches_runtime_size_chain():
    """Parity pin: the engine's futures plan equals DerivativesExecutor._size."""
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.portfolio import PortfolioContext

    executor = DerivativesExecutor(config={
        "risk_derivatives_risk_fraction": 0.01,
        "risk_derivatives_margin_fraction": 0.25,
        "risk_derivatives_max_risk_fraction": 0.02,
        "risk_derivatives_max_margin_fraction": 0.5,
    })
    book = PortfolioContext(cash=10000.0)
    price = 100.0
    proposal = _proposal(price)
    engine_plan = futures_plan(
        proposal=proposal, portfolio=book, price=price,
        caps=SizingCaps(max_risk_fraction=0.02, max_margin_fraction=0.5, min_order_cost=10.0),
    )
    runtime_plan = executor._size(
        signal=proposal.signal, ticker=proposal.ticker,
        ccxt_symbol=proposal.ccxt_symbol, portfolio=book, price=price,
        leverage=proposal.leverage, stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit, risk_fraction=proposal.risk_fraction,
        margin_fraction=proposal.margin_fraction,
        margin_mode=proposal.margin_mode,
        futures_execution_intent="enter",
    )
    assert engine_plan.model_dump() == runtime_plan.model_dump()


def test_risk_decision_parity_same_plan_same_guard_same_verdict():
    from tradingagents.execution.risk import RiskGuard
    from tradingagents.portfolio import PortfolioContext

    book = PortfolioContext(cash=10000.0)
    plan = futures_plan(proposal=_proposal(100.0), portfolio=book, price=100.0)
    guard = RiskGuard(limits=_runtime_limits(), price_of=lambda ticker: None)
    decision = guard.check(plan, book, "unused-sim-log-path")
    # Same inputs through a fresh guard instance must give the same verdict:
    # the engine's parity is the function's determinism, recorded in the log.
    again = RiskGuard(limits=_runtime_limits(), price_of=lambda ticker: None).check(
        plan, book, "unused-sim-log-path"
    )
    assert decision.allowed == again.allowed
    assert decision.reason == again.reason


def test_protection_parity_engine_inputs_match_direct_calls():
    """Re-calling the real function on the engine's own logged inputs must
    reproduce the engine's phases exactly."""
    candles = _rising_candles(7)
    result = _run(candles)
    protection_calls = [
        entry for entry in result.parity_log
        if entry["call"] == "evaluate_profit_protection"
    ]
    assert protection_calls, "engine never managed a position: entry refused"
    for entry in protection_calls:
        inputs = entry["input"]
        decision = evaluate_profit_protection(
            side=inputs["side"], entry=inputs["entry"],
            initial_stop=inputs["initial_stop"], current_stop=inputs["current_stop"],
            current_target=inputs["current_target"], mark=inputs["mark"],
            max_hold_hours=None, held_hours=inputs["held_hours"],
            cumulative_funding_r=inputs["cumulative_funding_r"],
            round_trip_fee_r=inputs["round_trip_fee_r"],
        )
        assert decision.stop == entry["output"]["stop"]
        assert decision.target == entry["output"]["target"]
        assert decision.phase == entry["output"]["phase"]
        assert decision.exit_reason == entry["output"]["exit_reason"]


def test_entry_parity_log_records_real_function_outputs():
    candles = _rising_candles(4)
    result = _run(candles)
    calls = {entry["call"] for entry in result.parity_log}
    assert {"futures_plan", "RiskGuard.check"} <= calls
    assert result.parity_log[0]["call"] == "futures_plan"


def test_ambiguity_counted_and_worst_first_fills_the_stop():
    # One candle spans both the stop (98) and the target (104); scale-out is
    # off so the take-profit is the matched target.
    ambiguous_candle = _candle(1_700_000_100_000, 100.0, 104.5, 97.5, 101.0)
    candles = [_candle(1_700_000_000_000, 100.0, 100.5, 99.5, 100.2), ambiguous_candle]
    proposal = Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=104.0, leverage=1.0,
        risk_fraction=0.004, margin_fraction=0.25, scale_out_enabled=False,
    )
    proposal_source = lambda candle: proposal if candle.open_time == 1_700_000_000_000 else None  # noqa: E731

    worst = run_simulation(
        candles, proposal_source=proposal_source, ambiguity_policy="worst-first",
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    assert worst.ambiguity_count == 1
    assert worst.exit_reason_distribution.get("stop") == 1
    stop_fill = next(fill for fill in worst.fills if fill["kind"] == "stop")
    assert stop_fill["ambiguous"] is True

    skipped = run_simulation(
        candles, proposal_source=proposal_source, ambiguity_policy="skip",
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    assert skipped.ambiguity_count == 1
    assert "stop" not in skipped.exit_reason_distribution


def test_unknown_ambiguity_policy_is_refused():
    with pytest.raises(ValueError, match="ambiguity policy"):
        run_simulation(
            _rising_candles(2), proposal_source=lambda candle: None,
            ambiguity_policy="interpolate", include_sensitivity=False,
        )


def test_gap_through_stop_fills_at_open_not_survived():
    """Blocker 3: a candle that OPENED below the long stop fills the stop at
    the open — the venue trigger fires there; the position can never quietly
    survive a candle that traded entirely past its stop."""
    fill_candle = _candle(1_700_000_050_000, 100.0, 100.6, 99.4, 100.3)
    gap_candle = _candle(1_700_000_100_000, 90.0, 97.0, 89.0, 95.0)  # open 90 < stop 98
    candles = [
        _candle(1_700_000_000_000, 100.0, 100.5, 99.5, 100.2),
        fill_candle,
        gap_candle,
    ]
    proposal = Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=104.0, leverage=1.0,
        risk_fraction=0.004, margin_fraction=0.25, scale_out_enabled=False,
    )
    proposal_source = lambda candle: proposal if candle.open_time == 1_700_000_000_000 else None  # noqa: E731
    result = run_simulation(
        candles, proposal_source=proposal_source, ambiguity_policy="worst-first",
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    assert result.ambiguity_count == 0  # a gap is decisive, not ambiguous
    assert result.exit_reason_distribution.get("stop") == 1
    stop_fill = next(fill for fill in result.fills if fill["kind"] == "stop")
    assert stop_fill["price"] == pytest.approx(90.0)  # filled at the open
    assert stop_fill["ambiguous"] is False


def test_entry_gap_below_stop_abandons_the_plan_instead_of_crashing():
    """A candle gapping through the bracket between decision and fill
    invalidates the plan: the entry is abandoned (validate_bracket
    semantics), never opened with a stop that does not protect it."""
    gap_fill_candle = _candle(1_700_000_050_000, 90.0, 97.0, 89.0, 95.0)
    candles = [
        _candle(1_700_000_000_000, 100.0, 100.5, 99.5, 100.2),
        gap_fill_candle,
    ]
    proposal_source = lambda candle: _proposal(100.0) if candle.open_time == 1_700_000_000_000 else None  # noqa: E731
    result = run_simulation(
        candles, proposal_source=proposal_source, ambiguity_policy="worst-first",
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    decisions = [d["decision"] for d in result.decisions]
    assert "entry_bracket_invalidated_by_gap" in decisions
    assert result.fills == []


def test_gap_through_target_fills_at_open_favorable():
    """A candle that OPENED beyond the resting target fills the exit at the
    open — never skipped (the fill candle's range stays below the target so
    the resting limit survives until the gap)."""
    fill_candle = _candle(1_700_000_050_000, 100.0, 103.4, 99.4, 103.0)
    gap_candle = _candle(1_700_000_100_000, 120.0, 121.0, 119.5, 120.5)  # open 120 > 104
    candles = [
        _candle(1_700_000_000_000, 100.0, 100.5, 99.5, 100.2),
        fill_candle,
        gap_candle,
    ]
    proposal = Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=104.0, leverage=1.0,
        risk_fraction=0.004, margin_fraction=0.25, scale_out_enabled=False,
    )
    result = run_simulation(
        candles,
        proposal_source=lambda candle: proposal if candle.open_time == 1_700_000_000_000 else None,
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    assert result.exit_reason_distribution.get("target") == 1
    target_fill = next(fill for fill in result.fills if fill["kind"] == "target")
    assert target_fill["price"] == pytest.approx(120.0)


def test_gap_stop_worse_than_level_rejects_the_better_stop_price():
    """The stop gap fill price is the open, and the engine's slippage applies
    on top of it — the fill is never better than the market open."""
    from tradingagents.simulation.fills import gaps_stop, match_exit

    candle = _candle(1_700_000_100_000, 90.0, 97.0, 89.0, 95.0)
    assert gaps_stop(candle, "long", 98.0) is True
    match = match_exit(
        candle, "long", stop=98.0, target=None, quantity=1.0,
        rules=MarketRules(), ambiguity_policy="worst-first",
    )
    assert match.fill is not None and match.fill.price == 90.0
    assert gaps_stop(_candle(0, 99.0, 101.0, 98.5, 100.0), "long", 98.0) is False


def test_scale_out_enabled_never_closes_full_at_the_take_profit():
    """Finding 2 (runtime parity): with scale-out enabled the TP is REPLACED
    by tranche legs plus a trailed runner — the sim must not exit 100% at the
    TP level; only the tranches and the trailed stop reduce it."""
    # Rise past the TP (104) but never far enough to trail the runner out:
    # favorable_r stays below trail_start (3R) for the whole run.
    candles = [_candle(1_700_000_000_000 + i * 3_600_000,
                       100.0 + i * 1.0, 101.4 + i * 1.0, 99.6 + i * 1.0,
                       100.8 + i * 1.0) for i in range(6)]
    proposal = Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=104.0,
        leverage=1.0, risk_fraction=0.004, margin_fraction=0.25,
        scale_out_enabled=True,
    )
    result = run_simulation(
        candles,
        proposal_source=lambda candle: proposal if candle.open_time == 1_700_000_000_000 else None,
        risk_limits=_runtime_limits(), include_sensitivity=False,
    )
    assert "target" not in result.exit_reason_distribution  # no full-TP exit
    assert result.exit_reason_distribution.get("scale_out", 0) >= 1  # tranches fired


def test_funding_no_settlement_inside_known_span_is_a_known_zero():
    """Finding 1: a window inside the series' known span with no settlement
    is a known zero, NOT unknown; only windows outside the span are unknown."""
    candles = _rising_candles(5)
    funding = FundingSeries(
        amounts={candles[3].open_time + 1: 2.0},
        venue_semantics="test: positive = received",
        known_from_ms=candles[0].open_time,
        known_to_ms=candles[-1].close_time,
    )
    result = _run(candles, funding=funding)
    assert result.funding_known is True  # every window covered by the span
    assert result.net_after_costs["funding_partial"] is False
    assert result.net_after_costs["funding_received"] == pytest.approx(2.0)


def test_funding_window_outside_known_span_is_unknown_not_zero():
    candles = _rising_candles(5)
    funding = FundingSeries(
        amounts={candles[3].open_time + 1: 2.0},
        venue_semantics="test: positive = received",
        known_from_ms=candles[3].open_time,  # windows before that are unknown
    )
    result = _run(candles, funding=funding)
    assert result.funding_known is False
    assert result.net_after_costs["funding_partial"] is True
    assert result.net_after_costs["funding_unknown_windows"] > 0
    assert result.net_after_costs["funding_received"] == pytest.approx(2.0)


def test_costs_slippage_moves_price_once_and_fee_charges_on_slipped_price():
    candles = _rising_candles(4)
    result = _run(
        candles,
        costs=CostModel(taker_rate=0.0005, slippage_rate=0.001),
    )
    entry = next(fill for fill in result.fills if fill["kind"] == "entry")
    open_price = candles[1].open
    assert entry["price"] == pytest.approx(open_price * 1.001)
    assert entry["fee"] == pytest.approx(entry["quantity"] * entry["price"] * 0.0005)
    # The engine takes no slippage deduction anywhere: the only slippage is in
    # the fill price itself.
    assert "slippage" not in result.net_after_costs


def test_funding_missing_reports_unknown_never_zero():
    candles = _rising_candles(4)
    result = _run(candles)
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] is None
    assert result.net_after_costs["funding_partial"] is False
    assert result.net_after_costs["funding_unknown_windows"] == 0


def test_funding_series_applies_signed_amounts():
    candles = _rising_candles(5)
    funding = FundingSeries(
        amounts={candles[3].open_time + 1: 2.0},  # one settlement inside window
        venue_semantics="test: positive = received",
    )
    result = _run(candles, funding=funding)
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(2.0)
    assert result.net_after_costs["funding_semantics"] == "test: positive = received"


def test_report_carries_simulation_label_disclaimer_and_required_sections():
    candles = _rising_candles(6)
    result = _run(candles)
    report = result.report()
    assert report["kind"] == SIMULATION_LABEL == "simulation"
    assert DISCLAIMER in report["disclaimer"]
    for section in ("equity_curve", "max_drawdown", "net_after_costs",
                    "ambiguity_count", "exit_reason_distribution", "cost_sensitivity"):
        assert section in report
    assert len(report["equity_curve"]) == len(candles)
    assert 0 <= report["max_drawdown"] <= 1


def test_cost_sensitivity_variants_shift_the_net():
    candles = _rising_candles(6)
    result = _run(candles, include_sensitivity=True)
    variants = [row["variant"] for row in result.sensitivity]
    assert variants == ["base", "fee+2bp", "fee-2bp", "slippage+5bp"]
    base = next(row for row in result.sensitivity if row["variant"] == "base")
    more_fee = next(row for row in result.sensitivity if row["variant"] == "fee+2bp")
    assert more_fee["fee_paid"] > base["fee_paid"]


def test_simulation_never_imports_network_paths():
    """Offline purity (plan §3.1): the simulation modules' own imports contain
    no exchange client, no bridge, no server layer, and a full run succeeds
    with every socket refused.

    Note: importing any ``tradingagents.execution.*`` submodule runs that
    package's eager ``__init__`` (a pre-existing repo condition), so a
    sys.modules check cannot isolate the engine; the source-level check below
    pins the engine's own import graph instead, and the subprocess proves no
    network is opened at import or run time.
    """
    import ast

    simulation_dir = Path(sys.modules["tradingagents.simulation.engine"].__file__).parent
    forbidden_prefixes = ("ccxt", "tradingagents.execution.bridge",
                          "tradingagents.execution.derivatives",
                          "tradingagents.execution.exchange_client", "server")
    for source_file in simulation_dir.glob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(forbidden_prefixes), (
                    f"{source_file.name} imports {name}"
                )
    code = """
import socket

def refuse(*args, **kwargs):
    raise OSError("network disabled in the simulation purity test")

# Same seam as tests/conftest._no_network: block connection attempts, not the
# class hierarchy (ssl.py subclasses socket.socket at import time).
socket.socket.connect = refuse
socket.socket.connect_ex = refuse
socket.getaddrinfo = lambda *a, **k: refuse(None, *a)
from tradingagents.dataflows.market_data import Candle
from tradingagents.simulation.engine import Proposal, run_simulation
from tradingagents.execution.risk import RiskLimits

def candle(t, o, h, l, c):
    return Candle("BTC/USDT:USDT", "1h", t, t + 3600000, o, h, l, c, 1.0, True)

candles = [candle(1700000000000 + i * 3600000, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(3)]
result = run_simulation(
    candles,
    proposal_source=lambda c: Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=104.0, leverage=1.0,
        risk_fraction=0.004, margin_fraction=0.25),
    risk_limits=RiskLimits(max_derivatives_leverage=3.0, max_derivatives_exposure_pct=50.0),
    include_sensitivity=False,
)
assert result.net_after_costs["final_equity"] > 0
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).parents[1]),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ok" in completed.stdout


def test_rising_market_lands_in_profit_after_costs():
    candles = _rising_candles(8)
    result = _run(candles)
    assert result.net_after_costs["final_equity"] > result.net_after_costs["initial_cash"]
    assert result.exit_reason_distribution  # something closed the position


def test_standalone_replay_script_runs_offline_on_the_real_fixture():
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "scripts" / "replay_futures_decisions.py"),
         "--fixture", str(FIXTURE_PATH)],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).parents[1]),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    # This script is a gate-agreement report, not a financial experiment: it
    # compares the rule against what was recorded and says plainly that the
    # financial part is unavailable from this data. The kind and the pointer to
    # the real financial entry point are what stop it masquerading as one.
    assert report["kind"] == "gate_agreement_replay"
    assert "financial_experiment" in report["financial_experiment_command"]
    assert report["financial_simulation"]["status"] == "unavailable"
    assert report["decision_disagreements"] == 0


def test_hard_capped_proposal_is_refused_by_the_size_chain():
    candles = _rising_candles(3)
    result = _run(
        candles,
        proposal_source=lambda candle: _proposal(candle.close, risk=0.5),
        caps=SizingCaps(max_risk_fraction=0.02, max_margin_fraction=0.5),
    )
    refusals = [d for d in result.decisions if d["decision"] == "no_order"]
    assert refusals and "hard cap" in refusals[0]["reason"]
    assert result.fills == []


def test_recorded_replay_reports_unavailable_financial_parts():
    from server.futures_replay import RULES

    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report = replay_recorded_decisions(rows, confirmation_rule=RULES["baseline"])
    assert report["recorded_cycles"] == len(rows)
    assert report["financial_simulation"]["status"] == "unavailable"
    assert "Missing data is not zero" in report["financial_simulation"]["reason"]
    # The replayed subset is exactly the rows with a signal and both candles.
    replayable = sum(
        1 for row in rows
        if row.get("signal") and isinstance(row.get("snapshot"), dict)
        and row["snapshot"].get("one_hour") and row["snapshot"].get("four_hour")
    )
    assert report["replayed_with_snapshot"] == replayable
    assert report["replayed_with_snapshot"] > 0
    assert (
        report["decision_agreements"] + report["decision_disagreements"]
        == report["replayed_with_snapshot"]
    )


def test_fee_r_conversion_matches_the_runtime_time_stop_formula():
    """Finding 7: the engine's round-trip fee R must equal the runtime
    monitor's conversion (futures_protection._time_stop_funding_inputs:
    fee_r = round_trip_rate * entry / risk_per_unit) — the cost model's
    2 x taker encodes the runtime default round-trip rate of 0.001."""
    candles = _rising_candles(6)
    result = _run(candles, costs=CostModel(taker_rate=0.0005, maker_rate=0.0005))
    fee_calls = [
        entry for entry in result.parity_log
        if entry["call"] == "evaluate_profit_protection"
    ]
    assert fee_calls, "engine never managed a position"
    for entry in fee_calls:
        inputs = entry["input"]
        if inputs["round_trip_fee_r"] == 0.0:
            continue
        entry_price, stop = inputs["entry"], inputs["initial_stop"]
        risk_per_unit = abs(entry_price - stop)
        expected = 0.001 * entry_price / risk_per_unit
        assert inputs["round_trip_fee_r"] == pytest.approx(expected)


def test_funding_r_denominator_is_the_opened_size_not_the_remainder():
    """Parity: the runtime divides cumulative funding by the size the bracket
    OPENED with (futures_protection._time_stop_funding_inputs), never the
    shrunken remainder after scale-outs."""
    candles = _rising_candles(6)
    proposal = Proposal(
        signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
        stop_loss=98.0, take_profit=140.0,  # far target: the position survives
        leverage=1.0, risk_fraction=0.004, margin_fraction=0.25,
        scale_out_enabled=False,
    )
    opened_quantity = None

    def source(candle: Candle) -> Proposal | None:
        return proposal if candle.open_time == candles[0].open_time else None

    no_funding = _run(candles, proposal_source=source)
    protection_calls = [
        entry for entry in no_funding.parity_log
        if entry["call"] == "evaluate_profit_protection"
    ]
    assert protection_calls
    opened_quantity = next(
        fill["quantity"] for fill in no_funding.fills if fill["kind"] == "entry"
    )
    # No funding series -> 0.0, matching the runtime's own fail-closed
    # fallback for the time-stop boundary (_time_stop_funding_inputs returns
    # 0.0 with a "funding_history_unavailable" note; unknown funding must
    # never rescue the position). The report separately marks funding unknown.
    assert protection_calls[0]["input"]["cumulative_funding_r"] == 0.0

    from tradingagents.simulation.costs import FundingSeries

    funding = FundingSeries(
        amounts={candles[3].open_time + 1: 2.0},
        venue_semantics="test: positive = received",
    )
    funded = _run(candles, funding=funding, proposal_source=source)
    funded_calls = [
        entry for entry in funded.parity_log
        if entry["call"] == "evaluate_profit_protection"
        and entry["input"]["cumulative_funding_r"] != 0.0
    ]
    assert funded_calls
    for entry in funded_calls:
        inputs = entry["input"]
        denominator = opened_quantity * (inputs["entry"] - inputs["initial_stop"])
        assert inputs["cumulative_funding_r"] == pytest.approx(
            funded.net_after_costs["funding_received"] / denominator, rel=1e-6,
        )


def test_engine_limits_match_the_runtime_guard_built_from_config():
    """Finding 7: limits handed to the engine must be constructible exactly
    like the runtime guard builds them from config (ExchangeBridge's
    _build_risk_guard mapping), so a parity run can pass runtime limits."""
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.execution.risk import RiskLimits

    config = {
        "risk_max_daily_loss_pct": 4.0,
        "risk_max_position_pct_per_asset": 20.0,
        "risk_max_total_exposure_pct": 70.0,
        "risk_max_consecutive_loss_count": 4,
        "risk_max_derivatives_leverage": 2.0,
        "risk_max_derivatives_exposure_pct": 30.0,
        "risk_max_derivatives_exposure_hard_pct": 60.0,
        "risk_derivatives_override_min_r": 1.5,
        "risk_derivatives_override_max_risk_fraction": 0.02,
        "risk_derivatives_override_max_per_day": 2,
        "risk_max_open_heat_pct": 15.0,
    }
    runtime_guard = DerivativesExecutor(config=config).risk_guard
    engine_limits = RiskLimits(
        max_daily_loss_pct=float(config["risk_max_daily_loss_pct"]),
        max_position_pct_per_asset=float(config["risk_max_position_pct_per_asset"]),
        max_total_exposure_pct=float(config["risk_max_total_exposure_pct"]),
        max_consecutive_loss_count=int(config["risk_max_consecutive_loss_count"]),
        max_derivatives_leverage=float(config["risk_max_derivatives_leverage"]),
        max_derivatives_exposure_pct=float(config["risk_max_derivatives_exposure_pct"]),
        max_derivatives_exposure_hard_pct=float(config["risk_max_derivatives_exposure_hard_pct"]),
        risk_derivatives_override_min_r=float(config["risk_derivatives_override_min_r"]),
        risk_derivatives_override_max_risk_fraction=float(config["risk_derivatives_override_max_risk_fraction"]),
        risk_derivatives_override_max_per_day=int(config["risk_derivatives_override_max_per_day"]),
        max_open_heat_pct=float(config["risk_max_open_heat_pct"]),
    )
    assert runtime_guard.limits == engine_limits


def test_recorded_replay_parity_on_the_real_fixture():
    """The real baseline rule re-decides every recorded confirmation outcome."""
    from server.futures_replay import RULES

    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    report = replay_recorded_decisions(rows, confirmation_rule=RULES["baseline"])
    assert report["decision_disagreements"] == 0
    assert report["rule_reason_distribution"]
