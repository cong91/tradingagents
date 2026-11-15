from datetime import UTC, datetime, timedelta

import pytest

from server import job_store
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.execution.futures_safety import (
    LIQUIDATION_BUFFER,
    FuturesPlanError,
    budget_limited_quantity,
    validate_bracket,
    validate_liquidation_buffer,
)
from tradingagents.execution.futures_scheduler import closed_candle_pair


def test_closed_candles_exclude_forming_and_future_bars():
    class Exchange:
        def __init__(self):
            self.kwargs = None

        def fetch_ohlcv(self, symbol, *, timeframe, since, limit):
            self.kwargs = (symbol, timeframe, since, limit)
            return [[3_600_000, 1, 2, 0.5, 1.5, 10], [7_200_000, 1, 2, 0.5, 1.5, 10]]

    exchange = Exchange()
    candles = fetch_closed_candles(
        exchange, "BTC/USDT", "1h",
        as_of=datetime.fromtimestamp(7_200_000 / 1000, tz=UTC),
    )
    assert [c.open_time for c in candles] == [3_600_000]
    assert exchange.kwargs == ("BTC/USDT", "1h", 0, 100)
    assert all(c.is_closed for c in candles)


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
def test_scheduler_uses_previous_closed_four_hour_candle_at_utc_edges(hour):
    now = datetime(2026, 10, 7, hour, 0, 5, tzinfo=UTC)
    one_hour, four_hour = closed_candle_pair(now)
    assert one_hour == (now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1), now.replace(minute=0, second=0, microsecond=0))
    assert four_hour[1].hour == hour
    assert four_hour[1] <= now


def test_bracket_geometry_is_fail_closed_for_both_directions():
    validate_bracket(side="buy", entry=100, stop_loss=95, take_profit=110)
    validate_bracket(side="sell", entry=100, stop_loss=105, take_profit=90)
    for kwargs in (
        {"side": "buy", "entry": 100, "stop_loss": None, "take_profit": 110},
        {"side": "buy", "entry": 100, "stop_loss": 105, "take_profit": 110},
        {"side": "sell", "entry": 100, "stop_loss": 95, "take_profit": 90},
    ):
        with pytest.raises(FuturesPlanError):
            validate_bracket(**kwargs)


def test_budget_sizing_scales_with_the_total_budget():
    """budget_fraction is the agent-owned share of the TOTAL budget:
    notional = equity x fraction, quantity = notional / entry."""
    quantity = budget_limited_quantity(
        equity=1_000, entry=100, stop_loss=95, leverage=3, budget_fraction=0.10,
    )
    assert quantity == pytest.approx(1.0)  # 10% of 1_000 = 100 notional @ 100
    with pytest.raises(FuturesPlanError):
        budget_limited_quantity(
            equity=None, entry=100, stop_loss=95, leverage=3, budget_fraction=0.10,
        )


def test_short_budget_sizing_and_amount_step():
    qty = budget_limited_quantity(
        side="sell", equity=1_000, entry=100, stop_loss=105, take_profit=90,
        leverage=3, budget_fraction=0.10, amount_step=0.01,
    )
    assert qty == pytest.approx(1.0)  # 10% of 1_000 = 100 notional @ 100
    with pytest.raises(FuturesPlanError):
        budget_limited_quantity(
            side="sell", equity=1_000, entry=100, stop_loss=95, take_profit=90,
            leverage=3, budget_fraction=0.10,
        )


def test_budget_fraction_bounds_are_structural():
    with pytest.raises(FuturesPlanError, match="outside \\(0, 1\\]"):
        budget_limited_quantity(
            equity=1_000, entry=100, stop_loss=95, leverage=3, budget_fraction=0.0,
        )
    with pytest.raises(FuturesPlanError, match="outside \\(0, 1\\]"):
        budget_limited_quantity(
            equity=1_000, entry=100, stop_loss=95, leverage=3, budget_fraction=1.5,
        )


def test_budget_sizing_allows_agent_budget_above_one_percent_stop_risk():
    quantity = budget_limited_quantity(
        equity=1_000, entry=100, stop_loss=98, leverage=2, budget_fraction=1.0,
    )
    assert quantity == pytest.approx(10.0)


def test_liquidation_buffer_refuses_a_stop_beyond_the_leverage_distance():
    """The leverage-based planning envelope rejects an excessively distant stop."""
    assert LIQUIDATION_BUFFER == 0.8
    entry, stop, leverage = 100.0, 98.0, 10.0
    validate_liquidation_buffer(entry=entry, stop_loss=stop, leverage=leverage)
    with pytest.raises(FuturesPlanError, match="liquidation buffer"):
        validate_liquidation_buffer(entry=entry, stop_loss=91.0, leverage=leverage)
    with pytest.raises(FuturesPlanError, match="liquidation buffer"):
        budget_limited_quantity(
            equity=1_000, entry=100, stop_loss=91.0, leverage=10, budget_fraction=0.10,
        )
    validate_liquidation_buffer(entry=100.0, stop_loss=102.0, leverage=10.0)
    with pytest.raises(FuturesPlanError, match="liquidation buffer"):
        validate_liquidation_buffer(entry=100.0, stop_loss=109.0, leverage=10.0)


def test_signal_claim_is_atomic_terminal_and_expired_lease_recoverable(postgres_schema):
    now = "2026-10-07T00:00:00+00:00"
    assert job_store.claim_futures_signal("signal-1", now=now, lease_until="2026-10-07T01:00:00+00:00")
    assert not job_store.claim_futures_signal("signal-1", now=now, lease_until="2026-10-07T02:00:00+00:00")
    assert job_store.claim_futures_signal("signal-1", now="2026-10-07T02:00:00+00:00", lease_until="2026-10-07T03:00:00+00:00")
    assert job_store.release_futures_signal("signal-1", now="2026-10-07T02:30:00+00:00")
    assert not job_store.claim_futures_signal("signal-1", now="2026-10-07T04:00:00+00:00", lease_until="2026-10-07T05:00:00+00:00")


def test_scheduler_boundaries_never_select_forming_four_hour_candle():
    from datetime import datetime

    now = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    one_hour, four_hour = closed_candle_pair(now)
    assert one_hour == (datetime(2026, 10, 7, 7, tzinfo=UTC), datetime(2026, 10, 7, 8, tzinfo=UTC))
    assert four_hour == (datetime(2026, 10, 7, 4, tzinfo=UTC), datetime(2026, 10, 7, 8, tzinfo=UTC))
