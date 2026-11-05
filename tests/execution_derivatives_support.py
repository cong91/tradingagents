"""Offline exchange, executor setup, and database isolation for derivatives tests."""

import json
from datetime import UTC, datetime

import ccxt
import pytest

import tradingagents.execution.risk as risk_module
from tradingagents.execution import futures_identity
from tradingagents.execution.derivatives import DerivativesExecutor

FROZEN_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


class FakeDerivExchange:
    """Offline ccxt stand-in that also records set_leverage and full
    create_order kwargs (bracket params must be assertable)."""

    def __init__(self, *, api_key="test-key", secret="test-secret", price=50_000.0,
                 create_order_error=None, bracket_error=None, fail_bracket_calls=0,
                 entry_response=None, hedged=False):
        self.apiKey = api_key
        self.secret = secret
        self.price = price
        self.create_order_error = create_order_error
        self.bracket_error = bracket_error
        self.fail_bracket_calls = fail_bracket_calls
        self.entry_response = entry_response
        self.hedged = hedged
        self.margin_mode_error = None
        self.calls = []
        self.bracket_calls = []
        self.open_orders = []
        self.orders_by_id = {}
        self.cancel_error = None
        self.cancel_response = {"status": "canceled"}
        self.emergency_error = None
        self.fetch_open_orders_error = None
        self._markets_loaded = False
        self.contracts = 0.0
        # The account-scope validators read these before any signed call: the
        # demo-profile private routes, the demo-mode options, and the market
        # metadata that maps raw V3 position rows.
        self.urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        self.options = {"enableDemoTrading": True, "sandboxMode": False}
        self.verbose = False
        self.markets_by_id = {"BTCUSDT": {
            "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "contract": True,
            "linear": True, "inverse": False, "quote": "USDT", "settle": "USDT",
            "settleId": "USDT",
        }}

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

    def set_margin_mode(self, margin_mode, symbol, params=None):
        self.calls.append(("set_margin_mode", margin_mode, symbol))
        if self.margin_mode_error is not None:
            raise self.margin_mode_error

    def fetch_position_mode(self, symbol=None, params=None):
        self.calls.append(("fetch_position_mode", symbol, params))
        return {"hedged": self.hedged, "info": {"dualSidePosition": self.hedged}}

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("fetch_positions", symbols, params))
        if self.contracts is None:
            raise ccxt.ExchangeError("position data unavailable")
        return [{
            "symbol": "BTC/USDT:USDT", "side": "long", "contracts": self.contracts,
            "entryPrice": self.price, "markPrice": self.price,
        }]

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.calls.append(("fapiPrivateV3GetPositionRisk", params))
        if self.contracts is None:
            raise ccxt.ExchangeError("position data unavailable")
        return [{
            "symbol": "BTCUSDT", "positionSide": "BOTH",
            "positionAmt": f"{self.contracts:.6f}",
            "entryPrice": f"{self.price:.2f}", "markPrice": f"{self.price:.2f}",
            "marginAsset": "USDT",
        }]

    def market(self, symbol):
        return {"quote": "USDT", "settle": "USDT", "inverse": False}

    def create_order(self, symbol, order_type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, order_type, side, amount, params))
        if params and params.get("clientOrderId", "").startswith("emergency-"):
            if self.emergency_error is not None:
                raise self.emergency_error
            return {"id": f"emergency-{len(self.calls)}", "status": "closed"}
        if params and ("stopLossPrice" in params or "takeProfitPrice" in params):
            if self.bracket_error is not None:
                raise self.bracket_error
            if self.fail_bracket_calls > 0:
                self.fail_bracket_calls -= 1
                raise ccxt.ExchangeError("protective leg unavailable")
        if self.create_order_error is not None:
            raise self.create_order_error
        order_id = f"deriv-{len(self.calls)}"
        if params and "stopLossPrice" in params:
            algo_type = "STOP_MARKET"
            trigger_price = params["stopLossPrice"]
        elif params and "takeProfitPrice" in params:
            algo_type = "TAKE_PROFIT_MARKET"
            trigger_price = params["takeProfitPrice"]
        else:
            return self.entry_response or {
                "id": order_id, "status": "closed", "filled": float(amount), "average": self.price,
            }
        algo_order = {
            "id": order_id, "symbol": symbol, "side": side,
            "amount": amount, "type": "market", "status": "open",
            "info": {
                "algoId": order_id, "symbol": "BTCUSDT", "side": side.upper(),
                "algoStatus": "NEW", "orderType": algo_type,
                "triggerPrice": str(trigger_price), "quantity": str(amount),
                "reduceOnly": True, "workingType": params.get("workingType"),
                "clientAlgoId": params.get("clientOrderId"),
            },
        }
        self.open_orders.append(algo_order)
        self.orders_by_id[order_id] = algo_order
        return {"id": order_id}

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("fetch_open_orders", symbol, since, limit, params))
        if self.fetch_open_orders_error is not None:
            raise self.fetch_open_orders_error
        return list(self.open_orders)

    def fetch_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("fetch_orders", symbol, since, limit, params))
        return list(self.orders_by_id.values())

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("fetch_order", order_id, symbol, params))
        if order_id in self.orders_by_id:
            return self.orders_by_id[order_id]
        for item in self.open_orders:
            if str(item.get("id") or item.get("info", {}).get("algoId")) == str(order_id):
                return item
        raise ccxt.OrderNotFound(f"unknown fake order {order_id}")

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel_order", order_id, symbol, params))
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"id": order_id, **self.cancel_response}

    def fetch_balance(self, params=None):
        self.calls.append(("fetch_balance",))
        # Shape of the live /fapi account payload: the totals are quoted in the
        # account's margin asset, which only ``assets`` names -- there is no
        # top-level ``asset`` key (probed on the demo venue 2026-11-05). A fake
        # that invents one hides the currency derivation from every execute
        # test, which is exactly how the live entry refusal shipped.
        return {"info": {
            "totalMarginBalance": "1000",
            "assets": [{"asset": "USDT", "walletBalance": "1000"}],
        }}


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
    return {
        "stop_loss": 48_000.0,
        "take_profit": 55_000.0,
        "futures_execution_intent": "enter",
    } if signal != "Sell" else {
        "stop_loss": 52_000.0,
        "take_profit": 45_000.0,
        "futures_execution_intent": "enter",
    }


def _live_bracket_order(executor, book):
    order = executor.plan_order("BTC-USD", "Buy", book, price=50_000.0, **bracket_kwargs())
    return order.model_copy(update={"stop_loss": 48_000.0, "take_profit": 55_000.0})


@pytest.fixture(autouse=True)
def _recovery_db(postgres_schema):
    """The derivatives executor persists bracket and position state in PostgreSQL."""
    return postgres_schema


# --- account-identity binding seam -------------------------------------------


VERIFIED_IDENTITY = futures_identity.FuturesAccountIdentity(
    exchange="binance", profile="demo", account_type="usds_futures",
    identity_hash=futures_identity._identity_hash("demo", "verified-test-account"),
)


def identity_for_api_key(api_key: str, profile: str = "demo"):
    """The identity a real read would bind for a client whose account alias
    equals ``api_key``: same key -> same digest, different key -> different."""
    return futures_identity.FuturesAccountIdentity(
        exchange="binance", profile=profile, account_type="usds_futures",
        identity_hash=futures_identity._identity_hash(profile, api_key),
    )


def api_key_identity(bridge):
    """bind_identity factory: derive the identity from the bridge's client apiKey."""
    return identity_for_api_key(bridge.client().apiKey)


def bind_identity(monkeypatch, value_or_factory) -> None:
    """Patch the one identity seam every production module reads through.

    ``value_or_factory`` is either a FuturesAccountIdentity (or None) returned
    for every bridge, or a callable receiving the bridge so tests can derive
    the identity from ``bridge.client().apiKey``.
    """
    if callable(value_or_factory):
        reader = value_or_factory
    else:
        value = value_or_factory

        def reader(bridge):
            return value

    monkeypatch.setattr(
        futures_identity, "read_futures_account_identity", reader,
    )
