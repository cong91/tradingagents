"""Unit tests for the derivatives executor (tradingagents/execution/derivatives.py).

The exchange is a fake injected through ``exchange_factory``; no real client
is built and no socket is opened (conftest ``_no_network`` would fail the test
anyway). The risk guard's clock is frozen. Every test pins the FR-D contract:
all three gate layers ship closed, and ANY default path (dry or live, with or
without confirm) is refused with an audit line and nothing sent.
"""

import ccxt
import pytest

import tradingagents.execution.risk as risk_module
from server import job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _live_bracket_order,
    _recovery_db as _recovery_db,
    bind_identity,
    bracket_kwargs,
    gates_config,
    live_config,
    live_env,
    make_executor,
    read_audit,
)
from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


def test_every_default_path_is_refused(tmp_path, monkeypatch):
    """PRD requirement: with the gates at defaults, no path plans or sends."""
    book = PortfolioContext(cash=1_000.0)

    # dry plan, no gates: refused on the config layer
    executor = make_executor(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="exec_derivatives"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"

    # live plan, env flag true but the config layer closed: refused again
    executor = make_executor(
        tmp_path, monkeypatch,
        config={"exec_live": True},
        env=live_env(),
    )
    with pytest.raises(PermissionError, match="exec_derivatives"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"

    # dry execute (crafted order, no confirm): refused on the confirm layer
    from tradingagents.execution.sizing import PlannedOrder

    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    crafted = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", side="buy",
        quantity=0.005, estimated_price=50_000.0, cost=250.0, market="derivatives",
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    with pytest.raises(PermissionError, match="confirm=True"):
        executor.execute_order(crafted, portfolio=PortfolioContext(cash=1_000.0))
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"

    # live execute with confirm but derivatives config gate closed: refused
    executor = make_executor(
        tmp_path, monkeypatch,
        config={"exec_live": True},
        env={"TRADINGAGENTS_EXEC_LIVE": "true", "TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    with pytest.raises(PermissionError, match="exec_derivatives"):
        executor.execute_order(crafted, confirm=True)
    assert read_audit(tmp_path)[-1]["action"] == "derivatives_denied"


def test_env_gate_read_fail_closed(tmp_path, monkeypatch):
    """Config armed but the env flag missing/garbage keeps everything closed."""
    book = PortfolioContext(cash=1_000.0)
    for env_value in (None, "false", "0", "garbage"):
        executor = make_executor(
            tmp_path, monkeypatch, config=gates_config(),
            env={"TRADINGAGENTS_EXEC_DERIVATIVES": env_value} if env_value else {},
        )
        with pytest.raises(PermissionError, match="TRADINGAGENTS_EXEC_DERIVATIVES"):
            executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())


def test_config_gate_never_opens_on_truthy_strings(tmp_path, monkeypatch):
    """The config gate only opens on a real boolean, like exec_live."""
    book = PortfolioContext(cash=1_000.0)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(exec_derivatives="true"),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    with pytest.raises(PermissionError, match="exec_derivatives"):
        executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())


def test_config_gate_env_var_is_not_a_config_override(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    config = _apply_env_overrides(dict(DEFAULT_CONFIG))
    assert config["exec_derivatives"] is False


def test_tradeable_rating_without_explicit_intent_is_refused_before_market_resolution(
    tmp_path, monkeypatch,
):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
    )
    assert order.side == "buy"
    assert order.market == "derivatives"
    assert order.leverage == 1.0
    assert order.cost == pytest.approx(250.0)  # margin cap
    assert order.quantity == pytest.approx(0.005)  # limited by margin, also within stop risk
    assert order.ccxt_symbol == "BTC/USDT:USDT"


@pytest.mark.parametrize("signal", ["Sell", "Underweight"])
def test_gated_plan_bearish_rating_opens_a_short_only_with_explicit_entry_intent(
    tmp_path, monkeypatch, signal,
):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    order = executor.plan_order(
        "BTC-USD", signal, PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=52_000.0, take_profit=45_000.0,
        futures_execution_intent="enter",
    )
    assert order.side == "sell"
    assert order.futures_execution_intent == "enter"
    assert order.cost == pytest.approx(250.0)


def test_gated_plan_leverage_scales_notional_not_margin(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=5.0,
            risk_max_derivatives_exposure_pct=200.0,
            risk_max_total_exposure_pct=200.0,
            risk_max_position_pct_per_asset=200.0,  # per-asset sees notional 125%
        ),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0, leverage=5.0,
        stop_loss=40_000.0, take_profit=60_000.0,
        futures_execution_intent="enter",
    )
    assert order.leverage == 5.0
    assert order.cost == pytest.approx(10.0)  # $50 risk budget at $10,000 stop distance
    assert order.quantity == pytest.approx(0.001)  # risk budget / 10,000 stop distance


def test_plan_hold_and_review_stay_no_orders(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    hold = executor.plan_order("BTC-USD", "Hold", PortfolioContext(cash=1_000.0), price=50_000.0)
    assert hold.side is None and hold.needs_review is False
    review = executor.plan_order("BTC-USD", "REVIEW", PortfolioContext(cash=1_000.0), price=50_000.0)
    assert review.needs_review is True


def test_plan_below_minimum_notional_is_refused(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    assert executor.symbol_for("BTC-USD") == "BTC/USDC:USDC"
    plain = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    assert plain.symbol_for("ETH-USD") == "ETH/USDT:USDT"


# --- risk caps on derivatives -------------------------------------------------


def test_plan_leverage_above_default_cap_returns_no_order_plan(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch, config=gates_config(),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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


def test_dry_execute_with_confirm_sends_nothing(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
    assert entries[0][1:5] == ("BTC/USDT:USDT", "market", "buy", "0.005000")
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


@pytest.mark.parametrize(("equity", "expected_reason"), [
    (500, "derivatives notional would reach 50.00%"),
    (499, "derivatives notional would reach 50.10%"),
])
def test_fresh_equity_drives_execute_exposure_cap(tmp_path, monkeypatch, equity, expected_reason):
    from dataclasses import replace

    from tradingagents.execution.futures_account import observe_futures_positions

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
    order = executor.plan_order("BTC-USD", "Buy", stale, price=50_000.0, **bracket_kwargs())
    monkeypatch.setattr(
        "tradingagents.execution.futures_account.observe_futures_positions",
        lambda bridge: replace(observe_futures_positions(bridge), scope_complete=True),
    )
    with pytest.raises(PermissionError, match=expected_reason):
        executor.execute_order(order, confirm=True, portfolio=stale)
    assert executor.risk_guard_decisions[-1]["allowed"] is False
    assert not [call for call in exchange.calls if call[0] == "create_order"]


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
        stop_loss=40_000.0, take_profit=60_000.0,
        futures_execution_intent="enter",
    )
    with pytest.raises(RuntimeError, match="durable intent retained"):
        executor.execute_order(order, confirm=True, portfolio=book)
    leverage_calls = [call for call in exchange.calls if call[0] == "set_leverage"]
    assert leverage_calls == [("set_leverage", 5, "BTC/USDT:USDT")]
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("set_leverage") < kinds.index("create_order")


def test_agent_terms_drive_sizing_and_margin_mode(tmp_path, monkeypatch):
    """The decision agents' risk/margin budgets and margin mode are what the
    plan uses -- the configured defaults are only the fallback."""
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_risk_fraction=0.01,
            risk_derivatives_margin_fraction=0.25,
        ),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        leverage=2.0, stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        risk_fraction=0.002, margin_fraction=0.05, margin_mode="isolated",
    )

    assert order.margin_mode == "isolated"
    assert order.leverage == 2.0
    # risk budget 1000 * 0.002 = 2 over a 2,000 stop distance -> 0.001
    # margin budget 1000 * 0.05 * 2 / 50,000 -> 0.002; the risk leg is tighter.
    assert order.quantity == pytest.approx(0.001)
    assert order.cost == pytest.approx(25.0)


def test_agent_terms_above_the_hard_caps_are_refused(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_leverage=2.0,
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_max_risk_fraction=0.02,
            risk_derivatives_max_margin_fraction=0.5,
        ),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    book = PortfolioContext(cash=1_000.0)

    over_risk = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=1.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        risk_fraction=0.5,
    )
    assert over_risk.side is None
    assert "hard cap" in (over_risk.reason or "")

    over_margin = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=1.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        margin_fraction=0.9,
    )
    assert over_margin.side is None
    assert "hard cap" in (over_margin.reason or "")

    bad_mode = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=1.0,
        stop_loss=48_000.0, take_profit=55_000.0,
        futures_execution_intent="enter",
        margin_mode="portfolio",
    )
    assert bad_mode.side is None
    assert "margin_mode" in (bad_mode.reason or "")


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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
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
