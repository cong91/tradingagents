from __future__ import annotations

from datetime import UTC, datetime

import ccxt
import pytest

from server import kronos_context
from tradingagents.dataflows.market_data import (
    ClosedCandlePaginationError,
    fetch_closed_candles,
)
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

_DURATION = 3_600_000
_CUTOFF = 8 * _DURATION
_AS_OF = datetime.fromtimestamp(_CUTOFF / 1000, UTC)


def _row(index: int, *, close: float | None = None) -> list[float | int]:
    price = float(index if close is None else close)
    return [index * _DURATION, price, price + 1, price - 1, price, 10.0]


def _pages_exchange(pages: list[list[list[float | int]]], *, cap: int = 3):
    class Exchange:
        options = {"fetchOHLCV": {"limit": cap}}

        def __init__(self):
            self.calls: list[dict[str, int | str]] = []
            self._pages = iter(pages)

        def fetch_ohlcv(self, symbol, *, timeframe, since, limit):
            self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
            return next(self._pages, [])

    return Exchange()


def _binance_futures_exchange(monkeypatch, pages):
    exchange = ccxt.binanceusdm()
    exchange.set_markets([{
        "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC",
        "quote": "USDT", "settle": "USDT", "settleId": "USDT",
        "type": "swap", "spot": False, "swap": True, "future": False,
        "contract": True, "linear": True, "inverse": False,
        "contractSize": 1,
    }])
    calls = []
    page_iter = iter(pages)

    def fetch(params):
        calls.append(dict(params))
        return next(page_iter, [])

    monkeypatch.setattr(exchange, "fapiPublicGetKlines", fetch)
    return exchange, calls


def test_pagination_dedupes_boundary_overlap_and_filters_forming_bar():
    exchange = _pages_exchange([
        [_row(2), _row(3), _row(4)],
        [_row(4), _row(5), _row(6)],
        [_row(6), _row(7), _row(8)],
    ])

    candles = fetch_closed_candles(
        exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=6,
    )

    assert [candle.open_time for candle in candles] == [
        index * _DURATION for index in range(2, 8)
    ]
    assert all(candle.close_time <= _CUTOFF for candle in candles)
    assert [call["limit"] for call in exchange.calls] == [3, 3, 3]
    assert exchange.calls[1]["since"] > exchange.calls[0]["since"]
    assert exchange.calls[2]["since"] > exchange.calls[1]["since"]


def test_non_boundary_as_of_aligns_single_page_window():
    exchange = _pages_exchange([[_row(6), _row(7), _row(8)]], cap=3)
    as_of = datetime.fromtimestamp((_CUTOFF + 5 * 60_000) / 1000, UTC)

    candles = fetch_closed_candles(
        exchange, "BTC/USDT:USDT", "1h", as_of=as_of, limit=2,
    )

    assert [candle.open_time for candle in candles] == [6 * _DURATION, 7 * _DURATION]
    assert exchange.calls[0]["since"] == 6 * _DURATION


def test_binanceusdm_feature_limit_paginates_2048_closed_four_hour_candles(monkeypatch):
    duration = 14_400_000
    requested = 2048
    closed_cutoff = 10_000 * duration
    start_index = closed_cutoff // duration - requested
    rows = [
        [index * duration, str(index), str(index + 1), str(index - 1), str(index), "10"]
        for index in range(start_index, closed_cutoff // duration)
    ]
    pages = [rows[index:index + 500] for index in range(0, len(rows), 500)]
    exchange, calls = _binance_futures_exchange(monkeypatch, pages)
    as_of = datetime.fromtimestamp((closed_cutoff + 5 * 60_000) / 1000, UTC)

    candles = fetch_closed_candles(
        exchange, "BTC/USDT:USDT", "4h", as_of=as_of, limit=requested,
    )

    assert len(candles) == requested
    assert candles[0].open_time == closed_cutoff - requested * duration
    assert candles[-1].open_time == closed_cutoff - duration
    assert all(
        current.open_time == previous.open_time + duration
        for previous, current in zip(candles, candles[1:], strict=False)
    )
    assert len(calls) == 5
    assert [call["limit"] for call in calls] == [500] * 5
    assert calls[0]["startTime"] == closed_cutoff - requested * duration
    assert all(
        current["startTime"] > previous["startTime"]
        for previous, current in zip(calls, calls[1:], strict=False)
    )


def test_limit_at_or_below_venue_cap_keeps_one_call_behavior():
    exchange = _pages_exchange([[_row(6), _row(7)]], cap=3)

    candles = fetch_closed_candles(
        exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=2,
    )

    assert len(candles) == 2
    assert len(exchange.calls) == 1
    assert exchange.calls[0]["limit"] == 2
    assert exchange.calls[0]["since"] == _CUTOFF - 2 * _DURATION


def test_small_limit_preserves_type_error_adapter_fallback():
    class LegacyExchange:
        options = {"fetchOHLCV": {"limit": 3}}

        def __init__(self):
            self.calls = 0

        def fetch_ohlcv(self, symbol, *, timeframe, limit):
            self.calls += 1
            return [_row(6), _row(7)]

    exchange = LegacyExchange()
    candles = fetch_closed_candles(
        exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=2,
    )

    assert len(candles) == 2
    assert exchange.calls == 1


def test_duplicate_open_time_within_page_is_rejected():
    exchange = _pages_exchange([[_row(0), _row(1), _row(1)]])

    with pytest.raises(ClosedCandlePaginationError, match="duplicate.*within page"):
        fetch_closed_candles(exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=4)


def test_conflicting_boundary_duplicate_is_rejected():
    exchange = _pages_exchange([
        [_row(2), _row(3), _row(4)],
        [_row(4, close=99), _row(5), _row(6)],
    ])

    with pytest.raises(ClosedCandlePaginationError, match="conflicting duplicate"):
        fetch_closed_candles(exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=5)


def test_repeated_page_is_rejected_as_no_progress():
    page = [_row(3), _row(4), _row(5)]
    exchange = _pages_exchange([page, [_row(5)]])

    with pytest.raises(ClosedCandlePaginationError, match="no progress"):
        fetch_closed_candles(exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=5)


def test_gap_in_requested_history_is_rejected():
    exchange = _pages_exchange([
        [_row(2), _row(3), _row(4)],
        [_row(4), _row(6), _row(7)],
    ])

    with pytest.raises(ClosedCandlePaginationError, match="candle gap"):
        fetch_closed_candles(exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=5)


def test_insufficient_closed_history_is_rejected():
    exchange = _pages_exchange([[_row(4), _row(5)]])

    with pytest.raises(ClosedCandlePaginationError, match="insufficient closed candle history"):
        fetch_closed_candles(exchange, "BTC/USDT:USDT", "1h", as_of=_AS_OF, limit=4)


def test_kronos_context_history_uses_paginated_closed_candles():
    exchange = _pages_exchange([
        [_row(4), _row(5)],
        [_row(6), _row(7)],
    ], cap=2)
    settings = ForecastSettings(profile="small_base", timeframe="1h", lookback_bars=4)

    candles = kronos_context.history_for(
        exchange, "BTC/USDT:USDT", settings, as_of=_AS_OF,
    )

    assert len(candles) == 4
    assert [candle.open_time for candle in candles] == [
        index * _DURATION for index in range(4, 8)
    ]
    assert len(exchange.calls) == 2
