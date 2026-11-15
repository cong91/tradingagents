"""Unit tests for the derivatives executor (tradingagents/execution/derivatives.py).

The exchange is a fake injected through ``exchange_factory``; no real client
is built and no socket is opened (conftest ``_no_network`` would fail the test
anyway). The risk guard's clock is frozen. Every test pins the FR-D contract:
Auto Futures and live mode ship closed; an entry additionally requires explicit
intent, per-order confirmation, and risk approval.
"""

import ccxt
import pytest

import tradingagents.execution.risk as risk_module
from server import job_store
from server.auto_modes import AutoMode
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _live_bracket_order,
    _recovery_db as _recovery_db,
    bind_identity,
    bracket_kwargs,
    enable_auto_futures as enable_auto_futures,
    gates_config,
    live_config,
    live_env,
    make_executor,
    read_audit,
)
from tradingagents.dataflows.config import set_config
from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.execution.auto_authorization import AutoAuthorizationError, mode_update_lock
from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


def test_every_default_path_is_refused(tmp_path, monkeypatch):
    """With Auto Futures disabled by default, no path plans or sends."""
    book = PortfolioContext(cash=1_000.0)

    # dry plan with mode closed: refused before metadata resolution
    executor = make_executor(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"

    # live mode cannot open Auto Futures
    executor = make_executor(
        tmp_path, monkeypatch,
        config={"exec_live": True},
        env=live_env(),
    )
    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"

    # A legacy derivatives env flag cannot open the new mode.
    executor = make_executor(
        tmp_path, monkeypatch,
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


def test_auto_futures_env_is_ignored(tmp_path, monkeypatch):
    """Neither the new mode key nor the removed gate is env-overridable."""
    monkeypatch.setenv("TRADINGAGENTS_AUTO_FUTURES_ENABLED", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    config = _apply_env_overrides(dict(DEFAULT_CONFIG))
    assert config.get("auto_futures_enabled") is not True

    executor = make_executor(
        tmp_path, monkeypatch,
        env={
            "TRADINGAGENTS_AUTO_FUTURES_ENABLED": "true",
            "TRADINGAGENTS_EXEC_DERIVATIVES": "true",
        },
    )
    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order(
            "BTC-USD", "Buy", PortfolioContext(cash=1_000.0),
            price=50_000.0, **bracket_kwargs(),
        )


def test_config_gate_never_opens_on_truthy_strings(tmp_path, monkeypatch):
    """Auto Futures only opens when its config value is the boolean True."""
    executor = make_executor(
        tmp_path, monkeypatch,
        config={"auto_futures_enabled": "true"},
    )
    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order(
            "BTC-USD", "Buy", PortfolioContext(cash=1_000.0),
            price=50_000.0, **bracket_kwargs(),
        )


def test_execute_requires_explicit_confirmation_when_auto_futures_is_open(
    tmp_path, monkeypatch,
):
    from tradingagents.execution.sizing import PlannedOrder

    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", side="buy",
        quantity=0.005, estimated_price=50_000.0, cost=250.0, market="derivatives",
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )

    with pytest.raises(PermissionError, match="confirm=True"):
        executor.execute_order(order, portfolio=PortfolioContext(cash=1_000.0))

    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


def test_tradeable_rating_without_explicit_intent_is_refused_before_market_resolution(
    tmp_path, monkeypatch,
):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    monkeypatch.setattr(
        executor,
        "resolve_market",
        lambda *_args, **_kwargs: pytest.fail("missing intent must refuse before market resolution"),
    )

    with pytest.raises(PermissionError, match="futures_execution_intent='enter'"):
        executor.plan_order(
            "BTC-USD", "Sell", PortfolioContext(cash=1_000.0), price=50_000.0,
            stop_loss=52_000.0, take_profit=45_000.0,
        )

    event = read_audit(tmp_path)[-1]
    assert event["action"] == "derivatives_denied"
    assert "investment rating alone" in event["reason"]


def test_crafted_derivatives_order_without_intent_is_refused_before_risk_or_send(
    tmp_path, monkeypatch,
):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
    )
    from tradingagents.execution.sizing import PlannedOrder

    crafted = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Sell", side="sell",
        quantity=0.005, estimated_price=50_000.0, cost=250.0, market="derivatives",
        stop_loss=52_000.0, take_profit=45_000.0,
    )

    with pytest.raises(PermissionError, match="futures_execution_intent='enter'"):
        executor.execute_order(crafted, confirm=True, portfolio=PortfolioContext(cash=1_000.0))

    assert exchange.calls == []
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


# --- plans once the gates are open -------------------------------------------


def test_gated_dry_plan_long(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    assert order.side == "buy"
    assert order.market == "derivatives"
    assert order.leverage == 1.0
    assert order.cost == pytest.approx(100.0)  # 10% total-budget allocation at 1x
    assert order.quantity == pytest.approx(0.002)
    assert order.ccxt_symbol == "BTC/USDT:USDT"


@pytest.mark.parametrize("signal", ["Sell", "Underweight"])
def test_gated_plan_bearish_rating_opens_a_short_only_with_explicit_entry_intent(
    tmp_path, monkeypatch, signal,
):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    order = executor.plan_order(
        "BTC-USD", signal, PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=52_000.0, take_profit=45_000.0,
        futures_execution_intent="enter",
    )
    assert order.side == "sell"
    assert order.futures_execution_intent == "enter"
    assert order.cost == pytest.approx(100.0)


def test_gated_plan_leverage_scales_notional_not_margin(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=5.0,
            risk_max_derivatives_exposure_pct=200.0,
            risk_max_total_exposure_pct=200.0,
            risk_max_position_pct_per_asset=200.0,  # per-asset sees notional 125%
        ),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0, leverage=5.0,
        stop_loss=45_000.0, take_profit=60_000.0,
        futures_execution_intent="enter",
    )
    assert order.leverage == 5.0
    assert order.cost == pytest.approx(20.0)  # $100 notional / 5x leverage
    assert order.quantity == pytest.approx(0.002)  # 10% total-budget allocation


def test_plan_hold_and_review_stay_no_orders(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
    )
    hold = executor.plan_order("BTC-USD", "Hold", PortfolioContext(cash=1_000.0), price=50_000.0)
    assert hold.side is None and hold.needs_review is False
    review = executor.plan_order("BTC-USD", "REVIEW", PortfolioContext(cash=1_000.0), price=50_000.0)
    assert review.needs_review is True


# The two executor-level soft-cap override plan tests moved to
# tests/test_execution_derivatives_override.py: this module's autouse
# _recovery_db fixture PostgreSQL-gates every test, and the override plan
# path needs no database -- there they run instead of skipping.


def test_plan_below_minimum_notional_is_refused(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=20.0), price=50_000.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )  # risk-sized notional is below the exchange minimum
    assert order.side is None
    assert "minimum" in order.reason


def test_plan_order_resolves_off_whitelist_override_through_metadata(tmp_path, monkeypatch):
    """A base outside the spot closed set still plans when the venue metadata
    lists an active USDT-settled contract for it (finding: the mark price and
    the contract must both come from the resolved unified symbol)."""
    exchange = FakeDerivExchange()
    exchange.load_markets = lambda: {
        "NEAR/USDT:USDT": {
            "base": "NEAR", "quote": "USDT", "settle": "USDT",
            "contract": True, "swap": True, "type": "swap", "active": True,
            "contractSize": 1.0, "linear": True, "inverse": False,
            "limits": {"cost": {"min": 10.0}},
        }
    }
    exchange.fetch_ticker = lambda symbol: {"last": 5.0, "close": 5.0}
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            exec_symbol_overrides={"NEAR-USD": "NEAR/USDT:USDT"},
            risk_max_derivatives_exposure_pct=100.0,
        ),
        factory=lambda: exchange,
    )
    order = executor.plan_order(
        "NEAR-USD", "Buy", PortfolioContext(cash=1_000.0), price=5.0,
        stop_loss=4.8, take_profit=5.5,
        futures_execution_intent="enter",
    )
    assert order.side == "buy"
    assert order.ccxt_symbol == "NEAR/USDT:USDT"  # the metadata unified key, not a synthesized one


def test_symbol_for_builds_unified_form_and_honors_overrides(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(exec_symbol_overrides={"BTC-USD": "BTC/USDC:USDC"}),
    )
    assert executor.symbol_for("BTC-USD") == "BTC/USDC:USDC"
    plain = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
    )
    assert plain.symbol_for("ETH-USD") == "ETH/USDT:USDT"


# --- risk caps on derivatives -------------------------------------------------


def test_plan_leverage_above_default_cap_returns_no_order_plan(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
    )  # default risk_max_derivatives_leverage = 1.0
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0, leverage=3.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    assert order.side is None
    assert "leverage" in order.reason
    assert read_audit(tmp_path)[-1]["action"] == "rejected_by_risk"


def test_plan_notional_above_zero_default_exposure_cap_is_rejected(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_leverage=5.0),
    )  # default risk_max_derivatives_exposure_pct = 0.0 blocks all notional
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0, leverage=3.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    assert order.side is None
    assert "derivatives notional" in order.reason


def test_plan_within_raised_caps_passes(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=3.0,
            risk_max_derivatives_exposure_pct=80.0,
            # the per-asset cap reads the NOTIONAL for derivatives:
            # 750 = 75% of equity 1000, so it must be raised above 75%
            risk_max_position_pct_per_asset=100.0,
        ),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0, leverage=3.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    # notional 750 = 75% per-asset (<= 100), 75% total exposure (<= 80) and
    # 75% derivatives exposure (<= 80)
    assert order.side == "buy"


# --- execution once the gates are open -----------------------------------------


@pytest.mark.usefixtures("enable_auto_futures")
def test_crafted_live_order_without_verified_contract_metadata_is_refused_before_mutation(
    tmp_path, monkeypatch,
):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    from tradingagents.execution.sizing import PlannedOrder

    crafted = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", side="buy",
        quantity=0.002, estimated_price=50_000.0, cost=100.0, market="derivatives",
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )

    with pytest.raises(PermissionError, match="contract metadata"):
        executor.execute_order(crafted, confirm=True, portfolio=PortfolioContext(cash=1_000.0))

    assert exchange.calls == []


@pytest.mark.usefixtures("enable_auto_futures")
def test_rounded_bracket_is_rejected_before_margin_or_leverage_mutation(
    tmp_path, monkeypatch,
):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    order = _live_bracket_order(executor, PortfolioContext(cash=1_000.0))
    original_price_precision = exchange.price_to_precision

    def collapse_stop(symbol, price):
        if price == order.stop_loss:
            return original_price_precision(symbol, order.estimated_price)
        return original_price_precision(symbol, price)

    monkeypatch.setattr(exchange, "price_to_precision", collapse_stop)

    with pytest.raises(PermissionError, match="bracket"):
        executor.execute_order(order, confirm=True, portfolio=PortfolioContext(cash=1_000.0))

    mutation_calls = {"set_margin_mode", "set_leverage", "create_order"}
    assert not [call for call in exchange.calls if call[0] in mutation_calls]


def test_dry_execute_with_confirm_sends_nothing(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    result = executor.execute_order(order, confirm=True, portfolio=book)
    assert result is None
    assert exchange.calls == [("load_markets",)]  # planning resolves venue metadata; dry execute sends nothing
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "buy"
    assert event["phase"] == "execute"
    assert event["market"] == "derivatives"  # the matcher's short-recognition field
    assert event["leverage"] == 1.0
    assert event["venue_key"] == "binance|https://demo-api.binance.com|False"


def test_live_caller_config_cannot_enable_futures_when_process_mode_closed(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )

    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.plan_order(
            "BTC-USD", "Buy", PortfolioContext(cash=1_000.0),
            price=50_000.0, **bracket_kwargs(),
        )

    assert exchange.calls == []
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_entry_is_refused_if_process_mode_closes_after_plan(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with mode_update_lock((AutoMode.FUTURES,)):
        set_config({"auto_futures_enabled": False})

    with pytest.raises(PermissionError, match="Auto Futures is disabled"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert exchange.calls == [("load_markets",)]
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_entry_rechecks_process_mode_at_submission(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    original_set_leverage = exchange.set_leverage

    def disable_before_submission(leverage, symbol, params=None):
        original_set_leverage(leverage, symbol, params)
        with mode_update_lock((AutoMode.FUTURES,)):
            set_config({"auto_futures_enabled": False})

    monkeypatch.setattr(exchange, "set_leverage", disable_before_submission)
    with pytest.raises(AutoAuthorizationError, match="Auto Futures is disabled or authorization is stale"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert ("set_leverage", 1, "BTC/USDT:USDT") in exchange.calls
    assert not [call for call in exchange.calls if call[0] == "create_order"]
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execute_places_market_order(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    entries = [call for call in exchange.calls if call[0] == "create_order" and call[3] == "buy"]
    assert len(entries) == 1
    assert entries[0][1:5] == ("BTC/USDT:USDT", "market", "buy", "0.002000")
    assert entries[0][5]["clientOrderId"].startswith("entry-")
    order_id = next(
        task["placed_ids"][0] for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    )
    assert order_id.startswith("deriv-")
    assert ("fetch_position_mode", "BTC/USDT:USDT", {"subType": "linear"}) in exchange.calls
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("load_markets") < kinds.index("amount_to_precision") < kinds.index("create_order")
    events = read_audit(tmp_path)
    event = next(
        item for item in events
        if item["action"] == "buy" and item.get("phase") == "execute"
    )
    assert event["order_id"] == order_id
    assert event["confirmed"] is True
    assert event["mode"] == "live"
    assert event["market"] == "derivatives"
    assert event["cost"] == pytest.approx(order.cost)
    assert events[-1]["order_id"].count("deriv-") == 2


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_entry_without_verified_account_is_refused(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    # No readable venue account: the fresh-account gate refuses before any
    # identity evidence could be bound, so nothing is sent.
    exchange.contracts = None
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(PermissionError, match="fresh futures account"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execute_rechecks_fresh_venue_book(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    stale = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", stale, price=50_000.0, **bracket_kwargs())
    exchange.contracts = 0.001
    # The fresh venue book is now readable and contradicts the stale plan: the
    # guard refuses on the position it reports instead of an unreadable account.
    with pytest.raises(PermissionError, match="already has an open derivatives position"):
        executor.execute_order(order, confirm=True, portfolio=stale)
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_risk_guard_rejects_position_added_after_analysis(tmp_path, monkeypatch):
    from dataclasses import replace

    from tradingagents.execution.futures_account import observe_futures_positions

    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    stale = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", stale, price=50_000.0, **bracket_kwargs())
    exchange.contracts = 0.001
    monkeypatch.setattr(
        "tradingagents.execution.futures_account.observe_futures_positions",
        lambda bridge: replace(observe_futures_positions(bridge), scope_complete=True),
    )
    with pytest.raises(PermissionError, match="already has an open derivatives position"):
        executor.execute_order(order, confirm=True, portfolio=stale)
    assert executor.risk_guard_decisions[-1]["stage"] == "execute"
    assert executor.risk_guard_decisions[-1]["allowed"] is False
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.usefixtures("enable_auto_futures")
@pytest.mark.parametrize(("equity", "expected_reason"), [
    (500, "derivatives notional would reach 50.00%"),
    (499, "derivatives notional would reach 50.10%"),
])
def test_fresh_equity_drives_execute_exposure_cap(tmp_path, monkeypatch, equity, expected_reason):
    from dataclasses import replace

    from tradingagents.execution.futures_account import observe_futures_positions

    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    exchange = FakeDerivExchange()
    exchange.fetch_balance = lambda params=None: {
        "info": {
            "totalMarginBalance": str(equity),
            "assets": [{"asset": "USDT", "walletBalance": str(equity)}],
        },
    }
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_exposure_pct=49.99,
            risk_max_position_pct_per_asset=200.0, risk_max_total_exposure_pct=200.0,
        ), factory=lambda: exchange, env=live_env(),
    )
    stale = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", stale, price=50_000.0,
        budget_fraction=0.25, **bracket_kwargs(),
    )
    monkeypatch.setattr(
        "tradingagents.execution.futures_account.observe_futures_positions",
        lambda bridge: replace(observe_futures_positions(bridge), scope_complete=True),
    )
    with pytest.raises(PermissionError, match=expected_reason):
        executor.execute_order(order, confirm=True, portfolio=stale)
    assert executor.risk_guard_decisions[-1]["allowed"] is False
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execute_rejects_unreadable_fresh_book(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    stale = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", stale, price=50_000.0, **bracket_kwargs())
    exchange.contracts = None
    with pytest.raises(PermissionError, match="fresh futures account"):
        executor.execute_order(order, confirm=True, portfolio=stale)
    assert not [call for call in exchange.calls if call[0] == "create_order"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_fresh_book_refusal_names_the_underlying_cause(tmp_path, monkeypatch):
    """The refusal must carry the cause, not only its type: five distinct
    ValueErrors and every venue error share this branch, and a bare type name
    left the 2026-11-04/05 entry refusals undiagnosable."""
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    stale = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", stale, price=50_000.0, **bracket_kwargs())
    exchange.contracts = None
    with pytest.raises(PermissionError) as excinfo:
        executor.execute_order(order, confirm=True, portfolio=stale)
    message = str(excinfo.value)
    assert message.startswith("fresh futures account or open orders unavailable: ")
    assert message != "fresh futures account or open orders unavailable: ExchangeError"
    assert "position data unavailable" in message
    denied = [
        event for event in read_audit(tmp_path)
        if event["action"] == "derivatives_denied"
    ]
    assert denied[-1]["reason"] == message


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_entry_in_hedge_mode_is_refused_before_any_send(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(hedged=True)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())

    with pytest.raises(RuntimeError, match="hedge mode"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert not [call for call in exchange.calls if call[0] == "create_order"]
    assert read_audit(tmp_path)[-1]["action"] == "rejected_at_execute"


@pytest.mark.usefixtures("enable_auto_futures")
def test_margin_mode_already_set_does_not_block_the_entry(tmp_path, monkeypatch):
    """Binance answers -4046 when the symbol already has that margin type:
    the desired end state, not a reason to refuse a protective entry."""
    exchange = FakeDerivExchange()
    exchange.margin_mode_error = ccxt.MarginModeAlreadySet(
        'binance {"code":-4046,"msg":"No need to change margin type."}'
    )
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, stop_loss=48_000.0,
        take_profit=55_000.0, margin_mode="isolated",
        futures_execution_intent="enter",
    )

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert ("set_margin_mode", "isolated", "BTC/USDT:USDT") in exchange.calls
    assert [call for call in exchange.calls if call[0] == "create_order" and call[5]]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execute_sets_leverage_before_create_order(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_leverage=5.0,
            risk_max_derivatives_exposure_pct=200.0,
            risk_max_total_exposure_pct=200.0,
            risk_max_position_pct_per_asset=200.0,  # per-asset sees notional 125%
        ),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=5.0,
        stop_loss=45_000.0, take_profit=60_000.0,
        futures_execution_intent="enter",
    )
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    leverage_calls = [call for call in exchange.calls if call[0] == "set_leverage"]
    assert leverage_calls == [("set_leverage", 5, "BTC/USDT:USDT")]
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("set_leverage") < kinds.index("create_order")


def test_agent_terms_drive_sizing_and_margin_mode(tmp_path, monkeypatch):
    """The agent's total-budget allocation and margin mode drive the plan."""
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_budget_fraction=0.10,
        ),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        leverage=2.0, stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        budget_fraction=0.05, margin_mode="isolated",
    )

    assert order.margin_mode == "isolated"
    assert order.leverage == 2.0
    # 5% of 1,000 = 50 notional; quantity = 50 / 50,000; margin = 50 / 2.
    assert order.quantity == pytest.approx(0.001)
    assert order.cost == pytest.approx(25.0)


def test_agent_budget_fraction_outside_structural_bounds_is_refused(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
        ),
    )
    book = PortfolioContext(cash=1_000.0)

    for invalid_budget in (0.0, 1.01):
        order = executor.plan_order(
            "BTC-USD", "Buy", book, price=50_000.0, leverage=1.0,
            stop_loss=48_000.0, take_profit=55_000.0,
            futures_execution_intent="enter", budget_fraction=invalid_budget,
        )
        assert order.side is None
        assert "outside (0, 1]" in (order.reason or "")

    bad_mode = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=1.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter", budget_fraction=0.1,
        margin_mode="portfolio",
    )
    assert bad_mode.side is None
    assert "margin_mode" in (bad_mode.reason or "")


def test_agent_budget_boolean_or_malformed_value_is_refused_without_conversion_error(
    tmp_path, monkeypatch,
):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
        ),
    )
    for invalid_budget in (True, "not-a-number"):
        order = executor.plan_order(
            "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
            leverage=1.0, stop_loss=48_000.0, take_profit=55_000.0,
            futures_execution_intent="enter", budget_fraction=invalid_budget,
        )
        assert order.side is None
        assert "sizing refused" in (order.reason or "")


def test_agent_budget_above_account_exposure_backstop_is_refused(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=5.0,
            risk_max_position_pct_per_asset=100.0,
            risk_max_total_exposure_pct=100.0,
        ),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        leverage=1.0, stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter", budget_fraction=0.10,
    )

    assert order.side is None
    assert "derivatives notional" in (order.reason or "")


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execution_sets_margin_mode_before_leverage_and_entry(tmp_path, monkeypatch):
    """Margin type can only change while flat, so it must precede the entry;
    it is pinned explicitly instead of trusting the venue's per-symbol preset."""
    exchange = FakeDerivExchange()
    exchange.set_margin_mode = lambda mode, symbol, params=None: exchange.calls.append(
        ("set_margin_mode", mode, symbol)
    )
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
        ),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=2.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        margin_mode="isolated",
    ).model_copy(update={"stop_loss": 48_000.0, "take_profit": 55_000.0})

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)

    kinds = [call[0] for call in exchange.calls]
    assert ("set_margin_mode", "isolated", "BTC/USDT:USDT") in exchange.calls
    assert kinds.index("set_margin_mode") < kinds.index("set_leverage")
    assert kinds.index("set_leverage") < kinds.index("create_order")


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execution_skips_margin_mode_when_the_plan_has_none(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.set_margin_mode = lambda *args, **kwargs: exchange.calls.append(
        ("set_margin_mode", *args)
    )
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)

    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)

    assert not [call for call in exchange.calls if call[0] == "set_margin_mode"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_without_credentials_is_denied(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(api_key="", secret="")
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(RuntimeError, match="BINANCE_DEMO_API_KEY"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert not any(call[0] == "create_order" for call in exchange.calls)


@pytest.mark.usefixtures("enable_auto_futures")
def test_exchange_error_is_audited_then_chained(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(create_order_error=ccxt.NetworkError("connection reset"))
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(RuntimeError) as excinfo:
        executor.execute_order(order, confirm=True, portfolio=book)
    assert isinstance(excinfo.value.__cause__, ccxt.NetworkError)
    assert read_audit(tmp_path)[-1]["action"] == "rejected_at_execute"


def test_execute_refuses_spot_orders(tmp_path, monkeypatch):
    from tradingagents.execution.sizing import PlannedOrder

    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
    )
    spot = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT", signal="Buy", side="buy",
        quantity=0.005, estimated_price=50_000.0, cost=250.0,
    )
    with pytest.raises(ValueError, match="derivatives orders only"):
        executor.execute_order(spot, confirm=True)


def test_execute_without_portfolio_is_refused_fail_closed(tmp_path, monkeypatch):
    # RiskGuard's percentage caps (leverage, derivatives notional) skip
    # fail-open without equity; on the derivatives path a None portfolio
    # must refuse BEFORE the guard runs, even with every gate open and
    # confirm given -- otherwise risk_max_derivatives_exposure_pct=0 is
    # silently disarmed.
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(PermissionError, match="portfolio"):
        executor.execute_order(order, confirm=True)  # gates open + confirm, no portfolio
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


def test_execute_with_unmarkable_portfolio_is_refused_fail_closed(tmp_path, monkeypatch):
    # Defense in depth: a portfolio that is present but yields no equity
    # (no cash, no positions) also makes the guard's percentage caps fail
    # open -- the derivatives path refuses it exactly like a None portfolio.
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(PermissionError, match="computable equity"):
        executor.execute_order(order, confirm=True, portfolio=PortfolioContext())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


def test_execute_fails_closed_when_audit_log_unreadable(tmp_path, monkeypatch):
    # Same rule as the spot execute re-check: an unreadable audit log hides
    # the halt/streak state a send decision needs, so it refuses even on the
    # dry path instead of passing fail-open.
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    monkeypatch.setattr(risk_module, "_read_events", lambda path, *, fail_closed=False: None)
    with pytest.raises(PermissionError, match="unreadable"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert exchange.calls == [("load_markets",)]
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "rejected_by_risk"
    assert "unreadable" in event["reason"]


@pytest.mark.usefixtures("enable_auto_futures")
def test_no_secrets_in_any_audit_line(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(api_key="sentinel-key-123", secret="sentinel-secret-456")
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sentinel-key-123" not in text
    assert "sentinel-secret-456" not in text


def test_exports_expose_the_executor():
    import tradingagents.execution as execution_package

    assert execution_package.DerivativesExecutor is DerivativesExecutor


@pytest.mark.usefixtures("enable_auto_futures")
def test_contract_metadata_converts_base_budget_to_venue_contracts(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(contract_size=0.01)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    assert order.quantity == pytest.approx(0.002)
    assert order.venue_contracts == pytest.approx(0.2)
    assert order.contract_size == pytest.approx(0.01)
    assert order.linear is True and order.inverse is False


@pytest.mark.parametrize("contract_size,linear,inverse", [(None, True, False), (0, True, False), (1, True, True), (1, False, False)])
def test_plan_rejects_missing_or_invalid_contract_metadata(
    tmp_path, monkeypatch, contract_size, linear, inverse,
):
    exchange = FakeDerivExchange(
        contract_size=contract_size, linear=linear, inverse=inverse,
    )
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        **bracket_kwargs(),
    )
    assert order.side is None
    assert "contract metadata" in (order.reason or "").lower()


@pytest.mark.usefixtures("enable_auto_futures")
def test_rounded_bracket_geometry_is_rejected_before_live_mutation(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.price_to_precision = lambda _symbol, value: f"{float(value):.0f}"
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, stop_loss=49_999.6,
        take_profit=50_000.4, futures_execution_intent="enter",
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    with pytest.raises(PermissionError, match="bracket"):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert not [call for call in exchange.calls if call[0] in {
        "set_margin_mode", "set_leverage", "create_order",
    }]


@pytest.mark.usefixtures("enable_auto_futures")
def test_live_execute_uses_fresh_equity_when_portfolio_is_missing(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        **bracket_kwargs(),
    )
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=None)
    assert any(call[0] == "create_order" for call in exchange.calls)
    assert any(item["stage"] == "execute_locked" for item in executor.risk_guard_decisions)
