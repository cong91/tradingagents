"""Tests for the auto-scan screening module (tradingagents/dataflows/screening.py).

Unit-scoped: no network, no ccxt, no yfinance — the universe builders are
faked at the module seam and the screen runs on synthetic OHLCV frames. The
research-backed criteria (momentum, RSI band, 52-week proximity, breakout,
RVOL, ATR% band, dollar-volume floor) are asserted through the candidate's
flags/reasons so the score weights stay tunable without rewriting tests.
"""

import pandas as pd
import pytest

from tradingagents.dataflows import screening
from tradingagents.dataflows.errors import NoMarketDataError, VendorError

pytestmark = pytest.mark.unit

# Volume large enough that flat-price fixtures clear the stock dollar-volume
# floor (close 100 * volume 200k = 20M >= 10M).
_FLAT_VOLUME = 200_000.0


def _ohlcv_frame(
    closes: list[float],
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=len(closes), freq="D")
    closes_s = pd.Series(closes)
    data = {
        "Date": dates,
        "Open": closes_s.shift(1).fillna(closes_s.iloc[0]),
        "High": closes_s * 1.01,
        "Low": closes_s * 0.99,
        "Close": closes_s,
        "Volume": pd.Series(volumes if volumes is not None else [_FLAT_VOLUME] * len(closes)),
    }
    return pd.DataFrame(data)


def _uptrend(days: int = 60, step: float = 1.0) -> list[float]:
    """Steady rise with a final jump past the prior day's high — the breakout
    context the screen is meant to catch (a flat step never clears High =
    close*1.01 of the previous day)."""
    return [100.0 + i * step for i in range(days - 1)] + [100.0 + days * step]


def test_screen_one_uptrend_with_volume_spike_flags_all_signals(monkeypatch):
    """60 rising closes on a final volume spike trip momentum, trend stack,
    RVOL, breakout, 52-week proximity and the ATR band at once."""
    closes = _uptrend()
    volumes = [_FLAT_VOLUME] * 59 + [_FLAT_VOLUME * 3]
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes, volumes)
    )
    candidate = screening.screen_one(
        "BTC-USD", "2026-10-01", asset_type="crypto", benchmark_momentum=0.0
    )
    assert candidate is not None
    assert candidate.asset_type == "crypto"
    assert candidate.breakout is True
    assert candidate.momentum_pct > 5
    joined = " | ".join(candidate.reasons)
    assert "SMA20" in joined
    assert "SMA50" in joined
    assert "RVOL" in joined
    assert "52 tuần" in joined
    assert "phá đỉnh 20 phiên" in joined
    assert "mạnh hơn benchmark" in joined
    assert candidate.score > 3.0


def test_screen_one_flat_series_has_no_signal_reasons(monkeypatch):
    closes = [100.0] * 60
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes)
    )
    candidate = screening.screen_one(
        "NVDA", "2026-10-01", asset_type="stock", benchmark_momentum=0.0
    )
    assert candidate is not None
    joined = " | ".join(candidate.reasons)
    assert "trong 20 phiên" not in joined  # momentum neutral
    assert "SMA20" not in joined
    assert "RVOL" not in joined
    assert "phá đỉnh" not in joined


def test_screen_one_thin_history_returns_none(monkeypatch):
    monkeypatch.setattr(
        screening, "load_ohlcv",
        lambda ticker, date, fill_gaps: _ohlcv_frame([100.0 + i for i in range(40)]),
    )
    assert screening.screen_one(
        "NVDA", "2026-10-01", asset_type="stock", benchmark_momentum=0.0
    ) is None


def test_screen_one_vendor_error_returns_none_not_raised(monkeypatch):
    def boom(ticker, date, fill_gaps):
        raise NoMarketDataError(ticker, ticker, "no rows")

    monkeypatch.setattr(screening, "load_ohlcv", boom)
    assert screening.screen_one(
        "AAPL", "2026-10-01", asset_type="stock", benchmark_momentum=0.0
    ) is None


def test_screen_one_illiquid_candidate_is_gated_out(monkeypatch):
    """Below the daily dollar-volume floor the candidate is dropped entirely —
    transaction costs erase the momentum edge down there (Starkiller)."""
    closes = _uptrend()
    monkeypatch.setattr(
        screening, "load_ohlcv",
        lambda ticker, date, fill_gaps: _ohlcv_frame(closes, volumes=[50_000.0] * 60),
    )
    assert screening.screen_one(
        "AAPL", "2026-10-01", asset_type="stock", benchmark_momentum=0.0
    ) is None


def test_screen_one_overextended_rvol_is_penalized(monkeypatch):
    closes = _uptrend()
    volumes = [_FLAT_VOLUME] * 59 + [_FLAT_VOLUME * 6]
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes, volumes)
    )
    candidate = screening.screen_one(
        "BTC-USD", "2026-10-01", asset_type="crypto", benchmark_momentum=0.0
    )
    assert candidate is not None
    assert candidate.volume_ratio >= 4.0
    assert any("RVOL >4" in reason for reason in candidate.reasons)


def test_screen_one_parabolic_run_flags_chasing(monkeypatch):
    """A monotonic run has RSI at its ceiling — the screen must say so instead
    of rewarding the chase."""
    closes = _uptrend(step=2.0)
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes)
    )
    candidate = screening.screen_one(
        "MOVR-USD", "2026-10-01", asset_type="crypto", benchmark_momentum=0.0
    )
    assert candidate is not None
    assert candidate.rsi14 is not None and candidate.rsi14 >= 75
    assert any("đu đỉnh" in reason for reason in candidate.reasons)


def test_screen_universe_ranks_and_truncates(monkeypatch):
    def fake_load(ticker, date, fill_gaps):
        if ticker == "HI":
            return _ohlcv_frame(_uptrend(step=2.0))
        if ticker == "MID":
            return _ohlcv_frame(_uptrend(step=0.5))
        return _ohlcv_frame([100.0] * 60)  # LO flat

    monkeypatch.setattr(screening, "load_ohlcv", fake_load)
    ranked = screening.screen_universe(["LO", "HI"], ["MID"], "2026-10-01", top_n=2)
    assert len(ranked) == 2
    assert ranked[0]["ticker"] == "HI"
    assert ranked[1]["ticker"] == "MID"
    assert ranked[0]["asset_type"] == "crypto"
    assert ranked[1]["asset_type"] == "stock"


def test_screen_universe_keeps_universe_asset_type_for_off_set_crypto(monkeypatch):
    """Liquid crypto outside the closed executable set (QNT-USD) must keep the
    crypto type from its universe, not fall back to closed-set inference."""
    closes = _uptrend()
    volumes = [_FLAT_VOLUME] * 59 + [_FLAT_VOLUME * 3]
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes, volumes)
    )
    ranked = screening.screen_universe(["QNT-USD"], [], "2026-10-01", top_n=5)
    (candidate,) = ranked
    assert candidate["ticker"] == "QNT-USD"
    assert candidate["asset_type"] == "crypto"
    assert candidate["rsi14"] is not None
    assert candidate["breakout"] is True


def test_crypto_universe_via_module_seam(monkeypatch):
    """The ccxt client is resolved through getattr(ccxt, exchange_id); the fake
    stands in at that exact seam."""
    import sys
    import types

    class FakeExchange:
        symbols = ["BTC/USDT", "USDC/USDT", "DOGE/USDT", "ZEC/USDT", "ETH/BTC"]

        def __init__(self, config):
            self.demo_enabled = False
            self.urls = {
                "api": {
                    "public": "https://api.binance.com/api/v3",
                    "private": "https://api.binance.com/api/v3",
                    "fapiPublic": "https://fapi.binance.com/fapi/v1",
                    "fapiPrivate": "https://fapi.binance.com/fapi/v1",
                }
            }

        def enable_demo_trading(self, enabled):
            self.demo_enabled = enabled
            self.urls["api"] = {
                key: value.replace("https://api.binance.com", "https://demo-api.binance.com")
                .replace("https://fapi.binance.com", "https://demo-fapi.binance.com")
                for key, value in self.urls["api"].items()
            }

        def market(self, symbol):
            return {"base": symbol.split("/", 1)[0]}

        def fetch_tickers(self):
            return {
                "BTC/USDT": {"quoteVolume": 1_000_000_000},
                "USDC/USDT": {"quoteVolume": 3_000_000_000},
                "DOGE/USDT": {"quoteVolume": 82_000_000},
                "RLUSD/USDT": {"quoteVolume": 40_000_000},
                "ZEC/USDT": {"quoteVolume": 4_000_000},
                "ETH/BTC": {"quoteVolume": 9_000_000_000},
            }

    fake_ccxt = types.SimpleNamespace(binance=FakeExchange)
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    universe = screening.crypto_universe(
        {"exec_exchange_id": "binance", "exec_base_url": "https://demo-api.binance.com"},
        top_n=5,
    )
    exchange = screening._public_exchange(
        {"exec_exchange_id": "binance", "exec_base_url": "https://demo-api.binance.com"}
    )
    assert exchange.demo_enabled is True
    assert exchange.urls["api"]["public"].startswith("https://demo-api.binance.com/")
    # stablecoin dropped, RLUSD stablecoin dropped, thin dropped, non-USDT dropped
    assert universe == ["BTC-USD", "DOGE-USD"]


def test_crypto_universe_volume_floor_excludes_mid_liquidity_pairs(monkeypatch):
    """A pair above the old 5M floor but below the 10M floor stays out: the
    universe pool is for assets that can actually carry an order."""
    import sys
    import types

    class FakeExchange:
        symbols = ["BTC/USDT", "LOW/USDT", "OK/USDT"]

        def __init__(self, config):
            self.demo_enabled = False

        def enable_demo_trading(self, enabled):
            self.demo_enabled = enabled

        def market(self, symbol):
            return {"base": symbol.split("/", 1)[0]}

        def fetch_tickers(self):
            return {
                "BTC/USDT": {"quoteVolume": 1_000_000_000},
                "LOW/USDT": {"quoteVolume": 7_000_000},   # above old 5M, below 10M
                "OK/USDT": {"quoteVolume": 12_000_000},   # above the floor
            }

    fake_ccxt = types.SimpleNamespace(binance=FakeExchange)
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)

    universe = screening.crypto_universe(
        {"exec_exchange_id": "binance", "exec_base_url": "https://demo-api.binance.com"},
        top_n=5,
    )
    assert universe == ["BTC-USD", "OK-USD"]


def test_crypto_universe_uses_production_only_when_configured(monkeypatch):
    import sys
    import types

    class FakeExchange:
        def __init__(self, config):
            self.demo_enabled = False
            self.urls = {"api": {"public": "https://api.binance.com/api/v3"}}

        def fetch_tickers(self):
            return {}

    monkeypatch.setitem(sys.modules, "ccxt", types.SimpleNamespace(binance=FakeExchange))

    exchange = screening._public_exchange(
        {"exec_exchange_id": "binance", "exec_base_url": "https://api.binance.com"}
    )

    assert exchange.demo_enabled is False
    assert exchange.urls["api"]["public"].startswith("https://api.binance.com/")


def test_crypto_universe_failure_raises_vendor_error(monkeypatch):
    import sys
    import types

    class Broken:
        def __init__(self, config):
            pass

        def fetch_tickers(self):
            raise OSError("connection refused")

    fake_ccxt = types.SimpleNamespace(binance=Broken)
    monkeypatch.setitem(sys.modules, "ccxt", fake_ccxt)
    with pytest.raises(VendorError):
        screening.crypto_universe({"exec_exchange_id": "binance"}, top_n=5)


def test_stock_universe_falls_back_when_screener_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("screener down")

    # The function imports yfinance inside its body, so patch the source module.
    import yfinance

    monkeypatch.setattr(yfinance, "EquityQuery", boom, raising=False)
    monkeypatch.setattr(yfinance, "screen", boom, raising=False)
    universe = screening.stock_universe({}, top_n=5)
    assert universe == list(screening._STOCK_UNIVERSE_FALLBACK[:5])


def test_atr_targets_derive_protective_exits_from_atr(monkeypatch):
    """The bracket prices bracket the last close: stop below, target above,
    both derived from the ATR the fixtures imply (High/Low = ±1%)."""
    closes = [100.0 + (i % 5) * 2 for i in range(60)]  # wobble, no NaN ATR
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes)
    )
    stop, take = screening.atr_targets("BTC-USD", "2026-10-01", "crypto")
    assert stop is not None and take is not None
    last = closes[-1]
    assert stop < last < take
    sl_dist = (last - stop) / last * 100
    tp_dist = (take - last) / last * 100
    assert screening._STOP_MIN_PCT <= sl_dist <= screening._STOP_MAX_PCT
    assert screening._TP_MIN_PCT <= tp_dist <= screening._TP_MAX_PCT


def test_atr_targets_vendor_error_returns_none_pair(monkeypatch):
    def boom(ticker, date, fill_gaps):
        raise NoMarketDataError(ticker, ticker, "no rows")

    monkeypatch.setattr(screening, "load_ohlcv", boom)
    assert screening.atr_targets("BTC-USD", "2026-10-01") == (None, None)


def test_atr_targets_signed_long_matches_atr_targets(monkeypatch):
    """The signed refactor must not change the long behavior: identical
    distances, stop below entry, target above."""
    closes = [100.0 + (i % 5) * 2 for i in range(60)]
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes)
    )
    signed = screening.atr_targets_signed("BTC-USD", "2026-10-01", "long")
    assert signed == screening.atr_targets("BTC-USD", "2026-10-01", "crypto")
    stop, take = signed
    last = closes[-1]
    assert stop is not None and take is not None
    assert stop < last < take


def test_atr_targets_signed_short_mirrors_long(monkeypatch):
    """A short entry is the mirror image: stop ABOVE entry, target BELOW, and
    the distances match the long side exactly — same ATR, opposite side."""
    closes = [100.0 + (i % 5) * 2 for i in range(60)]
    monkeypatch.setattr(
        screening, "load_ohlcv", lambda ticker, date, fill_gaps: _ohlcv_frame(closes)
    )
    last = closes[-1]
    long_stop, long_take = screening.atr_targets_signed("BTC-USD", "2026-10-01", "long")
    short_stop, short_take = screening.atr_targets_signed("BTC-USD", "2026-10-01", "short")
    assert short_stop > last > short_take
    assert short_stop == pytest.approx(last + (last - long_stop))
    assert short_take == pytest.approx(last - (long_take - last))


def test_atr_targets_signed_rejects_unknown_direction(monkeypatch):
    monkeypatch.setattr(
        screening, "load_ohlcv",
        lambda ticker, date, fill_gaps: _ohlcv_frame([100.0] * 60),
    )
    with pytest.raises(ValueError, match="direction"):
        screening.atr_targets_signed("BTC-USD", "2026-10-01", "sideways")


def test_atr_targets_signed_vendor_error_returns_none_pair(monkeypatch):
    def boom(ticker, date, fill_gaps):
        raise NoMarketDataError(ticker, ticker, "no rows")

    monkeypatch.setattr(screening, "load_ohlcv", boom)
    assert screening.atr_targets_signed("BTC-USD", "2026-10-01", "short") == (None, None)
