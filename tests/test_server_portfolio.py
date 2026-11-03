"""Unknown position quantities remain explicit in portfolio API responses."""

from types import SimpleNamespace

import pytest

from server import portfolio as portfolio_mod
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio import PortfolioContext, Position

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("marked_price", [50.0, None])
def test_portfolio_unknown_quantity_preserves_unknown_value_and_equity(
    tmp_path, monkeypatch, marked_price,
):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "audit.jsonl")})
    book = PortfolioContext(
        cash=1_000.0,
        currency="USDT",
        positions=[
            Position(ticker="BTC-USD", quantity=None),
            Position(ticker="ETH-USD", quantity=2.0),
        ],
    )
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: book,
        fetch_price=lambda ticker: marked_price if ticker == "BTC-USD" else 10.0,
    )
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda config: bridge)
    futures = {
        "equity": None,
        "wallet_balance": None,
        "available_balance": None,
        "unrealized_pnl": None,
        "venue_profile": None,
        "equity_currency": None,
        "wallet_balance_currency": None,
        "available_balance_currency": None,
        "unrealized_pnl_currency": None,
        "account_scope": "derivatives",
        "positions": [],
        "positions_read": {
            "status": "partial", "skipped": 0,
            "issues": [{"symbol": None, "reason": "scope_unverified"}],
            "observed_at": "2026-11-02T12:00:00+00:00",
        },
        "status": "ok",
        "error": None,
    }
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda bridge: futures)
    monkeypatch.setattr(portfolio_mod, "_fifo_basis", lambda lots: (40.0, "fifo"))

    body = portfolio_mod.get_portfolio()

    unknown, known = body["positions"]
    assert unknown["quantity"] is None
    assert unknown["marked_price"] == marked_price
    assert unknown["market_value"] is None
    assert unknown["unrealized_pnl"] is None
    assert known["market_value"] == 20.0
    assert body["cash"] == 1_000.0
    assert body["equity_est"] is None
    assert body["futures"]["status"] == "ok"
    assert body["futures"]["positions_read"] == {
        "status": "partial", "skipped": 0,
        "issues": [{"symbol": None, "reason": "scope_unverified"}],
        "observed_at": "2026-11-02T12:00:00+00:00",
    }
    assert body["futures"]["positions"] == []
    assert any("quantity" in warning and "BTC-USD" in warning for warning in body["warnings"])
