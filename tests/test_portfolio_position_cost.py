"""Holding-cost arithmetic behind the portfolio's "Giữ / phí" column.

``position_cost`` is pure: notional, fee rate, the venue's FUNDING_FEE income
rows, the stored entry timestamp and "now". No database, no venue, no clock.
The last three tests check the wiring: a funding read that fails nulls only that
figure and adds a warning, never the snapshot.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server import portfolio as portfolio_mod, position_cost as position_cost_module
from server.position_cost import position_cost
from tradingagents.dataflows.config import set_config
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _income(amount: float, when: datetime) -> dict:
    return {"type": "FUNDING_FEE", "amount": amount,
            "timestamp": int(when.timestamp() * 1000)}


def test_cost_of_a_held_position_sums_funding_and_charges_the_round_trip_fee():
    opened = NOW - timedelta(hours=12.5)
    rows = [_income(-0.20, opened + timedelta(hours=8)), _income(-0.07, opened + timedelta(hours=10))]

    cost = position_cost(990.0, 0.001, rows, opened.isoformat(), NOW)

    assert cost["opened_at"] == opened.isoformat()
    assert cost["held_hours"] == pytest.approx(12.5)
    assert cost["funding_usdt"] == pytest.approx(-0.27)
    # The rate is the round-trip total (matching profit_protection's
    # break-even stop); it must not be doubled again here.
    assert cost["round_trip_fee_usdt"] == pytest.approx(990.0 * 0.001)


def test_notional_null_leaves_the_fee_unknown_but_keeps_the_rest():
    opened = NOW - timedelta(hours=3)
    rows = [_income(0.54, opened + timedelta(hours=1))]

    cost = position_cost(None, 0.001, rows, opened.isoformat(), NOW)

    assert cost["round_trip_fee_usdt"] is None
    assert cost["held_hours"] == pytest.approx(3.0)
    assert cost["funding_usdt"] == pytest.approx(0.54)


def test_empty_income_rows_are_zero_funding_not_unknown():
    opened = NOW - timedelta(hours=1)

    cost = position_cost(100.0, 0.001, [], opened.isoformat(), NOW)

    assert cost["funding_usdt"] == 0.0
    assert cost["held_hours"] == pytest.approx(1.0)


def test_failed_funding_read_is_null_not_zero():
    cost = position_cost(100.0, 0.001, None, None, NOW)

    assert cost["funding_usdt"] is None
    assert cost["round_trip_fee_usdt"] == pytest.approx(0.1)


@pytest.mark.parametrize("opened_at", ["not-a-date", "2026-13-01T00:00:00+00:00", "", None])
def test_malformed_opened_at_becomes_null(opened_at):
    cost = position_cost(500.0, 0.001, [_income(-0.01, NOW)], opened_at, NOW)

    assert cost["opened_at"] is None
    assert cost["held_hours"] is None
    assert cost["funding_usdt"] == pytest.approx(-0.01)


def test_zero_rate_makes_the_fee_free_instead_of_unknown():
    cost = position_cost(990.0, 0.0, [], None, NOW)

    assert cost["round_trip_fee_usdt"] == 0.0


def test_a_future_entry_time_renders_as_zero_hours():
    opened = NOW + timedelta(minutes=5)

    cost = position_cost(100.0, 0.001, [], opened.isoformat(), NOW)

    assert cost["held_hours"] == 0.0


def test_funding_rows_are_summed_as_returned():
    opened = NOW - timedelta(hours=2)
    rows = [
        _income(-0.01, opened + timedelta(hours=1)),
        _income(0.04, opened + timedelta(hours=2)),  # a short can receive funding
    ]

    cost = position_cost(990.0, 0.001, rows, opened.isoformat(), NOW)

    assert cost["funding_usdt"] == pytest.approx(0.03)


def test_short_notional_uses_absolute_size():
    cost = position_cost(-990.0, 0.001, [], None, NOW)

    assert cost["round_trip_fee_usdt"] == pytest.approx(0.99)


def test_malformed_income_rows_are_skipped_not_guessed():
    opened = NOW - timedelta(hours=1)
    rows = ["junk", {"amount": "abc"}, {"amount": None}, _income(-0.05, NOW)]

    cost = position_cost(100.0, 0.001, rows, opened.isoformat(), NOW)

    assert cost["funding_usdt"] == pytest.approx(-0.05)


def _futures_payload() -> dict:
    return {
        "status": "ok",
        "positions": [{
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "notional": 990.0,
            "notional_currency": "USDT",
        }],
    }


def _install_bridge(monkeypatch, client) -> None:
    bridge = SimpleNamespace(
        effective_mode=lambda: "live",
        sync_portfolio=lambda: PortfolioContext(cash=0.0, currency="USDT", positions=[]),
        client=lambda: client,
    )
    monkeypatch.setattr(portfolio_mod, "_build_bridge", lambda config: bridge)
    monkeypatch.setattr(
        portfolio_mod, "read_futures_account",
        lambda bridge: SimpleNamespace(as_dict=_futures_payload),
    )
    opened = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    state = {"position_key": "BTC/USDT:USDT:sell:42",
             "snapshot": {"side": "short", "opened_at": opened}}
    monkeypatch.setattr(
        position_cost_module.job_store, "active_position_states", lambda: [state]
    )
    monkeypatch.setattr(
        position_cost_module.job_store, "load_position_state", lambda key: state
    )


def test_portfolio_fills_cost_fields_from_state_and_income(monkeypatch, tmp_path):
    set_config({
        "exec_live": True,
        "exec_log_path": str(tmp_path / "absent.jsonl"),
        "risk_derivatives_round_trip_fee_rate": 0.002,
    })

    class _Client:
        def fetch_funding_history(self, symbol, since, limit, params):
            assert symbol == "BTC/USDT:USDT"
            assert params == {}
            return [{"timestamp": int(datetime.now(UTC).timestamp() * 1000),
                     "amount": 0.54}]

    _install_bridge(monkeypatch, _Client())

    body = portfolio_mod.get_portfolio()

    (position,) = body["futures"]["positions"]
    assert position["opened_at"].endswith("+00:00")
    assert position["held_hours"] == pytest.approx(6.0, abs=0.05)
    assert position["funding_usdt"] == pytest.approx(0.54)
    assert position["round_trip_fee_usdt"] == pytest.approx(990.0 * 0.002)
    assert body["futures"]["status"] == "ok"


def test_portfolio_funding_read_failure_nulls_only_funding(monkeypatch, tmp_path):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})

    class _Client:
        def fetch_funding_history(self, symbol, since, limit, params):
            raise RuntimeError("private endpoint said no")

    _install_bridge(monkeypatch, _Client())

    body = portfolio_mod.get_portfolio()

    (position,) = body["futures"]["positions"]
    assert body["futures"]["status"] == "ok"
    assert position["funding_usdt"] is None
    assert position["held_hours"] == pytest.approx(6.0, abs=0.05)
    assert position["round_trip_fee_usdt"] is not None
    assert any("funding history unavailable for BTC/USDT:USDT" in w for w in body["warnings"])


def test_portfolio_without_a_recorded_state_leaves_cost_unknown(monkeypatch, tmp_path):
    set_config({"exec_live": True, "exec_log_path": str(tmp_path / "absent.jsonl")})
    _install_bridge(monkeypatch, object())
    monkeypatch.setattr(
        position_cost_module.job_store, "active_position_states", lambda: []
    )

    body = portfolio_mod.get_portfolio()

    (position,) = body["futures"]["positions"]
    assert position["opened_at"] is None
    assert position["held_hours"] is None
    assert position["funding_usdt"] is None
    # The fee needs only notional × rate, so it survives an unknown entry time.
    assert position["round_trip_fee_usdt"] == pytest.approx(990.0 * 0.001)
    assert body["futures"]["status"] == "ok"
