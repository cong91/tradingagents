from datetime import datetime, timedelta, timezone

import pytest

from server import job_store
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.execution.futures_safety import (
    FuturesPlanError,
    risk_limited_quantity,
    validate_bracket,
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
        as_of=datetime.fromtimestamp(7_200_000 / 1000, tz=timezone.utc),
    )
    assert [c.open_time for c in candles] == [3_600_000]
    assert exchange.kwargs == ("BTC/USDT", "1h", 0, 100)
    assert all(c.is_closed for c in candles)


@pytest.mark.parametrize("hour", [0, 4, 8, 12, 16, 20])
def test_scheduler_uses_previous_closed_four_hour_candle_at_utc_edges(hour):
    now = datetime(2026, 10, 7, hour, 0, 5, tzinfo=timezone.utc)
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


def test_risk_sizing_uses_stop_distance_and_margin_cap():
    quantity = risk_limited_quantity(
        equity=1_000, entry=100, stop_loss=95, leverage=3,
        risk_fraction=0.01, margin_fraction=0.25,
    )
    assert quantity == pytest.approx(2.0)
    with pytest.raises(FuturesPlanError):
        risk_limited_quantity(
            equity=None, entry=100, stop_loss=95, leverage=3,
            risk_fraction=0.01, margin_fraction=0.25,
        )


def test_short_risk_sizing_and_precision_caps():
    qty = risk_limited_quantity(
        side="sell", equity=1_000, entry=100, stop_loss=105, take_profit=90,
        leverage=3, risk_fraction=0.01, margin_fraction=0.25, amount_step=0.01,
    )
    assert qty == pytest.approx(2.0)
    with pytest.raises(FuturesPlanError):
        risk_limited_quantity(
            side="sell", equity=1_000, entry=100, stop_loss=95, take_profit=90,
            leverage=3, risk_fraction=0.01, margin_fraction=0.25,
        )


def test_signal_claim_is_atomic_terminal_and_expired_lease_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr("server.paths.db_path", lambda: tmp_path / "control.db")
    now = "2026-10-07T00:00:00+00:00"
    assert job_store.claim_futures_signal("signal-1", now=now, lease_until="2026-10-07T01:00:00+00:00")
    assert not job_store.claim_futures_signal("signal-1", now=now, lease_until="2026-10-07T02:00:00+00:00")
    assert job_store.claim_futures_signal("signal-1", now="2026-10-07T02:00:00+00:00", lease_until="2026-10-07T03:00:00+00:00")
    assert job_store.release_futures_signal("signal-1", now="2026-10-07T02:30:00+00:00")
    assert not job_store.claim_futures_signal("signal-1", now="2026-10-07T04:00:00+00:00", lease_until="2026-10-07T05:00:00+00:00")


def test_scheduler_boundaries_never_select_forming_four_hour_candle():
    from datetime import datetime

    now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    one_hour, four_hour = closed_candle_pair(now)
    assert one_hour == (datetime(2026, 10, 7, 7, tzinfo=timezone.utc), datetime(2026, 10, 7, 8, tzinfo=timezone.utc))
    assert four_hour == (datetime(2026, 10, 7, 4, tzinfo=timezone.utc), datetime(2026, 10, 7, 8, tzinfo=timezone.utc))
