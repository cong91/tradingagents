"""Market screening for the auto-scan: build a discovery universe and rank it.

One job: given nothing but the current date, decide *which* instruments are
worth the expensive per-ticker LLM pipeline. The criteria are research-backed
(cross-sectional momentum with volume confirmation, George-Hwang 52-week-high
proximity, RSI discipline, ATR% tradability band, dollar-volume liquidity
floor) and computable from data already in the dependency tree:

* crypto — ccxt public market data (no credentials needed for tickers) on the
  configured ``exec_exchange_id``; ranked by 24h quote volume, mapped to the
  pipeline's Yahoo form (``BTC-USD``). Stablecoins and pairs with no Yahoo
  quote are dropped. The volume-ranked universe (freqtrade VolumePairlist
  pattern) deliberately includes movers that are NOT top-cap.
* stocks — Yahoo's equity screener (``yfinance.screen``): US equities above a
  market-cap floor that are ALREADY strong over 52 weeks (George-Hwang tilt),
  largest 52-week gainers first — a discovery universe, not a fame list. The
  screener is a live surface, so every failure degrades to the static
  large-cap fallback list rather than raising.

The screen itself is deliberately LLM-free and cheap: every metric comes from
the cached OHLCV series each candidate maps to, scored into a bounded number
with human-readable ``reasons``. Point-in-time integrity holds because
``load_ohlcv`` already filters to ``curr_date``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

logger = logging.getLogger(__name__)

# Stablecoins carry no trade thesis and skew volume rankings; exclude by base.
_STABLECOIN_BASES = frozenset(
    {"USDC", "USD1", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR"}
)

# Per-asset 24h dollar-volume floor for a spot pair to enter the universe.
MIN_CRYPTO_QUOTE_VOLUME_USD = 5_000_000
# Stock screener floor: below this market cap slippage swamps the signal.
_MIN_STOCK_MARKET_CAP = 2_000_000_000
# Daily dollar-volume floor (20d average) for a candidate to be tradable at
# retail size: ~125bps round-trip costs erase the momentum edge below this
# (Starkiller universe construction), so it gates exclusion, not just score.
_MIN_AVG_DOLLAR_VOLUME_CRYPTO = 5_000_000
_MIN_AVG_DOLLAR_VOLUME_STOCK = 10_000_000

# Static fallback for the Yahoo screener: liquid US large caps. Kept in engine
# code (not server state) so every consumer sees the same universe.
_STOCK_UNIVERSE_FALLBACK: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
    "BRK-B", "AVGO", "JPM", "LLY", "V", "XOM", "UNH", "MA", "COST", "HD",
    "PG", "JNJ", "NFLX", "WMT", "ORCL", "MRK", "AMD", "CRM", "BAC", "KO",
    "PEP", "ABBV", "TMO", "CVX", "ADBE", "DIS", "WFC", "CSCO", "ACN", "MCD",
)

# Benchmarks for relative strength: crypto candidates are measured against
# BTC, stocks against the S&P 500. One cached OHLCV read per screen run.
_BENCHMARK = {"crypto": "BTC-USD", "stock": "^GSPC"}


@dataclass
class Candidate:
    """One screened instrument with its metrics, ranked by score."""

    ticker: str
    asset_type: str
    score: float
    last_close: float
    momentum_pct: float
    trend_pct: float
    volume_ratio: float
    rsi14: float | None
    atr_pct: float | None
    dist_52w_pct: float
    breakout: bool
    avg_dollar_volume: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "asset_type": self.asset_type,
            "score": round(self.score, 4),
            "last_close": self.last_close,
            "momentum_pct": round(self.momentum_pct, 2),
            "trend_pct": round(self.trend_pct, 2),
            "volume_ratio": round(self.volume_ratio, 3),
            "rsi14": None if self.rsi14 is None else round(self.rsi14, 1),
            "atr_pct": None if self.atr_pct is None else round(self.atr_pct, 2),
            "dist_52w_pct": round(self.dist_52w_pct, 2),
            "breakout": self.breakout,
            "avg_dollar_volume": round(self.avg_dollar_volume, 2),
            "reasons": list(self.reasons),
        }


def _public_exchange(config: dict):
    """Build the screening client from the same Binance venue profile as execution."""
    exchange_id = str(config.get("exec_exchange_id") or "binance").strip().lower()
    import ccxt  # lazy like bridge.py: importing dataflows stays cheap

    exchange_class = getattr(ccxt, exchange_id, None)
    if not isinstance(exchange_class, type):
        raise VendorError(f"crypto universe unavailable via {exchange_id}: unsupported exchange")
    exchange = exchange_class({"enableRateLimit": True})
    if exchange_id == "binance":
        base_url = str(config.get("exec_base_url") or "https://demo-api.binance.com")
        from tradingagents.execution.bridge import _binance_profile

        profile = _binance_profile(base_url)
        if profile == "demo":
            enable_demo_trading = getattr(exchange, "enable_demo_trading", None)
            if not callable(enable_demo_trading):
                raise VendorError("crypto universe unavailable via binance: Demo Trading is unsupported")
            enable_demo_trading(True)
    return exchange


def crypto_universe(config: dict, top_n: int) -> list[str]:
    """Top ``top_n`` liquid crypto as pipeline tickers (``BTC-USD`` form).

    Public exchange data only — no credentials are read or sent. The ranking
    is by 24h quote volume, not market cap, so the universe contains active
    movers rather than the famous names. Bases the pipeline's closed crypto
    set does not know are kept: the OHLCV vendor resolves any Yahoo-listed
    ``BASE-USD`` and the bridge plans through ``exec_symbol_overrides`` if one
    of them ever earns a plan.
    """
    exchange_id = str(config.get("exec_exchange_id") or "binance").strip().lower()

    try:
        exchange = _public_exchange(config)
        tickers = exchange.fetch_tickers()
    except Exception as exc:  # noqa: BLE001 — any ccxt/network failure degrades
        raise VendorError(f"crypto universe unavailable via {exchange_id}: {exc}") from exc

    ranked: list[tuple[str, float]] = []
    for symbol, ticker in tickers.items():
        if not isinstance(symbol, str) or not symbol.endswith("/USDT"):
            continue
        market = exchange.market(symbol) if exchange_id else None
        try:
            base = str(market.get("base", "")) if market else symbol.split("/", 1)[0]
            quote_volume = float(ticker.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue
        if base in _STABLECOIN_BASES or quote_volume < MIN_CRYPTO_QUOTE_VOLUME_USD:
            continue
        ranked.append((f"{base}-USD", quote_volume))

    ranked.sort(key=lambda entry: -entry[1])
    seen: set[str] = set()
    out: list[str] = []
    for ticker, _volume in ranked:
        if ticker not in seen:
            seen.add(ticker)
            out.append(ticker)
        if len(out) >= top_n:
            break
    return out


def stock_universe(config: dict, top_n: int) -> list[str]:
    """Top ``top_n`` US equities ranked by 52-week performance as tickers.

    The discovery tilt is the sort: strong-over-52-weeks first (George-Hwang
    anchoring), mid-cap floor instead of mega-cap-only. Yahoo's screener is
    the source; its failures degrade to the static large-cap fallback so an
    outage never blocks an auto-scan entirely.
    """
    try:
        from yfinance import EquityQuery, screen

        query = EquityQuery(
            "and",
            [
                EquityQuery("eq", ["region", "us"]),
                EquityQuery("gt", ["intradaymarketcap", _MIN_STOCK_MARKET_CAP]),
                EquityQuery("gt", ["fiftytwowkpercentchange", 0]),
            ],
        )
        response = screen(
            query, size=top_n, sortField="fiftytwowkpercentchange", sortAsc=False
        )
        rows = response.get("quotes", []) if isinstance(response, dict) else []
        symbols = [str(row.get("symbol", "")).strip() for row in rows]
        out = [symbol for symbol in symbols if symbol]
    except Exception as exc:  # noqa: BLE001 — screener is best-effort
        logger.warning("stock screener failed (%s: %s); using static universe", type(exc).__name__, exc)
        out = []
    if not out:
        out = list(_STOCK_UNIVERSE_FALLBACK)
    return out[:top_n]


def _momentum(close, lookback: int) -> float | None:
    if len(close) < lookback + 1:
        return None
    return (float(close.iloc[-1]) / float(close.iloc[-lookback - 1]) - 1.0) * 100.0


def _benchmark_momentum(asset_type: str, curr_date: str, lookback: int) -> float:
    """Momentum of the asset class benchmark; 0.0 when unreadable (a screen
    must never fail because the benchmark row is missing)."""
    try:
        frame = load_ohlcv(_BENCHMARK[asset_type], curr_date, fill_gaps=True)
        value = _momentum(frame["Close"].dropna(), lookback)
        return value if value is not None else 0.0
    except (VendorError, OSError, ValueError, KeyError) as exc:
        logger.info("benchmark %s unreadable: %s", _BENCHMARK[asset_type], exc)
        return 0.0


def screen_one(
    ticker: str,
    curr_date: str,
    *,
    asset_type: str,
    benchmark_momentum: float,
    lookback: int = 20,
) -> Candidate | None:
    """Score one ticker against the research-backed criteria; None when the
    data is too thin or the tradability gate fails.

    ``asset_type`` comes from the universe that produced the ticker — the
    universe builders know the truth, and guessing here mislabels liquid
    crypto outside the closed executable set (QNT-USD, WLD-USD) as stocks.
    """
    try:
        data = load_ohlcv(ticker, curr_date, fill_gaps=True)
    except VendorError as exc:
        logger.info("screening %s skipped: %s", ticker, exc)
        return None
    except (OSError, ValueError, KeyError) as exc:
        logger.info("screening %s skipped: %s: %s", ticker, type(exc).__name__, exc)
        return None

    close = data["Close"].dropna()
    volume = data["Volume"].dropna() if "Volume" in data else None
    if len(close) < 60:  # SMA50 + RSI + 52w window all need real history
        return None

    momentum = _momentum(close, lookback)
    if momentum is None:
        return None
    last = float(close.iloc[-1])

    avg_dollar_volume = (
        float((close.iloc[-20:] * volume.iloc[-20:]).mean()) if volume is not None else 0.0
    )
    floor = (
        _MIN_AVG_DOLLAR_VOLUME_CRYPTO
        if asset_type == "crypto"
        else _MIN_AVG_DOLLAR_VOLUME_STOCK
    )
    if avg_dollar_volume < floor:
        logger.info("screening %s skipped: dollar volume %.0f < %.0f",
                    ticker, avg_dollar_volume, floor)
        return None

    sma20 = float(close.iloc[-20:].mean())
    sma50 = float(close.iloc[-50:].mean())
    trend = (last / sma20 - 1.0) * 100.0

    if volume is not None and len(volume) >= 20:
        ratio = float(volume.iloc[-1]) / max(float(volume.iloc[-20:].mean()), 1e-12)
    else:
        ratio = 1.0

    window = data.tail(252)
    high_52w = float(window["High"].max() if "High" in window else window["Close"].max())
    dist_52w = (last / high_52w - 1.0) * 100.0  # ≤ 0; 0 = at the high
    breakout = bool(
        len(data) >= 22
        and last > float(data["High"].iloc[-22:-1].max())
        and ratio >= 2.0
    )

    rsi14 = atr_pct = None
    try:
        from stockstats import wrap

        df = wrap(data[["Date", "Open", "High", "Low", "Close", "Volume"]])
        rsi_value = float(df["rsi"].iloc[-1])
        if rsi_value == rsi_value:  # not NaN
            rsi14 = rsi_value
        atr_value = float(df["atr"].iloc[-1])
        if atr_value == atr_value and last > 0:
            atr_pct = atr_value / last * 100.0
    except (ImportError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.info("screening %s: RSI/ATR unavailable: %s", ticker, exc)

    score = 0.0
    reasons: list[str] = []

    if momentum >= 5.0:
        score += min(abs(momentum) / 10.0, 2.0)
        reasons.append(f"+{momentum:.1f}% trong {lookback} phiên")
    elif momentum <= -5.0:
        score += min(abs(momentum) / 15.0, 1.5)
        reasons.append(f"{momentum:.1f}% trong {lookback} phiên (quá bán tiềm năng)")

    if last > sma20:
        score += 1.0
        reasons.append("giá trên SMA20")
        if last > sma50:
            score += 0.5
            reasons.append("trên cả SMA50 (xu hướng tăng kép)")
    if momentum - benchmark_momentum >= 3.0:
        score += 0.75
        reasons.append(f"mạnh hơn benchmark {momentum - benchmark_momentum:.1f} điểm %")

    if ratio >= 2.0:
        score += min(ratio - 1.0, 1.5)
        reasons.append(f"RVOL x{ratio:.1f} (khối lượng đột biến)")
    if ratio >= 4.0:
        score -= 0.75
        reasons.append("RVOL >4 — nguy cơ cực hạn, dễ hồi")
    elif ratio >= 1.5:
        score += 0.5
        reasons.append(f"khối lượng x{ratio:.1f} so với trung bình")

    if rsi14 is not None:
        if rsi14 >= 75:
            score -= 0.75
            reasons.append(f"RSI {rsi14:.0f} — đang đu đỉnh")
        elif 45.0 <= rsi14 <= 70.0:
            score += 0.5
            reasons.append(f"RSI {rsi14:.0f} — vùng sức mạnh còn dư địa")
        elif rsi14 < 30:
            score += 0.5
            reasons.append(f"RSI {rsi14:.0f} — quá bán")

    if dist_52w >= -5.0:
        score += 1.0
        reasons.append(f"sát đỉnh 52 tuần (chỉ ~{abs(dist_52w):.0f}%)")
    elif dist_52w >= -15.0:
        score += 0.5
        reasons.append("trong 15% quanh đỉnh 52 tuần")

    if breakout:
        score += 1.0
        reasons.append("phá đỉnh 20 phiên kèm khối lượng")

    if atr_pct is not None:
        band = (3.0, 10.0) if asset_type == "crypto" else (2.0, 8.0)
        if band[0] <= atr_pct <= band[1]:
            score += 0.5
            reasons.append(f"ATR {atr_pct:.1f}% — biến động trong tầm kiểm soát")
        elif atr_pct > 20.0:
            score -= 0.75
            reasons.append(f"ATR {atr_pct:.0f}% — biến động quá nguy hiểm")

    return Candidate(
        ticker=ticker,
        asset_type=asset_type,
        score=score,
        last_close=last,
        momentum_pct=momentum,
        trend_pct=trend,
        volume_ratio=ratio,
        rsi14=rsi14,
        atr_pct=atr_pct,
        dist_52w_pct=dist_52w,
        breakout=breakout,
        avg_dollar_volume=avg_dollar_volume,
        reasons=reasons,
    )


def screen_universe(
    crypto: list[str],
    stocks: list[str],
    curr_date: str,
    top_n: int,
    lookback: int = 20,
) -> list[dict]:
    """Rank both universes by screen score; return the top ``top_n`` as dicts.

    Tickers keep the asset type of the universe they came from — that type
    decides the LLM analysts and the advisory/plan path downstream. Each
    universe shares its class benchmark for the relative-strength bonus.
    """
    candidates: list[Candidate] = []
    for asset_type, tickers in (("crypto", crypto), ("stock", stocks)):
        benchmark = _benchmark_momentum(asset_type, curr_date, lookback)
        for ticker in tickers:
            candidate = screen_one(
                ticker, curr_date, asset_type=asset_type,
                benchmark_momentum=benchmark, lookback=lookback,
            )
            if candidate is not None:
                candidates.append(candidate)
    candidates.sort(key=lambda c: (-c.score, c.ticker))
    return [candidate.as_dict() for candidate in candidates[:top_n]]


# ATR multiples for the protective bracket: a 1.5x-ATR stop sits outside the
# day's normal noise, a 2.0x-ATR target keeps the reward:risk at 1.33:1 —
# the entry quality (breakout + volume) is what has to carry the expectancy.
_STOP_ATR_MULT = 1.5
_TAKE_PROFIT_ATR_MULT = 2.0
# Sanity clamps in percent of entry: below the floor the stop is inside noise,
# above the ceiling a single loss breaches the risk caps anyway.
_STOP_MIN_PCT, _STOP_MAX_PCT = 1.0, 20.0
_TP_MIN_PCT, _TP_MAX_PCT = 1.5, 30.0


def atr_targets(
    ticker: str, curr_date: str, asset_type: str = "crypto"
) -> tuple[float | None, float | None]:
    """(stop_loss, take_profit) for a LONG at the last close, ATR-derived.

    Both are exchange-order prices computed from the cached OHLCV series, so
    the daily worker can attach a protective bracket to every entry plan
    without a second market read. ``(None, None)`` when ATR is unavailable —
    the caller then enters without a bracket rather than inventing distances.
    """
    return atr_targets_signed(ticker, curr_date, "long")


def atr_targets_signed(
    ticker: str, curr_date: str, direction: str
) -> tuple[float | None, float | None]:
    """(stop_loss, take_profit) for a LONG or SHORT entry at the last close.

    The ATR distances are identical for both directions; only the side of the
    entry price flips: a long stops BELOW entry and targets ABOVE it, a short
    mirrors that (stop ABOVE, target BELOW), so a short plan satisfies
    ``stop > entry > take``. ``(None, None)`` when ATR is unavailable — the
    caller then enters without a bracket rather than inventing distances.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    try:
        data = load_ohlcv(ticker, curr_date, fill_gaps=True)
    except (VendorError, OSError, ValueError, KeyError) as exc:
        logger.info("atr_targets %s skipped: %s", ticker, exc)
        return None, None
    close = data["Close"].dropna()
    if close.empty:
        return None, None
    last = float(close.iloc[-1])
    if last <= 0:
        return None, None
    try:
        from stockstats import wrap

        df = wrap(data[["Date", "Open", "High", "Low", "Close", "Volume"]])
        atr = float(df["atr"].iloc[-1])
    except (ImportError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.info("atr_targets %s: ATR unavailable: %s", ticker, exc)
        return None, None
    if atr != atr or atr <= 0:  # NaN guard
        return None, None
    atr_pct = atr / last * 100.0
    stop_dist = min(max(atr_pct * _STOP_ATR_MULT, _STOP_MIN_PCT), _STOP_MAX_PCT)
    tp_dist = min(max(atr_pct * _TAKE_PROFIT_ATR_MULT, _TP_MIN_PCT), _TP_MAX_PCT)
    if direction == "long":
        return round(last * (1.0 - stop_dist / 100.0), 8), round(
            last * (1.0 + tp_dist / 100.0), 8
        )
    return round(last * (1.0 + stop_dist / 100.0), 8), round(
        last * (1.0 - tp_dist / 100.0), 8
    )
