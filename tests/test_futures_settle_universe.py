"""Futures-mode universe and multi-settle (USDT/USDC) contract pinning."""

from copy import deepcopy

import pytest

from tradingagents.dataflows.screening import futures_universe
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import ExchangeBridge
from tradingagents.execution.futures_account import (
    _binance_settle_values,
    read_futures_settle_balances,
)
from tradingagents.execution.futures_adoption import _raw_symbol


def _swap(base: str, settle: str, **extra) -> dict:
    return {
        "type": "swap", "contract": True, "active": True, "linear": True,
        "base": base, "quote": settle, "settle": settle, **extra,
    }


class _Venue:
    def __init__(self, markets: dict, tickers: dict | None = None, balance: dict | None = None):
        self._markets = markets
        self._tickers = tickers or {}
        self._balance = balance or {}

    def load_markets(self):
        return self._markets

    def fetch_tickers(self):
        return self._tickers

    def fetch_balance(self, params=None):
        return self._balance


MARKETS = {
    "BTC/USDT:USDT": _swap("BTC", "USDT"),
    "BTC/USDC:USDC": _swap("BTC", "USDC"),
    "SOL/USDC:USDC": _swap("SOL", "USDC"),
    "ENA/USDT:USDT": _swap("ENA", "USDT"),
    "1000PEPE/USDT:USDT": _swap("1000PEPE", "USDT"),
    "USDC/USDT:USDT": _swap("USDC", "USDT"),
    "DOGE/USD1:USD1": _swap("DOGE", "USD1"),
    "OLD/USDT:USDT": _swap("OLD", "USDT", active=False),
    "BTC/USD:BTC": _swap("BTC", "BTC", linear=False, quote="USD"),
    "ETH/USDT": {"type": "spot", "base": "ETH", "quote": "USDT"},
}

RANKING = {
    "BTC/USDT:USDT": {"quoteVolume": 9e9},
    "BTC/USDC:USDC": {"quoteVolume": 1e9},
    "SOL/USDC:USDC": {"quoteVolume": 2e8},
    "ENA/USDT:USDT": {"quoteVolume": 3e8},
    "1000PEPE/USDT:USDT": {"quoteVolume": 5e8},
    "USDC/USDT:USDT": {"quoteVolume": 4e9},
    "DOGE/USD1:USD1": {"quoteVolume": 6e8},
    "OLD/USDT:USDT": {"quoteVolume": 7e8},
}


def test_universe_keeps_only_held_settles_and_ranks_by_ranking_volume():
    venue = _Venue(MARKETS, tickers={})  # demo volumes ignored when a ranking client is given
    rows = futures_universe(venue, {"USDT", "USDC"}, 10, ranking_client=_Venue({}, RANKING))
    assert [(r["ticker"], r["futures_symbol"]) for r in rows] == [
        ("BTC-USD", "BTC/USDT:USDT"),  # the more liquid of BTC/USDT and BTC/USDC
        ("ENA-USD", "ENA/USDT:USDT"),
        ("SOL-USD", "SOL/USDC:USDC"),
    ]
    assert {r["settle"] for r in rows} == {"USDT", "USDC"}


def test_universe_without_usdc_balance_drops_usdc_only_contracts():
    rows = futures_universe(_Venue(MARKETS), {"USDT"}, 10, ranking_client=_Venue({}, RANKING))
    assert [r["ticker"] for r in rows] == ["BTC-USD", "ENA-USD"]


def test_universe_with_no_settle_assets_is_empty():
    assert futures_universe(_Venue(MARKETS), set(), 10, ranking_client=_Venue({}, RANKING)) == []


def _bridge(**config) -> ExchangeBridge:
    merged = deepcopy(DEFAULT_CONFIG)
    merged.update(config)
    return ExchangeBridge(config=merged, exchange_factory=lambda: _Venue(MARKETS))


def test_pinned_usdc_contract_resolves_against_its_own_settle():
    bridge = _bridge(exec_futures_settle_currencies=["USDT", "USDC"])
    bridge.pin_futures_contract("SOL-USD", "SOL/USDC:USDC")
    assert bridge.futures_symbol_for("SOL-USD", markets=MARKETS) == "SOL/USDC:USDC"
    # unpinned tickers keep the exec_quote_currency default
    assert bridge.futures_symbol_for("ENA-USD", markets=MARKETS) == "ENA/USDT:USDT"


def test_pin_outside_allowed_settles_is_refused():
    bridge = _bridge(exec_futures_settle_currencies=["USDT"])
    with pytest.raises(ValueError, match="outside exec_futures_settle_currencies"):
        bridge.pin_futures_contract("SOL-USD", "SOL/USDC:USDC")


def test_settle_balances_and_per_asset_margin_book():
    balance = {
        "total": {"USDT": 5000.0, "USDC": 1200.0, "BTC": 0.0},
        "info": {"assets": [
            {"asset": "USDT", "marginBalance": "5000", "walletBalance": "5000",
             "availableBalance": "5000", "unrealizedProfit": "0"},
            {"asset": "USDC", "marginBalance": "1190", "walletBalance": "1200",
             "availableBalance": "900", "unrealizedProfit": "-10"},
        ]},
    }
    bridge = ExchangeBridge(
        config=deepcopy(DEFAULT_CONFIG), exchange_factory=lambda: _Venue({}, balance=balance)
    )
    assert read_futures_settle_balances(bridge) == {"USDT": 5000.0, "USDC": 1200.0}
    values = _binance_settle_values(balance, "USDC")
    assert values["equity"] == 1190.0
    assert values["available_balance"] == 900.0
    assert values["equity_currency"] == "USDC"
    assert _binance_settle_values(balance, "FDUSD") is None


def test_raw_symbol_keeps_the_contract_quote():
    assert _raw_symbol("ETH/USDC:USDC") == "ETHUSDC"
    assert _raw_symbol("ZRO/USDT:USDT") == "ZROUSDT"


def test_universe_skips_tradfi_and_premarket_contracts():
    markets = {
        "BTC/USDT:USDT": _swap("BTC", "USDT", info={"underlyingType": "COIN"}),
        "SPY/USDT:USDT": _swap("SPY", "USDT", info={"underlyingType": "EQUITY"}),
        "XAU/USDT:USDT": _swap("XAU", "USDT", info={"underlyingType": "COMMODITY"}),
        "NEW/USDT:USDT": _swap("NEW", "USDT", info={"underlyingType": "PREMARKET"}),
    }
    ranking = {symbol: {"quoteVolume": 1e9} for symbol in markets}
    rows = futures_universe(_Venue(markets), {"USDT"}, 10, ranking_client=_Venue({}, ranking))
    assert [r["ticker"] for r in rows] == ["BTC-USD"]


def test_price_mismatch_flags_a_different_asset_behind_the_ticker():
    from tradingagents.dataflows.screening import MAX_PRICE_MISMATCH, price_mismatch

    assert price_mismatch(100.0, 101.0) < MAX_PRICE_MISMATCH
    assert price_mismatch(0.02, 1.5) > MAX_PRICE_MISMATCH
    assert price_mismatch(None, 1.0) is None


def test_momentum_with_zero_start_price_is_undefined():
    import pandas as pd

    from tradingagents.dataflows.screening import _momentum

    assert _momentum(pd.Series([0.0, 1.0, 2.0]), 2) is None
    assert _momentum(pd.Series([1.0, 1.5, 2.0]), 2) == 100.0
