"""API tests for the control-panel portfolio endpoint."""

import json
from datetime import UTC, datetime

import pytest

from server import portfolio as portfolio_mod
from tests.server_api_exchange import _FakeExchange
from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


# --- portfolio (§6) ----------------------------------------------------------------


@pytest.fixture
def _futures_observed_at(monkeypatch):
    observed_at = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return observed_at

    monkeypatch.setattr("server.futures_snapshot.datetime", _FrozenDateTime)
    return observed_at.isoformat()


def _trade(when: str, action: str, qty: float, price: float, *,
           phase: str = "execute", confirmed: bool = True) -> dict:
    return {"timestamp": when, "ticker": "BTC-USD", "ccxt_symbol": "BTC/USDT",
            "signal": "Buy", "action": action, "qty": qty, "price_est": price,
            "mode": "dry", "confirmed": confirmed, "order_id": None,
            "phase": phase, "market": "spot", "leverage": 1.0}


def test_portfolio_dry_mode_reconstructs_positions_from_audit(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.0012, 82000.0)),
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.0008, 85000.0)),
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})  # exec_live stays False → dry, no ccxt

    body = client.get("/api/portfolio").json()
    assert body["generated_at"].endswith("Z")
    assert body["currency"] == "USDT"
    assert body["cash"] is None
    (position,) = body["positions"]
    assert position["ticker"] == "BTC-USD"
    assert position["quantity"] == pytest.approx(0.002)
    assert position["average_price"] == pytest.approx((0.0012 * 82000 + 0.0008 * 85000) / 0.002)
    assert position["basis_source"] == "fifo"
    assert position["marked_price"] is None
    assert position["market_value"] is None
    assert position["unrealized_pnl"] is None
    assert body["equity_est"] == 0.0  # unmarked positions count 0 (risk.py:381-403)
    assert body["realized_pnl"]["total"] == 0
    assert body["realized_pnl"]["closed_trades"] == 0
    assert body["halted_today"] is False
    assert body["warnings"]  # honest about the reconstruction
    assert body["stale_after"] == 30


def test_portfolio_dry_empty_audit_is_valid_empty_state(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path / "absent.jsonl")})
    body = client.get("/api/portfolio").json()
    assert body["cash"] is None
    assert body["positions"] == []
    assert body["equity_est"] is None
    assert body["realized_pnl"] == {
        "total": 0, "today": 0, "by_ticker": {}, "closed_trades": 0,
        "unknown_basis_closes": 0, "consecutive_losses": 0,
    }
    assert body["halted_today"] is False
    assert body["warnings"] == []
    assert body["futures"]["status"] == "unavailable"
    assert body["futures"]["positions"] == []
    assert body["futures"]["equity_currency"] is None
    assert body["futures"]["wallet_balance_currency"] is None
    assert body["futures"]["positions_read"] == {
        "status": "unavailable", "skipped": None, "issues": [], "observed_at": None,
    }


def test_portfolio_realized_pnl_is_fifo_replay_of_the_audit_log(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.001, 80000.0)),
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.001, 84000.0)),
        json.dumps(_trade(f"{today}T06:00:00+00:00", "sell", 0.0012, 86000.0)),
        # plan lines never count twice (risk.py:271-281)
        json.dumps(_trade(f"{today}T07:00:00+00:00", "sell", 0.0008, 90000.0, phase="plan")),
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/portfolio").json()
    expected = (86000 - 80000) * 0.001 + (86000 - 84000) * 0.0002  # FIFO: oldest lots first
    assert body["realized_pnl"]["total"] == pytest.approx(expected)
    assert body["realized_pnl"]["today"] == pytest.approx(expected)
    assert body["realized_pnl"]["by_ticker"] == {"BTC-USD": pytest.approx(expected)}
    assert body["realized_pnl"]["closed_trades"] == 1
    assert body["realized_pnl"]["unknown_basis_closes"] == 0

    (position,) = body["positions"]  # the leftover 0.0008 lot @ 84000 stays open
    assert position["quantity"] == pytest.approx(0.0008)
    assert position["average_price"] == pytest.approx(84000.0)


def test_portfolio_spot_realized_pnl_excludes_futures_fills(client, tmp_path):
    today = _today()
    spot = _trade(f"{today}T04:00:00+00:00", "buy", 0.01, 100.0)
    spot["ticker"] = "NEAR-USD"
    spot_close = _trade(f"{today}T05:00:00+00:00", "sell", 0.01, 110.0)
    spot_close["ticker"] = "NEAR-USD"
    futures_open = _trade(f"{today}T06:00:00+00:00", "sell", 0.01, 100.0)
    futures_open.update({"ticker": "NEAR-USD", "market": "derivatives", "leverage": 3.0})
    futures_close = _trade(f"{today}T07:00:00+00:00", "buy", 0.01, 150.0)
    futures_close.update({"ticker": "NEAR-USD", "market": "derivatives", "leverage": 3.0})
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("".join(json.dumps(event) + "\n" for event in (
        spot, spot_close, futures_open, futures_close,
    )), encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/portfolio").json()

    assert body["realized_pnl"]["total"] == pytest.approx(0.1)
    assert body["realized_pnl"]["today"] == pytest.approx(0.1)
    assert body["realized_pnl"]["by_ticker"] == {"NEAR-USD": pytest.approx(0.1)}
    assert body["realized_pnl"]["closed_trades"] == 1
    assert body["realized_pnl"]["consecutive_losses"] == 0


def test_portfolio_counts_unknown_basis_and_loss_streak(client, tmp_path):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text("\n".join([
        json.dumps(_trade(f"{today}T04:00:00+00:00", "sell", 0.001, 80000.0)),  # no open lot
        json.dumps(_trade(f"{today}T05:00:00+00:00", "buy", 0.001, 90000.0)),
        json.dumps(_trade(f"{today}T06:00:00+00:00", "sell", 0.001, 85000.0)),  # -5 close
    ]) + "\n", encoding="utf-8")
    set_config({"exec_log_path": str(audit_file)})

    body = client.get("/api/portfolio").json()
    assert body["realized_pnl"]["unknown_basis_closes"] == 1
    assert body["realized_pnl"]["closed_trades"] == 1
    assert body["realized_pnl"]["total"] == pytest.approx(-5.0)
    assert body["realized_pnl"]["consecutive_losses"] == 1
    assert body["positions"] == []


def test_portfolio_live_mode_reads_venue_and_marks_positions(client, tmp_path, monkeypatch):
    today = _today()
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text(
        json.dumps(_trade(f"{today}T04:00:00+00:00", "buy", 0.001, 80000.0)) + "\n",
        encoding="utf-8",
    )
    set_config({"exec_live": True, "exec_log_path": str(audit_file)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")  # second gate half

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_FakeExchange)  # price 83078, cash 9500

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["currency"] == "USDT"
    (position,) = body["positions"]
    assert position["ticker"] == "BTC-USD"
    assert position["quantity"] == pytest.approx(0.001)
    assert position["average_price"] == pytest.approx(80000.0)
    assert position["basis_source"] == "fifo"
    assert position["marked_price"] == pytest.approx(83078.0)
    assert position["market_value"] == pytest.approx(83.078)
    assert position["unrealized_pnl"] == pytest.approx((83078.0 - 80000.0) * 0.001)
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    assert body["warnings"] == []


@pytest.mark.parametrize(
    ("contract_size", "expected_quantity"),
    [(None, None), (1.0, -0.01), (0.1, -0.001)],
)
def test_portfolio_live_futures_are_separate_from_spot_equity(
    client, tmp_path, monkeypatch, contract_size, expected_quantity, _futures_observed_at,
):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _FuturesExchange(_FakeExchange):
        def fetch_balance(self, params=None):
            if params:
                return {"info": {
                    "totalWalletBalance": "2000.0",
                    "totalMarginBalance": "2100.0",
                    "availableBalance": "1800.0",
                    "totalUnrealizedProfit": "100.0",
                    "positions": [],
                }}
            return super().fetch_balance(params)

        def fapiPrivateV3GetPositionRisk(self, params):
            assert params == {}
            return [{
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "-0.01",
                "entryPrice": "100000",
                "markPrice": "99000",
                "notional": "-990",
                "positionInitialMargin": "330",
                "leverage": "3",
                "unRealizedProfit": "10",
                "marginAsset": "USDT",
            }]

        @property
        def markets_by_id(self):
            market = {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "quote": "USDT",
                      "settle": "USDT", "contract": True, "linear": True, "inverse": False}
            if contract_size is not None:
                market["contractSize"] = contract_size
            return {"BTCUSDT": [market]}

    exchange = _FuturesExchange()

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    futures = body["futures"]
    assert futures["status"] == "ok"
    assert futures["wallet_balance"] == 2000.0
    assert futures["equity"] == 2100.0
    assert futures["available_balance"] == 1800.0
    assert futures["unrealized_pnl"] == 100.0
    assert futures["account_scope"] == "derivatives"
    assert futures["equity_currency"] is None
    assert futures["wallet_balance_currency"] is None
    assert futures["positions_read"] == {
        "status": "complete", "skipped": 0, "issues": [],
        "observed_at": _futures_observed_at,
    }
    (position,) = futures["positions"]
    assert position["side"] == "short"
    assert position["contracts"] == pytest.approx(0.01)
    if expected_quantity is None:
        assert position["quantity"] is None
    else:
        assert position["quantity"] == pytest.approx(expected_quantity)
    assert position["entry_price"] == 100000.0
    assert position["mark_price"] == 99000.0
    assert position["notional"] == 990.0
    assert position["margin"] == 330.0
    assert position["leverage"] == 3.0
    assert position["price_currency"] == "USDT"
    assert position["notional_currency"] == "USDT"
    assert position["margin_currency"] == "USDT"
    assert position["unrealized_pnl_currency"] == "USDT"


def test_portfolio_futures_partial_rows_preserve_valid_position_without_trading(
    client, tmp_path, monkeypatch, _futures_observed_at,
):
    audit_file = tmp_path / "absent.jsonl"
    set_config({"exec_live": True, "exec_log_path": str(audit_file)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _PartialExchange(_FakeExchange):
        markets_by_id = {
            market_id: [{"id": market_id, "symbol": symbol, "quote": "USDT", "settle": "USDT",
                         "contract": True, "linear": True, "inverse": False, "contractSize": 0.1}]
            for market_id, symbol in (("BTCUSDT", "BTC/USDT:USDT"), ("ETHUSDT", "ETH/USDT:USDT"))
        }

        def fapiPrivateV3GetPositionRisk(self, params):
            assert params == {}
            return [
                {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-2",
                 "entryPrice": "100", "markPrice": "100", "notional": "-20"},
                {"symbol": "private secret", "positionSide": "BOTH", "positionAmt": "2"},
                {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"},
            ]

        def create_order(self, *args, **kwargs):
            pytest.fail("portfolio display must not create an order")

    exchange = _PartialExchange()

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    assert [position["ticker"] for position in body["positions"]] == ["BTC-USD"]
    assert body["realized_pnl"]["total"] == 0
    futures = body["futures"]
    assert futures["status"] == "ok"
    assert futures["equity"] == 9500.0
    assert futures["positions_read"] == {
        "status": "partial", "skipped": 1,
        "issues": [
            {"symbol": None, "reason": "scope_unverified"},
            {"symbol": None, "reason": "unknown_market"},
        ],
        "observed_at": _futures_observed_at,
    }
    (position,) = futures["positions"]
    assert position["symbol"] == "BTC/USDT:USDT"
    assert position["side"] == "short"
    assert position["contracts"] == 2
    assert position["quantity"] == pytest.approx(-0.2)
    assert position["notional"] == 20.0
    assert position["round_trip_fee"] == pytest.approx(0.02)
    assert position["round_trip_fee_currency"] == "USDT"
    assert position["funding"] is None
    assert position["opened_at"] is None
    assert position["held_hours"] is None
    assert "secret" not in response.text
    assert not audit_file.exists()


def test_portfolio_futures_positions_read_failure_preserves_account_and_spot(
    client, tmp_path, monkeypatch,
):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _FuturesFailure(_FakeExchange):
        def fapiPrivateV3GetPositionRisk(self, params):
            assert params == {}
            raise RuntimeError("private endpoint response leaked secret")

    exchange = _FuturesFailure()

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    assert [position["ticker"] for position in body["positions"]] == ["BTC-USD"]
    assert body["futures"]["status"] == "ok"
    assert body["futures"]["error"] is None
    assert body["futures"]["equity"] == 9500.0
    assert body["futures"]["wallet_balance"] == 9500.0
    assert body["futures"]["available_balance"] == 9500.0
    assert body["futures"]["unrealized_pnl"] == 0.0
    assert body["futures"]["positions"] == []
    assert body["futures"]["equity_currency"] is None
    assert body["futures"]["positions_read"] == {
        "status": "partial",
        "skipped": None,
        "issues": [{"symbol": None, "reason": "read_failed"}],
        "observed_at": None,
    }
    assert "secret" not in response.text
    assert body["warnings"] == []


def test_portfolio_futures_balance_read_failure_preserves_spot_snapshot(
    client, tmp_path, monkeypatch,
):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _FuturesFailure(_FakeExchange):
        def fetch_balance(self, params=None):
            if params:
                raise RuntimeError("private endpoint response leaked secret")
            return super().fetch_balance(params)

    exchange = _FuturesFailure()

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cash"] == 9500.0
    assert body["equity_est"] == pytest.approx(9500.0 + 83.078)
    assert [position["ticker"] for position in body["positions"]] == ["BTC-USD"]
    assert body["futures"]["status"] == "error"
    assert body["futures"]["positions"] == []
    assert body["futures"]["equity"] is None
    assert body["futures"]["equity_currency"] is None
    assert body["futures"]["positions_read"] == {
        "status": "unavailable", "skipped": None, "issues": [], "observed_at": None,
    }
    assert body["futures"]["error"] == "futures account read failed (RuntimeError)"
    assert "secret" not in response.text
    assert body["warnings"] == ["futures account read failed (RuntimeError)"]


def test_portfolio_live_position_without_basis_or_mark_is_honest(client, tmp_path, monkeypatch):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    exchange = _FakeExchange()
    exchange.fetch_ticker = lambda symbol: {}  # venue answers but cannot mark

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=lambda: exchange)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    body = client.get("/api/portfolio").json()
    (position,) = body["positions"]
    assert position["average_price"] is None  # no log → unknown basis (risk.py:15-17)
    assert position["basis_source"] is None
    assert position["marked_price"] is None
    assert position["unrealized_pnl"] is None
    assert body["equity_est"] == 9500.0  # unmarked counts 0, cash still counts
    assert any("cannot be marked" in warning for warning in body["warnings"])


def test_portfolio_live_spot_venue_failure_preserves_futures_status(client, tmp_path, monkeypatch):
    set_config({"exec_live": True})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")

    class _DeadVenue:
        def fetch_balance(self):
            raise RuntimeError("venue down")

        def fetch_ticker(self, symbol):
            raise RuntimeError("venue down")

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_DeadVenue)

    monkeypatch.setattr(portfolio_mod, "_build_bridge", build)
    response = client.get("/api/portfolio")
    assert response.status_code == 200
    assert response.json()["positions_source"] == "unavailable"
    assert response.json()["futures"]["status"] == "error"
    assert any("spot account read failed" in warning for warning in response.json()["warnings"])


def test_portfolio_unreadable_audit_log_is_503(client, tmp_path):
    set_config({"exec_log_path": str(tmp_path)})  # a directory: OSError on read
    response = client.get("/api/portfolio")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_log_unreadable"
