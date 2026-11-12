"""Isolated runtime fixtures shared by autoloop API, scheduler and cycle tests."""

import socket
import threading
import weakref
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tradingagents.dataflows.screening as screening_mod
from server import (
    autoloop as autoloop_mod,
    autoloop_api,
    autoloop_futures_context,
    daily as daily_mod,
    health as health_mod,
    runs as runs_mod,
)
from server.contract import install_error_handlers
from tradingagents.dataflows.config import set_config
from tradingagents.execution import futures_account
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.forecast.block import ForecastBlock
from tradingagents.portfolio import PortfolioContext

_STOP, _TAKE = 80246.0, 86562.0
_STUB_PRICE = 83078.0
_STUB_CASH = 9500.0


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests (same shadow as
    tests/test_server_api.py: the event loop's Windows self-pipe is the only
    whitelisted socket; every other connect still refuses)."""
    if request.node.get_closest_marker("integration"):
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def marked_socketpair(*args, **kwargs):
        # Windows socketpair() performs its own loopback handshake through
        # socket.connect, so the refusal must step aside for its duration,
        # then re-arm. Only the pair it returns is whitelisted afterwards.
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        try:
            first, second = original_socketpair(*args, **kwargs)
        finally:
            socket.socket.connect = refuse_connect
            socket.socket.connect_ex = refuse_connect_ex
        selfpipe_sockets.add(first)
        selfpipe_sockets.add(second)
        return first, second

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect_ex)
    monkeypatch.setattr(socket, "socketpair", marked_socketpair)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


@pytest.fixture(autouse=True)
def _server_state(tmp_path, monkeypatch, postgres_schema):
    """Point all server state at tmp and reset the in-memory loop state."""
    if autoloop_mod._scheduler_thread is not None and autoloop_mod._scheduler_thread.is_alive():
        autoloop_mod.stop()
    scheduler_stop = threading.Event()
    monkeypatch.setattr(autoloop_mod, "_scheduler_stop", scheduler_stop)
    monkeypatch.setattr(autoloop_mod, "_last_cycle", None)
    monkeypatch.setattr(autoloop_mod, "_last_telemetry", None)
    monkeypatch.setattr(autoloop_mod, "_cycle_thread", None)
    monkeypatch.setattr(autoloop_mod, "_scheduler_thread", None)
    autoloop_mod._wakeup.clear()

    def refuse_real_exchange(*_args, **_kwargs):
        raise AssertionError("autoloop tests must inject a fake exchange")

    monkeypatch.setattr("tradingagents.execution.bridge._default_exchange_factory", refuse_real_exchange)
    # A run/job planted anywhere must never leak into a cycle's busy check.
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})
    set_config({"auto_spot_enabled": False, "auto_futures_enabled": False})
    yield
    scheduler_stop.set()
    wakeup_set = getattr(autoloop_mod._wakeup, "set", None)
    if wakeup_set is not None:
        wakeup_set()
    for thread in threading.enumerate():
        if thread.name == "server-autoloop-scheduler":
            thread.join(timeout=1.0)
            assert not thread.is_alive(), "autoloop scheduler leaked past test teardown"


def _test_app() -> FastAPI:
    test_app = FastAPI()
    install_error_handlers(test_app)
    test_app.include_router(autoloop_api.router)
    test_app.include_router(health_mod.router)
    return test_app


@pytest.fixture()
def client(_server_state):
    with TestClient(_test_app()) as test_client:
        yield test_client


class _StubVenue:
    """Fixture venue mirroring server/daily._StubVenue: constant answers, so
    sync_portfolio and plan_order never touch ccxt or the network."""

    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": _STUB_PRICE, "close": _STUB_PRICE}

    def load_markets(self) -> dict:
        return {}  # no market minimums: plan_order falls back to the rules floor

    def fetch_balance(self, params: dict | None = None) -> dict:
        # ``equity``/``info.asset`` are the futures wallet the cycle sizes
        # against; the spot ``total`` map is what sync_portfolio reads.
        return {
            "total": {"USDT": _STUB_CASH}, "free": {"USDT": _STUB_CASH},
            "equity": _STUB_CASH, "info": {"asset": "USDT"},
        }

    def fetch_positions(self, symbols=None, params: dict | None = None) -> list:
        return []

    def fetch_ohlcv(self, symbol: str, timeframe: str, **kwargs):
        duration = 3_600_000 if timeframe == "1h" else 14_400_000
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        open_ms = now_ms - (now_ms % duration) - duration
        return [[open_ms, _STUB_PRICE, _STUB_PRICE + 100,
                 _STUB_PRICE - 100, _STUB_PRICE + 1, 1.0]]


def _agent_proposal(**overrides) -> dict:
    """A complete FuturesExecutionProposal-shaped decision for the fixtures."""
    proposal = {
        "margin_mode": "isolated",
        "leverage": 3,
        "risk_fraction": 0.01,
        "margin_fraction": 0.25,
        "rationale": "Aligned closed 1h/4h candles, isolated collateral, 1% risk.",
    }
    proposal.update(overrides)
    return proposal


class _FakeGraph:
    """propagate() returns a fixed signal/state and records the calls it saw."""

    def __init__(self, signal: str, proposal: dict | None = None, intent: str = "enter",
                 note: str | None = None):
        self._signal = signal
        self._proposal = _agent_proposal() if proposal is None else proposal
        self._intent = intent
        self._note = note
        self.calls: list[tuple[str, str, str]] = []

    def propagate(self, ticker: str, trade_date: str, asset_type: str | None = None):
        self.calls.append((ticker, trade_date, str(asset_type)))
        # Mirror the real PM node (portfolio_manager.py:150-157): a proposal is
        # only surfaced with an explicit enter intent.
        proposal_state = self._proposal if self._intent == "enter" else {}
        state = {
            "trader_decision_telemetry": {
                "action": "Buy",
                "futures_execution_intent": "enter" if self._intent == "enter" else "abstain",
                "futures_execution_proposal_present": bool(proposal_state),
                "structured_output": True,
            },
            "pm_decision_telemetry": {
                "rating": self._signal,
                "futures_execution_intent": self._intent,
                "futures_execution_proposal_present": bool(proposal_state),
                "futures_execution_status": "structured",
                "structured_output": True,
            },
            "futures_execution_proposal": proposal_state,
            "futures_execution_intent": self._intent,
            "futures_execution_status": "structured",
        }
        if self._note is not None:
            state["final_trade_decision"] = self._note
        return state, self._signal


class _StubDerivVenue(_StubVenue):
    """Same constant answers as the spot stub, shaped for the derivatives
    executor: unified symbols, leverage pinning, precision."""

    candle_direction = "bullish"

    def load_markets(self):
        return {
            "BTC/USDT:USDT": {
                "base": "BTC", "quote": "USDT", "settle": "USDT",
                "contract": True, "swap": True, "type": "swap", "active": True,
                "linear": True,
            }
        }

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{float(amount):.6f}"

    def fetch_ohlcv(self, symbol: str, timeframe: str, **kwargs):
        duration = 3_600_000 if timeframe == "1h" else 14_400_000
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        open_ms = now_ms - (now_ms % duration) - duration
        # open, high, low, close per requested direction; a doji closes flat.
        close = {"bullish": _STUB_PRICE + 1, "bearish": _STUB_PRICE - 1,
                 "doji": _STUB_PRICE}[self.candle_direction]
        return [[open_ms, _STUB_PRICE, _STUB_PRICE + 100,
                 _STUB_PRICE - 100, close, 1.0]]

    def set_leverage(self, leverage: int, symbol: str, params=None) -> None:
        pass


def _derivatives_cycle_env(tmp_path, monkeypatch, *, config_extra=None, candle_direction="bullish"):
    """Enable Auto Futures with fake venues and an explicit standalone dry executor."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    # Fully stub the account read: the derivatives cycle needs a complete
    # equity picture, not a venue round-trip, and a real read would now demand
    # a validate_client-compatible Binance transport the stub venue lacks.
    monkeypatch.setattr(
        futures_account, 'read_futures_account',
        lambda bridge, **_kwargs: FuturesAccountSnapshot(
            equity=_STUB_CASH, wallet_balance=_STUB_CASH,
            available_balance=_STUB_CASH, unrealized_pnl=0.0,
            venue_profile="demo",
            equity_currency="USDT", wallet_balance_currency="USDT",
            available_balance_currency="USDT", unrealized_pnl_currency="USDT",
            positions=(), positions_scope_complete=True,
        ),
    )
    set_config({
        "auto_spot_enabled": False, "auto_futures_enabled": True, "exec_live": False,
        # Risk caps sized for the 3x plan the cycle requests: margin = 25% of
        # cash 9500 = 2375, notional at 3x = 7125 = 75% of equity — so every
        # percentage cap must sit at/above 75% and the leverage cap at 3x.
        "risk_max_derivatives_leverage": 3.0,
        "risk_max_derivatives_exposure_pct": 100.0,
        "risk_max_position_pct_per_asset": 100.0,
        "risk_max_total_exposure_pct": 100.0,
        "exec_log_path": str(engine_audit),
        **(config_extra or {}),
    })

    monkeypatch.setattr(screening_mod, "crypto_universe", lambda config, top_n: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: [])
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [{"ticker": "BTC-USD", "asset_type": "crypto"}])

    class _FakeRankingClient:
        def fetch_tickers(self):
            return {"BTC/USDT:USDT": {"quoteVolume": 1_000_000_000_000.0, "last": _STUB_PRICE}}

    monkeypatch.setattr(screening_mod, "_public_futures_ranking_client", lambda config: _FakeRankingClient())

    # This fixture deliberately does not forward the managed authorization: its
    # existing audit and bracket cases exercise the standalone dry-run path.
    def build(config):
        from tradingagents.execution.derivatives import DerivativesExecutor
        from tradingagents.execution.risk import RiskGuard, RiskLimits

        executor = DerivativesExecutor(
            config=config, exchange_factory=_StubDerivVenue,
            risk_guard=RiskGuard(
                limits=RiskLimits(
                    max_derivatives_leverage=float(config["risk_max_derivatives_leverage"]),
                    max_derivatives_exposure_pct=100.0,
                    max_position_pct_per_asset=100.0,
                    max_total_exposure_pct=100.0,
                ),
                price_of=lambda ticker: _STUB_PRICE,
            ),
        )
        assert executor.effective_mode() == "dry"
        return executor

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )
    monkeypatch.setattr(_StubDerivVenue, "candle_direction", candle_direction)
    # These cycle tests exercise execution and telemetry; model inference has
    # separate coverage, so supply a recorded forward forecast at that seam.
    monkeypatch.setattr(
        autoloop_futures_context.kronos_context,
        "forecast_for_cycle",
        lambda **kwargs: ForecastBlock(
            symbol=kwargs["symbol"], status="ok", generation_kind="forward",
        ),
    )
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices',
                        lambda ticker, direction="long": (
                            (_STUB_PRICE - 1_000.0, _STUB_PRICE + 1_000.0)
                            if direction == "long"
                            else (_STUB_PRICE + 1_000.0, _STUB_PRICE - 1_000.0)
                        ))
    monkeypatch.setattr(autoloop_futures_context, "_today_iso", lambda: "2026-10-07")
    return engine_audit


class _StubOffWhitelistDerivVenue(_StubDerivVenue):
    """Off-whitelist fixture: load_markets carries the NEAR/USDT:USDT swap and
    fetch_ticker answers per unified symbol so the test can prove which symbol
    the mark price was read from."""

    tickers_fetched: list[str] = []

    def load_markets(self):
        markets = super().load_markets()
        markets["NEAR/USDT:USDT"] = {
            "base": "NEAR", "quote": "USDT", "settle": "USDT",
            "contract": True, "swap": True, "type": "swap", "active": True,
        }
        return markets

    def fetch_ticker(self, symbol: str) -> dict:
        self.tickers_fetched.append(symbol)
        return {"last": 5.0, "close": 5.0}


class _NoopDerivatives:
    """Minimal executor surface for the proposal-validation paths."""

    def __init__(self):
        self._bridge = _NoopBridge()
        self.config = {}

    def symbol_for(self, ticker):
        return "BTC/USDT:USDT"

    def plan_order(self, *args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("plan_order must not run when the proposal is refused")


class _NoopBridge:
    def client(self):
        return _NoopVenue()

    def sync_portfolio(self):
        return PortfolioContext(cash=1_000.0, currency="USDT")


class _NoopVenue:
    def fetch_ticker(self, symbol):
        return {"last": 100.0, "close": 100.0}


def _market_context(ticker: str = "BTC-USD"):
    from tradingagents.dataflows.market_data import Candle

    now = int(datetime(2026, 10, 7, 8, tzinfo=UTC).timestamp() * 1000)
    one_hour = Candle("BTC/USDT:USDT", "1h", now - 3_600_000, now - 1, 100, 110, 90, 105, 1.0, True)
    four_hour = Candle("BTC/USDT:USDT", "4h", now - 14_400_000, now - 1, 100, 110, 90, 105, 1.0, True)
    return {
        "requested_ticker": ticker,
        "symbol": "BTC/USDT:USDT",
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": one_hour,
        "four_hour": four_hour,
        "one_hour_open": one_hour.open_time,
        "one_hour_close": one_hour.close,
        "four_hour_open": four_hour.open_time,
        "four_hour_close": four_hour.close,
        "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    }
