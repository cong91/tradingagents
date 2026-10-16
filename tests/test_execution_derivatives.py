"""Unit tests for the derivatives executor (tradingagents/execution/derivatives.py).

The exchange is a fake injected through ``exchange_factory``; no real client
is built and no socket is opened (conftest ``_no_network`` would fail the test
anyway). The risk guard's clock is frozen. Every test pins the FR-D contract:
all three gate layers ship closed, and ANY default path (dry or live, with or
without confirm) is refused with an audit line and nothing sent.
"""

import json
from datetime import datetime, timezone

import ccxt
import pytest

import tradingagents.execution.risk as risk_module
from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.execution.derivatives import DerivativesExecutor
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

FROZEN_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


class FakeDerivExchange:
    """Offline ccxt stand-in that also records set_leverage and full
    create_order kwargs (bracket params must be assertable)."""

    def __init__(self, *, api_key="test-key", secret="test-secret", price=50_000.0,
                 create_order_error=None, bracket_error=None, fail_bracket_calls=0):
        self.apiKey = api_key
        self.secret = secret
        self.price = price
        self.create_order_error = create_order_error
        self.bracket_error = bracket_error
        self.fail_bracket_calls = fail_bracket_calls
        self.calls = []
        self.bracket_calls = []
        self.open_orders = []
        self._markets_loaded = False

    def fetch_ticker(self, symbol):
        self.calls.append(("fetch_ticker", symbol))
        return {"last": self.price, "close": self.price}

    def load_markets(self):
        self.calls.append(("load_markets",))
        self._markets_loaded = True
        return {
            "BTC/USDT:USDT": {
                "base": "BTC", "quote": "USDT", "settle": "USDT",
                "contract": True, "swap": True, "type": "swap", "active": True,
                "limits": {"cost": {"min": 10.0}},
            }
        }

    def amount_to_precision(self, symbol, amount):
        if not self._markets_loaded:
            raise ccxt.ExchangeError("fake markets not loaded")
        self.calls.append(("amount_to_precision", symbol, amount))
        return f"{float(amount):.6f}"

    def set_leverage(self, leverage, symbol, params=None):
        self.calls.append(("set_leverage", leverage, symbol))

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, order_type, side, amount, params))
        if params and params.get("clientOrderId", "").startswith("emergency:"):
            return {"id": f"emergency-{len(self.calls)}"}
        if params:
            if self.bracket_error is not None:
                raise self.bracket_error
            if self.fail_bracket_calls > 0:
                self.fail_bracket_calls -= 1
                raise ccxt.ExchangeError("protective leg unavailable")
        if self.create_order_error is not None:
            raise self.create_order_error
        order_id = f"deriv-{len(self.calls)}"
        return {"id": order_id}

    def fetch_open_orders(self, symbol):
        self.calls.append(("fetch_open_orders", symbol))
        return list(self.open_orders)

    def fetch_balance(self):
        self.calls.append(("fetch_balance",))
        return {"total": {"USDT": 1_000.0}, "free": {"USDT": 1_000.0}}


def freeze(monkeypatch):
    monkeypatch.setattr(risk_module, "_utc_now", lambda: FROZEN_NOW)


def make_executor(tmp_path, monkeypatch, *, config=None, factory=None, env=None):
    """An executor with tmp audit log, frozen risk clock, scrubbed env."""
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", raising=False)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    freeze(monkeypatch)
    merged = {"exec_log_path": str(tmp_path / "audit.jsonl")}
    merged.update(config or {})
    return DerivativesExecutor(config=merged, exchange_factory=factory or FakeDerivExchange)


def read_audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def gates_config(**extra):
    """Both derivatives gates armed in config (env stays a separate layer)."""
    return {"exec_derivatives": True, **extra}


def live_config(**extra):
    return gates_config(exec_live=True, **extra)


def live_env(**extra):
    return {"TRADINGAGENTS_EXEC_LIVE": "true", "TRADINGAGENTS_EXEC_DERIVATIVES": "true", **extra}


def bracket_kwargs(signal="Buy"):
    return {"stop_loss": 48_000.0, "take_profit": 55_000.0} if signal != "Sell" else {
        "stop_loss": 52_000.0, "take_profit": 45_000.0,
    }


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
    )
    assert order.side == "buy"
    assert order.market == "derivatives"
    assert order.leverage == 1.0
    assert order.cost == pytest.approx(250.0)  # margin cap
    assert order.quantity == pytest.approx(0.005)  # limited by margin, also within stop risk
    assert order.ccxt_symbol == "BTC/USDT:USDT"


def test_gated_plan_sell_opens_a_short_without_holdings(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=gates_config(risk_max_derivatives_exposure_pct=100.0),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )
    order = executor.plan_order(
        "BTC-USD", "Sell", PortfolioContext(cash=1_000.0), price=50_000.0,
        stop_loss=52_000.0, take_profit=45_000.0,
    )
    assert order.side == "sell"  # short: no held position required
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


def test_live_execute_places_market_order(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    order_id = executor.execute_order(order, confirm=True, portfolio=book)
    assert order_id.startswith("deriv-")
    assert ("create_order", "BTC/USDT:USDT", "market", "buy", "0.005000", None) in exchange.calls
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("load_markets") < kinds.index("amount_to_precision") < kinds.index("create_order")
    event = read_audit(tmp_path)[-1]
    assert event["order_id"].count("deriv-") == 2
    assert event["confirmed"] is True
    assert event["mode"] == "live"


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
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0, leverage=5.0,
        stop_loss=40_000.0, take_profit=60_000.0,
    )
    executor.execute_order(order, confirm=True, portfolio=book)
    leverage_calls = [call for call in exchange.calls if call[0] == "set_leverage"]
    assert leverage_calls == [("set_leverage", 5, "BTC/USDT:USDT")]
    kinds = [call[0] for call in exchange.calls]
    assert kinds.index("set_leverage") < kinds.index("create_order")


def test_live_execute_sets_leverage_even_at_one_x(tmp_path, monkeypatch):
    # 1x is still sent explicitly: without it the venue's per-symbol preset
    # would decide the real leverage.
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    executor.execute_order(order, confirm=True, portfolio=book)
    assert ("set_leverage", 1, "BTC/USDT:USDT") in exchange.calls


def test_live_without_credentials_is_denied(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(api_key="", secret="")
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
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
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    executor.execute_order(order, confirm=True, portfolio=book)
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "sentinel-key-123" not in text
    assert "sentinel-secret-456" not in text


# --- exchange-side brackets on derivatives (reduceOnly required) -----------------


def _live_bracket_order(executor, book):
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    return order.model_copy(update={"stop_loss": 48_000.0, "take_profit": 55_000.0})


@pytest.fixture(autouse=True)
def _recovery_db(tmp_path, monkeypatch):
    from server import db, paths

    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_initialized_for", None)


def _failed_bracket_task(tmp_path, monkeypatch, exchange):
    from server import job_store

    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    entry_id = executor.execute_order(order, confirm=True, portfolio=book)
    return executor, order, f"bracket:{order.ccxt_symbol}:{entry_id}", job_store


def test_partial_bracket_failure_persists_each_leg(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    _, order, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)

    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "pending"
    assert [leg["status"] for leg in task["legs"]] == ["pending", "pending"]
    assert task["attempts"] == 0

    position_key = f"{order.ccxt_symbol}:{order.side}:{task_id.rsplit(':', 1)[-1]}"
    position = store.load_position_state(position_key)
    assert position["entry_order_id"] == task_id.rsplit(":", 1)[-1]
    assert position["snapshot"]["side"] == "long"
    assert position["snapshot"]["entry"] == order.estimated_price
    assert position["snapshot"]["stop"] == order.stop_loss
    assert position["snapshot"]["targets"] == [order.take_profit]


def test_bracket_recovery_retries_after_restart(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, _, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    reloaded_executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )

    assert store.load_bracket_recovery(task_id)["status"] == "pending"
    assert reloaded_executor.recover_bracket(task_id) == "completed"
    assert store.load_bracket_recovery(task_id)["status"] == "completed"
    assert executor is not None


def test_recovery_reconciles_existing_leg_without_duplicate(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, _, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.open_orders = [{
        "id": "existing-stop", "side": "sell",
        "info": {"stopLossPrice": 48_000.0},
    }]

    assert executor.recover_bracket(task_id) == "completed"
    recovered = [call for call in exchange.calls if call[0] == "create_order" and call[5]]
    assert [call[5] for call in recovered[-1:]] == [{"takeProfitPrice": 55_000.0, "reduceOnly": True}]
    task = store.load_bracket_recovery(task_id)
    assert "existing-stop" in task["placed_ids"]


def test_completed_bracket_recovery_is_idempotent(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, _, task_id, _ = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    assert executor.recover_bracket(task_id) == "completed"
    send_count = len([call for call in exchange.calls if call[0] == "create_order"])

    assert executor.recover_bracket(task_id) == "completed"
    assert len([call for call in exchange.calls if call[0] == "create_order"]) == send_count


def test_repeated_recovery_failure_emergency_closes_reduce_only(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(fail_bracket_calls=1)
    executor, _, task_id, store = _failed_bracket_task(tmp_path, monkeypatch, exchange)
    exchange.bracket_error = ccxt.ExchangeError("protective leg still unavailable")

    assert executor.recover_bracket(task_id) == "emergency_closed"
    task = store.load_bracket_recovery(task_id)
    assert task["status"] == "emergency_closed"
    emergency = [call for call in exchange.calls if call[0] == "create_order"][-1]
    assert emergency[5]["reduceOnly"] is True
    assert emergency[5]["clientOrderId"] == f"emergency:{task_id}"


def test_live_bracket_long_closes_sell_with_reduce_only(tmp_path, monkeypatch):
    """A bracketed LONG entry is followed by TWO reduceOnly conditional legs
    (stop below + target above) routed through the venue's conditional-order
    surface via the unified flat stopLossPrice/takeProfitPrice params."""
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    executor.execute_order(order, confirm=True, portfolio=book)
    bracket_calls = [call for call in exchange.calls
                     if call[0] == "create_order" and call[5]]
    assert bracket_calls == [
        ("create_order", "BTC/USDT:USDT", "market", "sell", order.quantity,
         {"stopLossPrice": 48_000.0, "reduceOnly": True}),
        ("create_order", "BTC/USDT:USDT", "market", "sell", order.quantity,
         {"takeProfitPrice": 55_000.0, "reduceOnly": True}),
    ]
    actions = [event["action"] for event in read_audit(tmp_path)]
    assert "bracket_placed" in actions


def test_live_bracket_short_closes_buy_with_reduce_only(tmp_path, monkeypatch):
    """A SELL entry (a short opener) gets reduceOnly BUY conditional legs --
    the mirrored close side the spot path never needs: stop ABOVE entry,
    target BELOW."""
    exchange = FakeDerivExchange()
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = executor.plan_order("BTC-USD", "Sell", book, price=50_000.0,
                                stop_loss=52_000.0, take_profit=45_000.0)
    executor.execute_order(order, confirm=True, portfolio=book)
    bracket_calls = [call for call in exchange.calls
                     if call[0] == "create_order" and call[5]]
    assert bracket_calls == [
        ("create_order", "BTC/USDT:USDT", "market", "buy", order.quantity,
         {"stopLossPrice": 52_000.0, "reduceOnly": True}),
        ("create_order", "BTC/USDT:USDT", "market", "buy", order.quantity,
         {"takeProfitPrice": 45_000.0, "reduceOnly": True}),
    ]


def test_live_entry_without_bracket_is_refused_before_any_order(tmp_path, monkeypatch):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        env=live_env(),
    )
    order = executor.plan_order("BTC-USD", "Buy", PortfolioContext(cash=1_000.0),
                                price=50_000.0)
    assert order.side is None
    assert "explicit stop_loss" in order.reason



def test_bracket_failure_after_fill_is_audited_not_raised(tmp_path, monkeypatch):
    """The entry already filled: a bracket exchange error must never discard
    the fill or raise -- audited as bracket_failed, order id still returned."""
    exchange = FakeDerivExchange(bracket_error=ccxt.ExchangeError("stop price invalid"))
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    order_id = executor.execute_order(order, confirm=True, portfolio=book)
    assert order_id.startswith("deriv-")  # the entry fill stands
    event = read_audit(tmp_path)[-1]
    assert event["action"] == "bracket_failed"
    assert "stop price invalid" in event["reason"]


def test_exports_expose_the_executor():
    import tradingagents.execution as execution_package

    assert execution_package.DerivativesExecutor is DerivativesExecutor
