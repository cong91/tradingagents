from datetime import UTC, datetime, timedelta, timezone

import pytest

from tradingagents.auto_timeframes import last_closed_candle, timeframe_ms, validate_auto_timeframe
from tradingagents.dataflows.market_data import ClosedCandlePaginationError, fetch_closed_candles

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "4h"])
def test_closed_grid_and_cutoff(timeframe):
    step = timeframe_ms(timeframe)
    boundary = 1_800_000_000_000 // step * step
    as_of = datetime.fromtimestamp((boundary + step // 2) / 1000, UTC)

    class Venue:
        def fetch_ohlcv(self, _symbol, **kwargs):
            assert kwargs["timeframe"] == timeframe
            assert kwargs["since"] == boundary - step
            return [[boundary - step, 100, 101, 99, 100, 1],
                    [boundary, 100, 101, 99, 100, 1]]

    candles = fetch_closed_candles(Venue(), "BTC/USDT", timeframe, as_of=as_of, limit=1)
    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open_time, candle.close_time, candle.is_closed) == (boundary - step, boundary, True)
    assert last_closed_candle(as_of, timeframe) == (
        datetime.fromtimestamp((boundary - step) / 1000, UTC),
        datetime.fromtimestamp(boundary / 1000, UTC),
    )
    assert last_closed_candle(as_of.astimezone(timezone(timedelta(hours=7))), timeframe) == last_closed_candle(as_of, timeframe)


@pytest.mark.parametrize("bad", [True, 123.0, "123", -1, 123])
def test_invalid_candle_timestamp_refused(bad):
    class Venue:
        def fetch_ohlcv(self, *_args, **_kwargs):
            return [[bad, 100, 101, 99, 100, 1]]

    with pytest.raises(ClosedCandlePaginationError):
        fetch_closed_candles(Venue(), "BTC/USDT", "15m", as_of=datetime(2026, 1, 1, tzinfo=UTC), limit=1)


def test_unknown_timeframe_is_refused():
    with pytest.raises(ValueError, match="unsupported"):
        validate_auto_timeframe("5m")
