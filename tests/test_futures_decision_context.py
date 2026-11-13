"""Offline regression tests for futures decision-context preparation."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import (
    autoloop_analysis,
    autoloop_cycle_records,
    autoloop_futures_context,
    autoloop_futures_execution,
    futures_cycle_store,
    futures_cycle_telemetry,
    job_store,
)
from server.futures_decision_context import (
    build_futures_portfolio,
    effective_futures_leverage_cap,
    render_futures_decision_context,
)
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot, FuturesPosition
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.contract import GENERATION_FORWARD, STATUS_OK
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

BRACKETS = {"long": (90.0, 110.0), "short": (110.0, 90.0)}


def _market_context(**metrics):
    candle = SimpleNamespace(open=100, high=110, low=90, close=105)
    return {
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": candle,
        "four_hour": candle,
        "one_hour_open": "2026-10-26T07:00:00Z",
        "four_hour_open": "2026-10-26T04:00:00Z",
        "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
        **metrics,
    }


def _account(**values):
    return FuturesAccountSnapshot(**{
        "equity": 1_000.0,
        "available_balance": 900.0,
        "wallet_balance": 950.0,
        "unrealized_pnl": 50.0,
        "equity_currency": "USDT",
        "available_balance_currency": "USDT",
        **values,
    })


def _order(**values):
    return PlannedOrder(**{
        "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT:USDT", "signal": "Buy",
        "market": "derivatives", "side": "buy", "quantity": 2.0,
        "estimated_price": 100.0, "cost": 100.0, "leverage": 2.0,
        "futures_execution_intent": "enter",
        **values,
    })


def test_position_with_notional_and_missing_quantity_is_kept_in_book_and_caps(tmp_path):
    snapshot = _account(positions=(FuturesPosition(
        symbol="BTC/USDT:USDT", quantity=None, mark_price=100.0,
        notional=900.0, notional_currency="USDT",
    ),))
    book = build_futures_portfolio(snapshot)
    assert book.currency == "USDT"
    decision = RiskGuard(limits=RiskLimits(
        max_position_pct_per_asset=50.0,
        max_total_exposure_pct=50.0,
        max_derivatives_exposure_pct=50.0,
        max_derivatives_leverage=2.0,
    )).check(
        _order(cost=10.0, quantity=0.1, leverage=1.0, estimated_price=100.0),
        book,
        tmp_path / "audit.jsonl",
    )

    assert len(book.positions) == 1
    assert book.positions[0].quantity is None
    assert decision.allowed is False
    assert "per-asset" in decision.reason
    assert "total exposure" in decision.reason
    assert "derivatives notional" in decision.reason


def test_inverse_position_notional_is_converted_into_account_currency_before_caps(tmp_path):
    snapshot = _account(
        equity_currency="USD", available_balance_currency="USD",
        positions=(FuturesPosition(
            symbol="BTC/USD:BTC", quantity=0.02, mark_price=40_000.0,
            notional=0.02, notional_currency="BTC", price_currency="USD",
        ),),
    )
    book = build_futures_portfolio(snapshot)
    guard = RiskGuard(limits=RiskLimits(
        max_position_pct_per_asset=50.0,
        max_total_exposure_pct=50.0,
        max_derivatives_exposure_pct=50.0,
        max_derivatives_leverage=2.0,
    ))
    exposure = guard._position_exposure(book.positions[0], book.currency)
    decision = guard.check(
        _order(cost=10.0, quantity=0.00025, leverage=1.0, estimated_price=40_000.0),
        book,
        tmp_path / "audit.jsonl",
    )

    assert exposure == pytest.approx(800.0)
    assert book.currency == "USD"
    assert decision.allowed is False
    assert "per-asset" in decision.reason
    assert "total exposure" in decision.reason
    assert "derivatives notional" in decision.reason


def test_futures_portfolio_infers_currency_only_for_uniform_notional_currency(tmp_path):
    homogeneous = _account(
        equity_currency=None, available_balance_currency=None,
        positions=(FuturesPosition(
            symbol="BTC/USDT:USDT", quantity=1.0, notional=100.0,
            notional_currency="usdt",
        ),),
    )
    ambiguous = _account(
        equity_currency=None, available_balance_currency=None,
        positions=(
            FuturesPosition(
                symbol="BTC/USDT:USDT", quantity=1.0, notional=100.0,
                notional_currency="USDT",
            ),
            FuturesPosition(
                symbol="BTC/USD:USD", quantity=1.0, notional=100.0,
                notional_currency="USD",
            ),
        ),
    )

    homogeneous_book = build_futures_portfolio(homogeneous)
    ambiguous_book = build_futures_portfolio(ambiguous)

    assert homogeneous_book.currency == "USDT"
    assert ambiguous_book.currency is None
    decision = RiskGuard(limits=RiskLimits(
        max_position_pct_per_asset=100.0,
        max_total_exposure_pct=100.0,
        max_derivatives_exposure_pct=100.0,
        max_derivatives_leverage=2.0,
    )).check(
        _order(), ambiguous_book, tmp_path / "ambiguous-currency.jsonl",
    )
    assert decision.allowed is False
    assert "unknown or unconvertible exposure" in decision.reason


@pytest.mark.parametrize("quantity", [0.5, -0.5])
def test_build_futures_portfolio_keeps_equity_and_signed_position_exposure(quantity):
    book = build_futures_portfolio(_account(
        equity=420.5,
        available_balance=380.0,
        positions=(FuturesPosition(
            symbol="BTC/USDT:USDT", side="long" if quantity > 0 else "short",
            quantity=quantity, entry_price=50_000.0, mark_price=49_000.0,
            notional=24_500.0, notional_currency="USDT", leverage=3.0,
        ),),
    ))

    assert book.cash == pytest.approx(420.5)
    assert book.currency == "USDT"
    assert RiskGuard(price_of=lambda _: 49_000.0)._equity(book) == pytest.approx(420.5)
    assert len(book.positions) == 1
    position = book.position_in("BTC-USD")
    assert position is not None
    assert position.quantity == pytest.approx(quantity)
    assert position.average_price == pytest.approx(50_000.0)
    assert position.mark_price == pytest.approx(49_000.0)
    assert position.notional == pytest.approx(24_500.0)
    assert position.market == "derivatives"


def test_futures_sizing_book_uses_the_futures_wallet_not_spot_cash():
    book = build_futures_portfolio(_account(
        equity=420.5, wallet_balance=400.0, available_balance=380.0,
    ))

    assert book.cash == pytest.approx(420.5)
    assert book.currency == "USDT"
    assert book.positions == []


def test_futures_sizing_book_falls_back_to_available_balance():
    book = build_futures_portfolio(_account(equity=None, available_balance=125.0))

    assert book.cash == pytest.approx(125.0)
    assert RiskGuard()._equity(book) == pytest.approx(125.0)


@pytest.mark.parametrize("cap,reason", [
    ("max_position_pct_per_asset", "per-asset"),
    ("max_total_exposure_pct", "total exposure"),
    ("max_derivatives_exposure_pct", "derivatives notional"),
])
@pytest.mark.parametrize("quantity", [2.5, -2.5])
def test_futures_book_existing_positions_count_in_each_cap(tmp_path, cap, reason, quantity):
    book = build_futures_portfolio(_account(positions=(FuturesPosition(
        symbol="BTC/USDT:USDT", quantity=quantity, mark_price=100.0,
        notional=250.0, notional_currency="USDT", leverage=3.0,
    ),)))
    limits = RiskLimits(**{
        "max_position_pct_per_asset": 100.0,
        "max_total_exposure_pct": 100.0,
        "max_derivatives_exposure_pct": 100.0,
        "max_derivatives_leverage": 3.0,
        cap: 40.0,
    })
    guard = RiskGuard(limits=limits)
    decision = guard.check(_order(), book, tmp_path / "audit.jsonl")

    assert guard._equity(book) == pytest.approx(1_000.0)
    assert decision.allowed is False
    assert decision.halted is False
    assert reason in decision.reason
    assert "45.00%" in decision.reason


@pytest.mark.parametrize("use_notional", [True, False])
def test_gross_exposure_adds_all_same_asset_long_and_short_positions(tmp_path, use_notional):
    book = build_futures_portfolio(_account(positions=tuple(
        FuturesPosition(
            symbol="BTC/USDT:USDT", quantity=quantity, mark_price=100.0,
            notional=100.0 if use_notional else None,
            notional_currency="USDT" if use_notional else None,
        )
        for quantity in (1.0, -1.0)
    )))
    guard = RiskGuard(limits=RiskLimits(
        max_position_pct_per_asset=25.0, max_total_exposure_pct=25.0,
        max_derivatives_exposure_pct=25.0, max_derivatives_leverage=2.0,
    ))

    decision = guard.check(_order(leverage=1.0, quantity=1.0), book, tmp_path / "audit.jsonl")

    assert guard._equity(book) == pytest.approx(1_000.0)
    assert decision.allowed is False
    assert "per-asset" in decision.reason
    assert "total exposure" in decision.reason
    assert "derivatives notional" in decision.reason
    assert decision.reason.count("30.00%") == 3


def test_long_and_short_positions_share_per_asset_exposure_even_with_alias_tickers(tmp_path):
    book = build_futures_portfolio(_account(positions=(
        FuturesPosition(
            symbol="PEPE/USDT:USDT", quantity=1.0, mark_price=1.0, notional=1.0,
        ),
        FuturesPosition(
            symbol="PEPE/USDT:USDT", quantity=-1.0, mark_price=1.0, notional=1.0,
        ),
    )))
    limits = RiskLimits(
        max_position_pct_per_asset=0.25,
        max_total_exposure_pct=0.25,
        max_derivatives_exposure_pct=0.25,
        max_derivatives_leverage=2.0,
    )
    guard = RiskGuard(limits=limits)
    order = _order(ticker="PEPE24478-USD", cost=1.0, quantity=1.0, leverage=1.0, estimated_price=1.0)

    decision = guard.check(order, book, tmp_path / "audit.jsonl")

    assert decision.allowed is False
    assert "per-asset" in decision.reason
    assert "total exposure" in decision.reason
    assert "derivatives notional" in decision.reason
    assert "0.30%" in decision.reason

    position = book.position_in("PEPE24478-USD")
    assert position is not None
    assert position.market_symbol == "PEPE-USD"
    for ticker in ("PEPE-USD", "PEPE24478-USD"):
        legacy_ticker_decision = guard.check(
            _order(ticker=ticker, cost=1.0, quantity=1.0, leverage=1.0, estimated_price=1.0),
            book,
            tmp_path / f"{ticker}.jsonl",
        )
        assert legacy_ticker_decision.allowed is False
        assert "per-asset" in legacy_ticker_decision.reason


def test_futures_exposure_at_caps_is_allowed_without_multiplying_existing_leverage(tmp_path):
    # The held position sits on a DIFFERENT symbol on purpose: a new entry on
    # a held symbol is refused outright (one open position per symbol), while
    # this test pins the exposure arithmetic -- the held 250 notional counts
    # ONCE toward the totals, never margin x its own leverage again.
    book = build_futures_portfolio(_account(positions=(FuturesPosition(
        symbol="ETH/USDT:USDT", quantity=-2.5, mark_price=100.0,
        notional=250.0, leverage=3.0,
    ),)))
    guard = RiskGuard(limits=RiskLimits(
        max_position_pct_per_asset=45.0, max_total_exposure_pct=45.0,
        max_derivatives_exposure_pct=45.0, max_derivatives_leverage=2.0,
    ))

    decision = guard.check(_order(), book, tmp_path / "audit.jsonl")

    assert decision.allowed is True


@pytest.mark.parametrize("risk_cap,expected", [(1.0, 1.0), (1.5, 1.5), (3.0, 3.0), (5.0, 3.0)])
def test_render_context_uses_lowest_risk_guard_and_auto_leverage_cap(risk_cap, expected):
    effective_cap = effective_futures_leverage_cap(risk_guard_cap=risk_cap, auto_cap=3.0)
    context = render_futures_decision_context(
        _market_context(), _account(), "BTC-USD", {}, BRACKETS,
        max_leverage=effective_cap,
    )

    assert effective_cap == expected
    assert f"Maximum leverage: {expected:g}x" in context


def test_decision_context_reports_funding_and_open_interest():
    context = render_futures_decision_context(
        _market_context(
            funding_rate=0.0001, funding_datetime="2026-10-26T08:00:00Z",
            mark_price=737.06, open_interest=30_960_870_652.53, open_interest_value=2.2e10,
        ),
        _account(), "BNB-USD", {}, BRACKETS, max_leverage=1.0,
    )
    missing = render_futures_decision_context(
        _market_context(), _account(), "BNB-USD", {}, BRACKETS, max_leverage=1.0,
    )

    assert "Funding rate: 0.0100%" in context
    assert "2026-10-26T08:00:00Z" in context
    assert "Open interest: 30,960,870,652.5300" in context
    assert "Funding rate: unavailable" in missing
    assert "Open interest: unavailable" in missing


def test_decision_context_states_symmetric_enter_criteria_and_hard_limits():
    """The context tells the agents what a qualifying entry looks like —
    direction confirmation, executable bracket, reward/risk with funding
    carry, and the effective caps — so a strong setup is not met with an
    unexplained abstain, and Sell/Underweight is a short symmetric to a long."""
    config = {"risk_derivatives_max_risk_fraction": 0.01}
    context = render_futures_decision_context(
        _market_context(), _account(), "BTC-USD", config, BRACKETS, max_leverage=3.0,
    )

    assert "Enter criteria for this cycle" in context
    assert "the bar is symmetric for longs and shorts" in context
    assert "Sell/Underweight short" in context
    assert "Engine ATR bracket (long): stop=90.0 target=110.0" in context
    assert "Engine ATR bracket (short): stop=110.0 target=90.0" in context
    assert "Maximum leverage: 3x" in context
    assert "Maximum risk_fraction: 0.01" in context
    assert "propose inside the caps, never above them" in context
    assert "instead of defaulting to abstain" in context
    # The proposal-required contract stays intact.
    assert "A FuturesExecutionProposal is required for any entry" in context
    assert "no order is placed" in context


def test_unavailable_atr_brackets_are_named_not_silently_dropped():
    """Every other unavailable metric prints 'unavailable'; a bracket line that
    vanishes would read as criterion 2 failing and invite an unexplained
    abstain while the ATR source is empty. Naming it shifts the bracket duty
    to the agent's own proposal instead of closing the entry."""
    context = render_futures_decision_context(
        _market_context(), _account(), "BTC-USD", {},
        {"long": (None, None), "short": (None, None)}, max_leverage=3.0,
    )

    assert "Engine ATR bracket (long): unavailable" in context
    assert "Engine ATR bracket (short): unavailable" in context
    assert "if the brackets above are unavailable" in context
    assert "propose your own stop and take-profit together" in context
    assert "without both protective levels no order is placed" in context
    assert "not a reason to skip the analysis" in context


def test_context_discloses_protective_level_substitution():
    """The engine bracket substitutes only for an omitted stop/take-profit
    pair and is revalidated; size terms are refuse-don't-resize, and the
    context must say so instead of letting the agent claim engine prices."""
    context = render_futures_decision_context(
        _market_context(), _account(), "BTC-USD", {}, BRACKETS, max_leverage=3.0,
    )

    assert "the engine ATR bracket substitutes for those two protective levels only" in context
    assert "margin mode, leverage, and budgets are never substituted" in context
    assert "a proposal missing them is refused" in context


@pytest.mark.parametrize("risk_cap,expected", [(1.0, 1.0), (1.5, 1.5), (5.0, 3.0)])
def test_candidate_passes_open_positions_and_effective_guard_cap_without_database(
    monkeypatch, risk_cap, expected,
):

    account = _account(positions_scope_complete=True, positions=(FuturesPosition(
        symbol="BTC/USDT:USDT", quantity=-2.0, mark_price=100.0, notional=200.0,
    ),))
    seen = {}
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=risk_cap)),
    )
    forecast = ForecastBlock(
        symbol="BTC/USDT:USDT", status=STATUS_OK, direction="up",
        direction_counts={"up": 12, "down": 3, "flat": 1},
        direction_ratio={"up": 0.75, "down": 0.1875, "flat": 0.0625},
        band={"low": 100.0, "p50": 106.0, "high": 110.0},
        band_spread_ratio=10 / 105, reference_close=105.0,
        data_open_time=1_792_987_200_000, as_of="2026-10-26T08:00:00+00:00",
        horizon_hours=24, timeframe="4h", paths=16,
        generation_kind=GENERATION_FORWARD, temperature=0.5, profile="small_base",
        model_revision="NeoQuasar/Kronos-small@901c26c13326", artifact_hash="a" * 64,
        created_at="2026-10-26T08:00:00+00:00", latency_ms=100.0,
    )
    monkeypatch.setattr(
        autoloop_futures_context.kronos_context, "forecast_for_cycle", lambda **_: forecast,
    )
    monkeypatch.setattr(
        autoloop_futures_context, '_closed_futures_context',
        lambda *_: _market_context(four_hour_open=1_792_987_200_000),
    )
    monkeypatch.setattr(futures_account, 'read_futures_account', lambda *_args, **_kwargs: account)
    monkeypatch.setattr(job_store, "futures_position_stops", lambda: {})
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda _, direction: BRACKETS[direction])
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', lambda *_args, **_kwargs: None)

    class Graph:
        def propagate(self, ticker, trade_date, *, asset_type, execution_context):
            seen["context"] = execution_context
            return {"futures_execution_intent": "enter", "futures_execution_proposal": {}}, "Buy"

    def capture_plan(_executor, _ticker, _signal, _config, result, **kwargs):
        seen["portfolio"] = kwargs["portfolio"]
        result.reason = "planning captured offline"

    monkeypatch.setattr(autoloop_futures_execution, '_process_futures_candidate', capture_plan)
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"risk_max_derivatives_leverage": 3.0}, lambda *_: Graph(), executor,
    )

    assert result.reason == "planning captured offline"
    assert result.executed is False
    assert f"Maximum leverage: {expected:g}x" in seen["context"]
    assert seen["portfolio"].position_in("BTC-USD").quantity == pytest.approx(-2.0)
    assert executor.risk_guard._equity(seen["portfolio"]) == pytest.approx(1_000.0)


def test_build_futures_portfolio_attaches_position_stops():
    """The open-heat cap reads Position.stop_loss; the map is keyed exactly
    like the market_symbol build_futures_portfolio derives ("BASE-USD")."""
    snapshot = _account(positions=(FuturesPosition(
        symbol="BTC/USDT:USDT", quantity=1.0, mark_price=100.0,
        notional=100.0, notional_currency="USDT",
    ),))

    stopped = build_futures_portfolio(snapshot, position_stops={"BTC-USD": 48_000.0})
    assert stopped.positions[0].market_symbol == "BTC-USD"
    assert stopped.positions[0].stop_loss == pytest.approx(48_000.0)

    mismatched = build_futures_portfolio(snapshot, position_stops={"SOL-USD": 100.0})
    assert mismatched.positions[0].stop_loss is None

    assert build_futures_portfolio(snapshot).positions[0].stop_loss is None


def test_render_context_reports_current_notional_headroom_override_rules_and_heat():
    """The Hard limits block carries the numbers already computed (current
    notional, headroom) and spells the conditional override rule out, so the
    PM never has to infer what unlocks the hard cap."""
    config = {
        "risk_max_derivatives_exposure_pct": 25.0,
        "risk_max_derivatives_exposure_hard_pct": 35.0,
        "risk_derivatives_override_min_r": 2.0,
        "risk_derivatives_override_max_risk_fraction": 0.01,
        "risk_derivatives_override_max_per_day": 1,
        "risk_max_open_heat_pct": 6.0,
    }
    account = _account(positions=(FuturesPosition(
        symbol="ETH/USDT:USDT", quantity=1.5, mark_price=100.0,
        notional=150.0, notional_currency="USDT",
    ),))
    context = render_futures_decision_context(
        _market_context(), account, "BTC-USD", config, BRACKETS, max_leverage=3.0,
    )

    assert (
        "Current derivatives notional: 15.00% of equity | Cap: 25% | "
        "Remaining headroom: 10.00%" in context
    )
    assert (
        "Between the normal cap and the hard cap of 35%, an entry is allowed "
        "ONLY if ALL hold" in context
    )
    assert "closed 1h+4h candles confirm the direction" in context
    assert "reward/risk >= 2" in context
    assert "risk-at-stop <= 0.01" in context
    assert "funding is not adverse" in context
    assert "daily override budget (1) is not spent" in context
    assert "The executor verifies these mechanically" in context
    assert "refused, not resized" in context
    assert "Maximum open heat (total risk-at-stop across open positions): 6% of equity" in context


def test_render_context_hides_override_rules_and_heat_when_disabled():
    """hard = 0 and heat = 0 disable the mechanisms; describing them would
    invite the PM to propose entries the executor then refuses."""
    context = render_futures_decision_context(
        _market_context(), _account(), "BTC-USD", {}, BRACKETS, max_leverage=3.0,
    )

    assert "Between the normal cap" not in context
    assert "Maximum open heat" not in context
    # The current-notional line itself stays: soft cap 0 -> zero headroom.
    assert (
        "Current derivatives notional: 0.00% of equity | Cap: 0% | "
        "Remaining headroom: 0.00%" in context
    )


def test_render_context_reports_notional_unavailable_without_equity():
    account = _account(equity=None, available_balance=900.0, positions=(FuturesPosition(
        symbol="ETH/USDT:USDT", quantity=1.5, mark_price=100.0,
        notional=150.0, notional_currency="USDT",
    ),))
    context = render_futures_decision_context(
        _market_context(), account, "BTC-USD", {}, BRACKETS, max_leverage=3.0,
    )

    assert "Current derivatives notional: unavailable" in context


def test_render_context_reports_notional_unavailable_without_positive_equity():
    """A zero or negative equity is a broken account, not a debt-free one:
    RiskGuard derives an infinite deriv_pct from it and refuses every entry,
    so the context line must not fabricate remaining headroom."""
    for equity in (0.0, -12.5):
        account = _account(equity=equity, available_balance=900.0, positions=(FuturesPosition(
            symbol="ETH/USDT:USDT", quantity=1.5, mark_price=100.0,
            notional=150.0, notional_currency="USDT",
        ),))
        context = render_futures_decision_context(
            _market_context(), account, "BTC-USD", {}, BRACKETS, max_leverage=3.0,
        )
        assert "Current derivatives notional: unavailable" in context
        assert "Remaining headroom" not in context


def test_candidate_planning_passes_confirmation_and_funding_rate(monkeypatch):
    """The planning call carries the computed override evidence: confirmation
    from the closed 1h/4h pair and the venue funding rate read at market
    context time — the values the risk guard measures mechanically."""
    from server import autoloop

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    one_hour_ms, four_hour_ms = 3_600_000, 14_400_000
    one_open = now_ms - (now_ms % one_hour_ms) - one_hour_ms
    four_open = now_ms - (now_ms % four_hour_ms) - four_hour_ms
    candle_1h = SimpleNamespace(
        open=100, high=110, low=90, close=105,
        open_time=one_open, close_time=one_open + one_hour_ms,
    )
    candle_4h = SimpleNamespace(
        open=100, high=110, low=90, close=105,
        open_time=four_open, close_time=four_open + four_hour_ms,
    )
    context = {
        "requested_ticker": "BTC-USD",
        "symbol": "BTC/USDT:USDT",
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": candle_1h,
        "four_hour": candle_4h,
        "one_hour_open": datetime.fromtimestamp(one_open / 1000, UTC).isoformat(),
        "one_hour_close": candle_1h.close,
        "four_hour_open": datetime.fromtimestamp(four_open / 1000, UTC).isoformat(),
        "four_hour_close": candle_4h.close,
        "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
        "funding_rate": -0.0001,
    }
    book = PortfolioContext(cash=1_000.0, currency="USDT")
    seen: dict = {}
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: SimpleNamespace(
            fetch_ticker=lambda symbol: {"last": 100.0},
        )),
        risk_guard=RiskGuard(),
        risk_guard_decisions=[],
        config={"exec_auto_confirm": False},
    )

    def plan_order(ticker, signal, portfolio=None, **kwargs):
        seen["portfolio"] = portfolio
        seen.update(kwargs)
        return PlannedOrder(
            ticker=ticker, ccxt_symbol="BTC/USDT:USDT", signal=signal,
            side="buy", quantity=1.0, estimated_price=100.0, cost=100.0,
            market="derivatives", leverage=3.0, margin_mode="isolated",
            stop_loss=90.0, take_profit=110.0, futures_execution_intent="enter",
        )

    executor.plan_order = plan_order
    executor.audit_plan = lambda *args, **kwargs: None
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', lambda *_args, **_kwargs: None)
    # The terminal write goes straight through futures_cycle_store (not the patched
    # persist alias), so patch it at the module seam like the telemetry tests.
    monkeypatch.setattr(futures_cycle_store, "update_futures_cycle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autoloop_cycle_records, '_record_futures_cycle_rejection',
        lambda *args, **kwargs: seen.setdefault("rejections", []).append(kwargs),
    )
    proposal = {
        "margin_mode": "isolated", "leverage": 3,
        "risk_fraction": 0.01, "margin_fraction": 0.25,
        "rationale": "Aligned closed 1h/4h candles, isolated collateral, 1% risk.",
    }

    result = autoloop.DailyResult(ticker="BTC-USD")
    autoloop_futures_execution._process_futures_candidate(
        executor, "BTC-USD", "Buy", {}, result,
        context=context, proposal=proposal, portfolio=book,
        brackets=BRACKETS, intent="enter",
    )

    assert not seen.get("rejections")
    assert seen["confirmation_ok"] is True
    assert seen["funding_rate"] == pytest.approx(-0.0001)
    assert seen["futures_execution_intent"] == "enter"
    assert seen["portfolio"] is book
    assert result.reason == "awaiting approval"


def test_candidate_planning_passes_none_funding_when_the_venue_reports_none(monkeypatch):
    """No funding data -> the plan carries funding_rate=None (override
    eligibility fails downstream; the normal path is unaffected)."""
    from server import autoloop

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    one_hour_ms, four_hour_ms = 3_600_000, 14_400_000
    one_open = now_ms - (now_ms % one_hour_ms) - one_hour_ms
    four_open = now_ms - (now_ms % four_hour_ms) - four_hour_ms
    candle_1h = SimpleNamespace(
        open=100, high=110, low=90, close=105,
        open_time=one_open, close_time=one_open + one_hour_ms,
    )
    candle_4h = SimpleNamespace(
        open=100, high=110, low=90, close=105,
        open_time=four_open, close_time=four_open + four_hour_ms,
    )
    context = {
        "requested_ticker": "BTC-USD",
        "symbol": "BTC/USDT:USDT",
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": candle_1h,
        "four_hour": candle_4h,
        "one_hour_open": datetime.fromtimestamp(one_open / 1000, UTC).isoformat(),
        "one_hour_close": candle_1h.close,
        "four_hour_open": datetime.fromtimestamp(four_open / 1000, UTC).isoformat(),
        "four_hour_close": candle_4h.close,
        "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    }
    seen: dict = {}
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: SimpleNamespace(
            fetch_ticker=lambda symbol: {"last": 100.0},
        )),
        risk_guard=RiskGuard(),
        risk_guard_decisions=[],
        config={"exec_auto_confirm": False},
        audit_plan=lambda *args, **kwargs: None,
        plan_order=lambda ticker, signal, portfolio=None, **kwargs: (
            seen.update(kwargs),
            PlannedOrder(
                ticker=ticker, ccxt_symbol="BTC/USDT:USDT", signal=signal,
                side="buy", quantity=1.0, estimated_price=100.0, cost=100.0,
                market="derivatives", leverage=3.0, margin_mode="isolated",
                stop_loss=90.0, take_profit=110.0, futures_execution_intent="enter",
            ),
        )[1],
    )
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(futures_cycle_store, "update_futures_cycle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autoloop_cycle_records, '_record_futures_cycle_rejection',
        lambda *args, **kwargs: seen.setdefault("rejections", []).append(kwargs),
    )
    proposal = {
        "margin_mode": "isolated", "leverage": 3,
        "risk_fraction": 0.01, "margin_fraction": 0.25,
        "rationale": "Aligned closed 1h/4h candles, isolated collateral, 1% risk.",
    }

    result = autoloop.DailyResult(ticker="BTC-USD")
    autoloop_futures_execution._process_futures_candidate(
        executor, "BTC-USD", "Buy", {}, result,
        context=context, proposal=proposal,
        portfolio=PortfolioContext(cash=1_000.0, currency="USDT"),
        brackets=BRACKETS, intent="enter",
    )

    assert seen["confirmation_ok"] is True
    assert seen["funding_rate"] is None
