"""Holding-cost wiring behind the portfolio's "Giữ / phí" column.

Only the round-trip fee is displayed, and only from the notional the snapshot
itself proved, carried with the notional's currency. Held time, the stored
``opened_at`` and funding are deliberately unknown: position states carry no
durable account binding, so the display path must not pair a venue position
with state history (data contract §6.5) — these tests pin that boundary.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import portfolio as portfolio_mod, position_cost as position_cost_module
from server.position_cost import attach_position_costs
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _futures_payload(**position_changes) -> dict:
    position = {
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "contracts": 0.01,
        "quantity": -0.01,
        "entry_price": 100_000.0,
        "mark_price": 99_000.0,
        "unrealized_pnl": 10.0,
        "notional": 990.0,
        "notional_currency": "USDT",
    }
    position.update(position_changes)
    return {
        "equity": 2_100.0,
        "wallet_balance": 2_000.0,
        "available_balance": 1_800.0,
        "unrealized_pnl": 100.0,
        "venue_profile": "demo",
        "equity_currency": None,
        "wallet_balance_currency": None,
        "available_balance_currency": None,
        "unrealized_pnl_currency": None,
        "account_scope": "derivatives",
        "status": "ok",
        "error": None,
        "positions_read": {
            "status": "partial", "skipped": 0,
            "issues": [{"symbol": None, "reason": "scope_unverified"}],
            "observed_at": NOW.isoformat(),
        },
        "positions": [position],
    }


def test_fee_uses_the_proven_notional_and_its_currency():
    futures = _futures_payload()

    attach_position_costs(futures, {"risk_derivatives_round_trip_fee_rate": 0.002})

    (position,) = futures["positions"]
    assert position["round_trip_fee"] == pytest.approx(990.0 * 0.002)
    assert position["round_trip_fee_currency"] == "USDT"


def test_unproven_notional_or_currency_leave_the_fee_unknown():
    for changes in ({"notional": None}, {"notional_currency": None},
                    {"notional_currency": "  "}, {"notional": "NaN"}):
        futures = _futures_payload(**changes)

        attach_position_costs(futures, {"risk_derivatives_round_trip_fee_rate": 0.002})

        (position,) = futures["positions"]
        assert position["round_trip_fee"] is None
        assert position["round_trip_fee_currency"] is None


def test_malformed_rate_leaves_the_fee_unknown():
    futures = _futures_payload()

    attach_position_costs(futures, {"risk_derivatives_round_trip_fee_rate": "junk"})

    (position,) = futures["positions"]
    assert position["round_trip_fee"] is None
    assert position["round_trip_fee_currency"] is None


def test_display_never_pairs_venue_positions_with_stored_state_or_funding():
    """The module has no state or funding access at all: nulls are structural."""
    futures = _futures_payload()

    attach_position_costs(futures, {"risk_derivatives_round_trip_fee_rate": 0.001})

    (position,) = futures["positions"]
    assert position["opened_at"] is None
    assert position["held_hours"] is None
    assert position["funding"] is None
    assert position["funding_currency"] is None
    assert not hasattr(position_cost_module, "job_store")
    assert not hasattr(position_cost_module, "_funding_income_rows")


def test_portfolio_wiring_attaches_fee_only(monkeypatch, tmp_path):
    set_config({
        "exec_live": True,
        "exec_log_path": str(tmp_path / "absent.jsonl"),
        "risk_derivatives_round_trip_fee_rate": 0.002,
    })
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: PortfolioContext(cash=0.0, currency="USDT", positions=[]),
        client=lambda: SimpleNamespace(fetch_funding_history=(
            lambda *args, **kwargs: pytest.fail("funding must not be read for display")
        )),
    )
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda config: bridge)
    monkeypatch.setattr(portfolio_mod, "read_futures_snapshot", lambda _bridge, **_: _futures_payload())

    body = portfolio_mod.get_portfolio()

    (position,) = body["futures"]["positions"]
    assert body["futures"]["status"] == "ok"
    assert position["round_trip_fee"] == pytest.approx(990.0 * 0.002)
    assert position["round_trip_fee_currency"] == "USDT"
    assert position["opened_at"] is None
    assert position["held_hours"] is None
    assert position["funding"] is None
    assert body["warnings"] == []
