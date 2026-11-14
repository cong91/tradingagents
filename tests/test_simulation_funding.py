"""Funding evidence and exposure accounting in the offline futures simulator."""

from dataclasses import replace

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.execution.risk import RiskLimits
from tradingagents.simulation.costs import CostModel, FundingSeries
from tradingagents.simulation.engine import Proposal, run_simulation

pytestmark = pytest.mark.unit

HOUR = 3_600_000
START = 1_700_000_000_000
SYMBOL = "BTC/USDT:USDT"


def _candles(count=3, *, spacing=HOUR):
    return [
        Candle(
            symbol=SYMBOL, timeframe="1h", open_time=START + index * spacing,
            close_time=START + index * spacing + HOUR,
            open=100.0, high=100.5, low=99.5, close=100.0,
            volume=1000.0, is_closed=True,
        )
        for index in range(count)
    ]


def _proposal(side="long", risk=0.01):
    return Proposal(
        signal="Buy" if side == "long" else "Sell", ticker="BTC-USD",
        ccxt_symbol=SYMBOL, stop_loss=90.0 if side == "long" else 110.0,
        take_profit=150.0 if side == "long" else 50.0,
        risk_fraction=risk, margin_fraction=0.5, scale_out_enabled=False,
    )


def _series(candles, *, rates=None, marks=None, **overrides):
    fields = {
        "symbol": SYMBOL,
        "rates": {} if rates is None else rates,
        "mark_prices": {} if marks is None else marks,
        "known_from_ms": candles[1].open_time,
        "known_to_ms": candles[-1].close_time,
        "quote_currency": "USDT",
        "contract_type": "linear",
        "quantity_unit": "base",
    }
    fields.update(overrides)
    return FundingSeries(**fields)


def _run(candles, funding, *, proposal=None, source=None, **kwargs):
    proposal = proposal or _proposal()
    source = source or (lambda candle: proposal if candle is candles[0] else None)
    return run_simulation(
        candles, proposal_source=source, funding=funding,
        risk_limits=RiskLimits(
            max_daily_loss_pct=5.0, max_position_pct_per_asset=25.0,
            max_total_exposure_pct=80.0, max_derivatives_leverage=3.0,
            max_derivatives_exposure_pct=50.0,
            max_derivatives_exposure_hard_pct=100.0, max_open_heat_pct=0.0,
        ),
        costs=CostModel(taker_rate=0.0, maker_rate=0.0),
        include_sensitivity=False, **kwargs,
    )


def _entry_quantity(result):
    return next(fill["quantity"] for fill in result.fills if fill["kind"] == "entry")


@pytest.mark.parametrize(("side", "risk"), [("long", 0.004), ("long", 0.01), ("short", 0.01)])
def test_settlement_uses_side_remaining_quantity_rate_and_mark(side, risk):
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
        proposal=_proposal(side, risk),
    )
    expected = _entry_quantity(result) * 120.0 * 0.01
    if side == "long":
        expected = -expected
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(expected)


def test_missing_settlement_rate_is_unknown():
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(candles, _series(candles, marks={settlement: 120.0}))
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


def test_settlements_use_half_open_windows_at_entry_and_candle_boundaries():
    candles = _candles()
    boundaries = (candles[1].open_time, candles[2].open_time, candles[-1].close_time)
    rates = dict.fromkeys(boundaries, 0.01)
    marks = dict.fromkeys(boundaries, 120.0)
    result = _run(candles, _series(candles, rates=rates, marks=marks))
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -2 * _entry_quantity(result) * 120.0 * 0.01
    )


def test_gap_settlement_is_counted_once_before_a_pending_exit():
    candles = _candles(spacing=2 * HOUR)
    settlement = candles[1].close_time + HOUR // 2
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
        max_hold_hours=0.1,
    )
    assert result.exit_reason_distribution.get("time_stop") == 1
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -_entry_quantity(result) * 120.0 * 0.01
    )


def test_missing_settlement_mark_is_unknown_and_never_uses_candle_price():
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(candles, _series(candles, rates={settlement: 0.01}))
    assert result.funding_known is False
    assert result.net_after_costs["funding_partial"] is True
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)
    assert result.net_after_costs["final_equity_status"] == "estimate"


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "ETH/USDT:USDT"},
        {"quote_currency": "USDC"},
        {"contract_type": "inverse"},
        {"quantity_unit": "contracts"},
        {"contract_type": None},
        {"quantity_unit": None},
        {"known_from_ms": None},
        {"known_to_ms": None},
    ],
)
def test_missing_or_mismatched_evidence_is_unknown(overrides):
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    funding = _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}, **overrides)
    result = _run(candles, funding)
    assert result.funding_known is False
    assert result.net_after_costs["final_equity_status"] == "estimate"
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


@pytest.mark.parametrize("bad_rate", [None, float("nan"), float("inf"), "0.01", True])
def test_invalid_rate_is_unknown(bad_rate):
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(candles, _series(candles, rates={settlement: bad_rate}, marks={settlement: 120.0}))
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


@pytest.mark.parametrize("bad_mark", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_invalid_mark_is_unknown(bad_mark):
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(candles, _series(candles, rates={settlement: 0.01}, marks={settlement: bad_mark}))
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("rates", "marks"),
    [({"invalid": 0.01}, {"invalid": 120.0}), ({True: 0.01}, {True: 120.0})],
)
def test_invalid_settlement_timestamp_does_not_prove_coverage(rates, marks):
    candles = _candles()
    result = _run(candles, _series(candles, rates=rates, marks=marks))
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


def test_overflowing_settlement_is_unknown():
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(
        candles,
        _series(candles, rates={settlement: 1e308}, marks={settlement: 1e308}),
    )
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


def test_funding_after_scale_out_uses_the_remaining_quantity():
    candles = _candles(4)
    candles[1] = replace(candles[1], high=102.5)
    settlement = candles[2].open_time + HOUR // 2
    proposal = replace(_proposal(risk=0.004), stop_loss=98.0, scale_out_enabled=True)
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
        proposal=proposal,
    )
    scale = next(fill for fill in result.fills if fill["kind"] == "scale_out")
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -(_entry_quantity(result) - scale["quantity"]) * 120.0 * 0.01
    )


def test_intrabar_exit_at_unknown_time_makes_settlement_unknown():
    candles = _candles()
    candles[1] = replace(candles[1], high=102.5)
    settlement = candles[1].open_time + HOUR // 2
    proposal = replace(_proposal(), take_profit=102.0)
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
        proposal=proposal,
    )
    assert result.exit_reason_distribution.get("target") == 1
    assert result.funding_known is False
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


def test_entry_open_settlement_precedes_intrabar_exit():
    candles = _candles(2)
    candles[1] = replace(candles[1], high=102.5)
    entry_open = candles[1].open_time
    intrabar = entry_open + 1
    result = _run(
        candles,
        _series(
            candles, rates={entry_open: 0.01, intrabar: 0.02},
            marks={entry_open: 120.0, intrabar: 130.0},
        ),
        proposal=replace(_proposal(), take_profit=102.0),
    )
    assert result.exit_reason_distribution.get("target") == 1
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -_entry_quantity(result) * 120.0 * 0.01
    )
    assert result.funding_known is False


def test_settlement_at_open_of_gap_exit_is_excluded():
    candles = _candles()
    candles[2] = replace(candles[2], open=89.0, high=90.0, low=88.0, close=89.0)
    exit_open = candles[2].open_time
    result = _run(
        candles, _series(candles, rates={exit_open: 0.01}, marks={exit_open: 120.0}),
    )
    assert result.exit_reason_distribution.get("stop") == 1
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(0.0)


def test_scale_out_open_settlement_uses_pre_exit_size():
    candles = _candles(3)
    candles[1] = replace(candles[1], high=102.5)
    settlement = candles[1].open_time
    proposal = replace(_proposal(risk=0.004), stop_loss=98.0, scale_out_enabled=True)
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
        proposal=proposal,
    )
    assert any(fill["kind"] == "scale_out" for fill in result.fills)
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -_entry_quantity(result) * 120.0 * 0.01
    )


def test_scale_out_at_open_excludes_closed_quantity_from_later_settlement():
    candles = _candles(3)
    candles[2] = replace(candles[2], open=102.5, high=103.0, low=102.0, close=102.5)
    exit_open = candles[2].open_time
    settlement = exit_open + HOUR // 2
    proposal = replace(_proposal(risk=0.004), stop_loss=98.0, scale_out_enabled=True)
    result = _run(
        candles,
        _series(
            candles, rates={exit_open: 0.02, settlement: 0.01},
            marks={exit_open: 130.0, settlement: 120.0},
        ),
        proposal=proposal,
    )
    scale = next(fill for fill in result.fills if fill["kind"] == "scale_out")
    assert scale["candle_open_time"] == candles[2].open_time
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -(_entry_quantity(result) - scale["quantity"]) * (130.0 * 0.02 + 120.0 * 0.01)
    )


def test_complete_funding_can_rescue_a_time_stop():
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    result = _run(
        candles, _series(candles, rates={settlement: -0.5}, marks={settlement: 120.0}),
        max_hold_hours=0.1,
    )
    protection = next(call for call in result.parity_log if call["call"] == "evaluate_profit_protection")
    assert result.funding_known is True
    assert protection["input"]["cumulative_funding_r"] > 1.0
    assert protection["output"]["phase"] == "time_stop_blocked_by_funding"


def test_final_candle_exposure_is_settled_before_forced_close():
    candles = _candles(2)
    settlement = candles[-1].open_time + HOUR // 2
    result = _run(
        candles, _series(candles, rates={settlement: 0.01}, marks={settlement: 120.0}),
    )
    assert result.exit_reason_distribution.get("final_close") == 1
    assert result.funding_known is True
    assert result.net_after_costs["funding_received"] == pytest.approx(
        -_entry_quantity(result) * 120.0 * 0.01
    )


def test_partial_funding_cannot_rescue_a_time_stop():
    candles = _candles()
    settlement = candles[1].open_time + HOUR // 2
    funding = _series(
        candles, rates={settlement: -0.5}, marks={settlement: 120.0},
        known_from_ms=candles[1].open_time + 1,
    )
    result = _run(candles, funding, max_hold_hours=0.1)
    protection = next(call for call in result.parity_log if call["call"] == "evaluate_profit_protection")
    assert result.net_after_costs["funding_received"] > 0
    assert result.funding_known is False
    assert protection["input"]["cumulative_funding_r"] == 0.0
    assert protection["output"]["phase"] == "time_stop"
    assert result.exit_reason_distribution.get("time_stop") == 1


@pytest.mark.parametrize("rate", [-0.05, 0.05])
def test_partial_funding_is_excluded_from_subsequent_position_sizing(rate):
    candles = _candles(4)
    settlement = candles[1].open_time + HOUR // 2
    funding = _series(
        candles, rates={settlement: rate}, marks={settlement: 120.0},
        known_from_ms=candles[1].open_time + 1,
    )

    def source(candle):
        return _proposal() if candle in (candles[0], candles[2]) else None

    partial = _run(candles, funding, source=source, max_hold_hours=0.1)
    no_funding = _run(candles, None, source=source, max_hold_hours=0.1)
    partial_entries = [fill for fill in partial.fills if fill["kind"] == "entry"]
    no_funding_entries = [fill for fill in no_funding.fills if fill["kind"] == "entry"]

    assert len(partial_entries) == len(no_funding_entries) == 2
    assert partial.funding_known is False
    assert partial.net_after_costs["funding_received"] == pytest.approx(
        -_entry_quantity(partial) * 120.0 * rate
    )
    assert partial_entries[1]["quantity"] == pytest.approx(no_funding_entries[1]["quantity"])
    candles = _candles(4)
    settlement = candles[1].open_time + HOUR // 2
    funding = _series(candles, rates={settlement: -0.05}, marks={settlement: 120.0})
    def source(candle):
        return _proposal() if candle in (candles[0], candles[2]) else None

    funded = _run(candles, funding, source=source, max_hold_hours=0.1)
    unfunded = _run(candles, _series(candles), source=source, max_hold_hours=0.1)
    funded_entries = [f for f in funded.fills if f["kind"] == "entry"]
    unfunded_entries = [f for f in unfunded.fills if f["kind"] == "entry"]
    assert len(funded_entries) == len(unfunded_entries) == 2
    assert funded_entries[1]["quantity"] > unfunded_entries[1]["quantity"]
    plans = [call for call in funded.parity_log if call["call"] == "futures_plan"]
    assert plans[1]["input"]["equity"] == pytest.approx(
        10000.0 + funded.net_after_costs["funding_received"]
    )


def test_direct_engine_rejects_overlapping_candles_but_accepts_gaps():
    candles = _candles()
    overlapping = [candles[0], replace(candles[1], open_time=candles[0].close_time - 1)]
    with pytest.raises(ValueError, match="overlap"):
        _run(overlapping, None)
    with pytest.raises(ValueError, match="overlap"):
        _run([candles[0], candles[0]], None)
    with pytest.raises(ValueError, match="duration"):
        _run([replace(candles[0], close_time=candles[0].open_time)], None)
    with pytest.raises(ValueError, match="duration"):
        _run([replace(candles[0], close_time=candles[0].close_time - 1)], None)
    gapped = _candles(spacing=2 * HOUR)
    result = _run(gapped, _series(gapped))
    assert result.funding_known is True
