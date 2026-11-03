"""Bounded, read-only Binance USD-M futures execution history.

The response describes only the explicitly requested symbols and time interval;
positions may predate it and the venue's historical retention is not known.
No account settings, orders, or audit records are changed.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from server import binance_futures_positions, performance_history_cache

_WEEK_MS = 7 * 24 * 60 * 60 * 1000
_MAX_SYMBOLS = 20
_MAX_PAGES = 20
_MAX_TIMESTAMP_MS = 253402300799999
_SYMBOL = re.compile(r"^[A-Z0-9]{1,30}/[A-Z0-9]{1,15}:[A-Z0-9]{1,15}(?:-[0-9]{6})?$")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_BAN_UNTIL = re.compile(r"\bIP banned until ([0-9]{13})(?![A-Za-z0-9_])", re.IGNORECASE)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FuturesTradeFill(TypedDict):
    trade_id: str
    order_id: str | None
    symbol: str
    side: Literal["buy", "sell"]
    timestamp: str
    contracts: str
    price: str
    fee: str | None
    fee_currency: str | None
    realized_pnl: str | None
    realized_pnl_currency: str | None


class FuturesTradeHistory(TypedDict):
    status: Literal["ok", "partial", "error", "unsupported", "unavailable"]
    source: Literal["venue_fills", "unavailable"]
    venue_profile: Literal["demo", "production"] | None
    start_at: str
    end_at: str
    observed_at: str | None
    coverage: str
    warnings: list[str]
    fills: list[FuturesTradeFill]
    retry_after_seconds: float | None


def _iso(milliseconds: int) -> str:
    return (_EPOCH + timedelta(milliseconds=milliseconds)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _profile(bridge: Any) -> Literal["demo", "production"] | None:
    config = getattr(bridge, "config", {})
    if not isinstance(config, dict):
        return None
    try:
        url = urlsplit(config.get("exec_base_url") or "https://demo-api.binance.com")
        if (
            url.scheme != "https" or url.path not in ("", "/") or url.query or url.fragment
            or url.username is not None or url.password is not None or url.port not in (None, 443)
        ):
            return None
    except (TypeError, ValueError):
        return None
    if url.hostname in {"demo-api.binance.com", "demo-fapi.binance.com"}:
        return "demo"
    if url.hostname in {"api.binance.com", "fapi.binance.com"}:
        return "production"
    return None


def _endpoint_matches(client: Any, profile: str) -> bool:
    try:
        api = client.urls["api"]
        endpoint = urlsplit(api["fapiPrivate"])
        expected = "demo-fapi.binance.com" if profile == "demo" else "fapi.binance.com"
        return (
            getattr(client, "id", None) == "binance"
            and endpoint.scheme == "https" and endpoint.hostname == expected
            and endpoint.path.rstrip("/") == "/fapi/v1"
            and endpoint.port in (None, 443)
            and endpoint.username is None and endpoint.password is None
            and not endpoint.query and not endpoint.fragment
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _private_logging_disabled(client: Any) -> bool:
    if getattr(client, "verbose", False) is not False:
        return False
    logger = getattr(client, "logger", None)
    return logger is None or (
        callable(getattr(logger, "isEnabledFor", None))
        and not logger.isEnabledFor(logging.DEBUG)
    )


def _decimal_text(value: Any, *, positive: bool = False) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value).strip()
    if not _DECIMAL.fullmatch(text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or (positive and number <= 0):
        return None
    return text


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdigit() or len(text) > 30:
        return None
    return str(int(text))


def _currency(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{1,20}", value):
        return value
    return None


def _normalize_fill(raw: Any, symbol: str, market_id: Any) -> FuturesTradeFill | None:
    if not isinstance(raw, dict) or raw.get("symbol") != symbol:
        return None
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    if not isinstance(market_id, str) or not market_id or info.get("symbol") != market_id:
        return None
    trade_id = _identifier(raw.get("id"))
    raw_ids = [info[key] for key in ("id", "tradeId") if key in info]
    if trade_id is None or not raw_ids or any(_identifier(value) != trade_id for value in raw_ids):
        return None
    order_id = _identifier(raw.get("order")) if raw.get("order") is not None else None
    side = raw.get("side")
    if info.get("side") not in ("BUY", "SELL") or info["side"].lower() != side:
        return None
    timestamp = raw.get("timestamp")
    contracts = _decimal_text(info.get("qty", raw.get("amount")), positive=True)
    price = _decimal_text(info.get("price", raw.get("price")), positive=True)
    if (
        trade_id is None or (raw.get("order") is not None and order_id is None)
        or side not in ("buy", "sell") or type(timestamp) is not int
        or not 0 <= timestamp <= _MAX_TIMESTAMP_MS or contracts is None or price is None
    ):
        return None
    fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else {}
    fee_value = info.get("commission") if "commission" in info else fee.get("cost")
    fee_currency = info.get("commissionAsset") if "commissionAsset" in info else fee.get("currency")
    pnl_value = info.get("realizedPnl")
    pnl_currency = info.get("marginAsset") if pnl_value is not None else None
    commission = _decimal_text(fee_value)
    pnl = _decimal_text(pnl_value)
    if (
        (fee_value is not None and commission is None)
        or (pnl_value is not None and pnl is None)
        or (fee_currency is not None and _currency(fee_currency) is None)
        or (pnl_currency is not None and _currency(pnl_currency) is None)
    ):
        return None
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "symbol": symbol,
        "side": side,
        "timestamp": _iso(timestamp),
        "contracts": contracts,
        "price": price,
        "fee": commission,
        "fee_currency": _currency(fee_currency),
        "realized_pnl": pnl,
        "realized_pnl_currency": _currency(info.get("marginAsset")) if pnl is not None else None,
    }


def _retry_after_seconds(exc: Exception, client: Any = None) -> float | None:
    now = time.time()
    durations: list[float] = []
    response = getattr(exc, "response", None)
    for headers in (
        getattr(exc, "headers", None), getattr(response, "headers", None),
        response.get("headers", response) if isinstance(response, Mapping) else None,
        getattr(client, "last_response_headers", None),
    ):
        if not isinstance(headers, Mapping):
            continue
        for key, value in headers.items():
            if not isinstance(key, str) or key.lower() != "retry-after" or not isinstance(value, str):
                continue
            value = value.strip()
            if re.fullmatch(r"[0-9]+", value):
                try:
                    duration = float(value)
                except (OverflowError, ValueError):
                    continue
                if math.isfinite(duration):
                    durations.append(duration)
                continue
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is not None:
                    durations.append(max(0.0, retry_at.timestamp() - now))
            except (OverflowError, OSError, TypeError, ValueError):
                continue
    match = _BAN_UNTIL.search(str(exc))
    if match:
        durations.append(max(0.0, int(match[1]) / 1000 - now))
    return max(durations) if durations else None


def _warning(code: str, symbol: str, start: int, end: int) -> str:
    return f"{code}: {symbol} [{_iso(start)}, {_iso(end)})"


def _read_symbol(
    client: Any, symbol: str, market_id: Any, start: int, end: int, page_limit: int,
    deadline: float, fills: dict[tuple[str, str], FuturesTradeFill], warnings: list[str],
) -> bool:
    if time.monotonic() >= deadline:
        warnings.append(_warning("deadline_reached", symbol, start, end))
        return False
    params = {"type": "future", "paginate": False, "papi": False, "until": end - 1}
    if performance_history_cache.is_rate_limited():
        warnings.append("history_rate_limit_cooldown")
        return False
    try:
        page = client.fetch_my_trades(symbol, start, page_limit, params)
    except Exception as exc:
        if performance_history_cache.is_rate_limit_error(exc):
            warnings.append(_warning("rate_limited", symbol, start, end))
            raise
        warnings.append(_warning("fetch_failed", symbol, start, end))
        return False
    if not isinstance(page, list) or len(page) > page_limit:
        warnings.append(_warning("invalid_page", symbol, start, end))
        return False
    conflicted: set[tuple[str, str]] = set()
    bad_fill = False
    conflict = False
    for raw in page:
        fill = _normalize_fill(raw, symbol, market_id)
        if fill is None:
            bad_fill = True
            continue
        timestamp = raw["timestamp"]
        if start <= timestamp < end:
            key = (symbol, fill["trade_id"])
            if key in conflicted:
                continue
            existing = fills.get(key)
            if existing is not None and existing != fill:
                conflict = True
                conflicted.add(key)
                del fills[key]
            else:
                fills[key] = fill
    if bad_fill:
        warnings.append(_warning("invalid_fill", symbol, start, end))
    if conflict:
        warnings.append(_warning("conflicting_fill", symbol, start, end))
    if len(page) == page_limit:
        warnings.append(_warning("initial_page_may_be_truncated", symbol, start, end))
    return True


def read_futures_trade_history(
    bridge: Any, symbols: Sequence[str], since_ms: int, until_ms: int, *,
    page_limit: int = 1000, max_pages_per_symbol: int = 20, max_total_seconds: float = 20,
) -> FuturesTradeHistory:
    """Read confirmed USD-M fills from the bridge's configured Binance account.

    ``since_ms`` is inclusive and ``until_ms`` exclusive, both UTC epoch
    milliseconds. At most the latest seven requested days and 20 distinct
    unified linear-futures symbols are read. A cooperative monotonic deadline
    stops between calls; it cannot interrupt CCXT I/O or its internal retries.
    The caller must configure the client's network timeout separately.

    Amounts are decimal strings, preferring the exchange's original values.
    Missing fees, realized PnL, currencies, and starting basis remain unknown.
    Any normalized result is partial: CCXT can silently omit malformed raw
    records, including records with missing timestamps. A full initial page
    may also omit older fills, even at the same timestamp; it is not paged
    forward because doing so cannot recover those fills. ``max_pages_per_symbol``
    is retained as a request bound, but at most one page per symbol is read.
    """
    if (
        not isinstance(symbols, (list, tuple)) or not symbols
        or any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in symbols)
        or type(since_ms) is not int or type(until_ms) is not int
        or not 0 <= since_ms < until_ms <= _MAX_TIMESTAMP_MS
        or type(page_limit) is not int or not 1 <= page_limit <= 1000
        or type(max_pages_per_symbol) is not int or not 1 <= max_pages_per_symbol <= _MAX_PAGES
        or isinstance(max_total_seconds, bool) or not isinstance(max_total_seconds, (int, float))
        or not math.isfinite(max_total_seconds) or not 0 < max_total_seconds <= 60
    ):
        raise ValueError("invalid futures history request")
    deadline = time.monotonic() + max_total_seconds
    requested_symbols = list(dict.fromkeys(symbols))
    selected = requested_symbols[:_MAX_SYMBOLS]
    start = max(since_ms, until_ms - _WEEK_MS)
    coverage = (
        "binance_usd_m_futures; requested_symbols_only; starting_basis_unknown; "
        "venue_retention_unknown; full initial page may omit older fills, "
        "including fills at the same timestamp; symbols=" + ",".join(selected)
    )
    result: FuturesTradeHistory = {
        "status": "ok", "source": "venue_fills", "venue_profile": None,
        "start_at": _iso(start), "end_at": _iso(until_ms), "observed_at": None,
        "coverage": coverage, "warnings": [], "fills": [], "retry_after_seconds": None,
    }
    warnings = result["warnings"]
    if start != since_ms:
        warnings.append(f"time_limit_reached: omitted [{_iso(since_ms)}, {_iso(start)})")
    if len(requested_symbols) > _MAX_SYMBOLS:
        warnings.append(f"symbol_limit_reached: {len(requested_symbols) - _MAX_SYMBOLS} omitted")
    if str(getattr(bridge, "exchange_id", "")).strip().lower() != "binance":
        result.update(status="unsupported", source="unavailable")
        warnings.append("unsupported_exchange")
        return result
    profile = _profile(bridge)
    result["venue_profile"] = profile
    if profile is None:
        result.update(status="error", source="unavailable")
        warnings.append("unknown_profile")
        return result
    if time.monotonic() >= deadline:
        result.update(status="unavailable", source="unavailable")
        warnings.append("deadline_reached: all symbols unqueried")
        return result
    if performance_history_cache.is_rate_limited():
        result.update(status="unavailable", source="unavailable")
        warnings.append("history_rate_limit_cooldown")
        return result
    try:
        client = bridge.client()
    except Exception as exc:
        result.update(status="unavailable", source="unavailable")
        if performance_history_cache.is_rate_limit_error(exc):
            result["retry_after_seconds"] = _retry_after_seconds(exc)
            performance_history_cache.start_cooldown(result)
            warnings.append("rate_limited")
        else:
            warnings.append("client_unavailable")
        return result
    if not _endpoint_matches(client, profile):
        result.update(status="error", source="unavailable")
        warnings.append("profile_mismatch")
        return result
    if getattr(client, "enableRateLimit", None) is not True:
        result.update(status="error", source="unavailable")
        warnings.append("rate_limit_disabled")
        return result
    if not _private_logging_disabled(client):
        result.update(status="error", source="unavailable")
        warnings.append("private_logging_enabled")
        return result
    options = getattr(client, "options", {})
    if isinstance(options, dict):
        method_options = options.get("fetchMyTrades")
        scopes = (options, method_options) if isinstance(method_options, dict) else (options,)
        if any(scope.get(key) for scope in scopes for key in (
            "papi", "defaultPapi", "portfolioMargin", "defaultPortfolioMargin",
        )):
            result.update(status="unsupported", source="unavailable")
            warnings.append("portfolio_margin_unsupported")
            return result
    try:
        client = binance_futures_positions.isolated_client(client)
    except binance_futures_positions.BinanceProfileError:
        result.update(status="error", source="unavailable")
        warnings.append("profile_mismatch")
        return result
    if time.monotonic() >= deadline:
        result.update(status="unavailable", source="unavailable")
        warnings.append("deadline_reached: all symbols unqueried")
        return result
    if performance_history_cache.is_rate_limited():
        result.update(status="unavailable", source="unavailable")
        warnings.append("history_rate_limit_cooldown")
        return result
    try:
        markets = client.load_markets()
    except Exception as exc:
        result.update(status="unavailable", source="unavailable")
        if performance_history_cache.is_rate_limit_error(exc):
            result["retry_after_seconds"] = _retry_after_seconds(exc, client)
            performance_history_cache.start_cooldown(result)
            warnings.append("rate_limited")
        else:
            warnings.append("markets_unavailable")
        return result
    if not isinstance(markets, dict):
        result.update(status="unavailable", source="unavailable")
        warnings.append("markets_unavailable")
        return result
    fills: dict[tuple[str, str], FuturesTradeFill] = {}
    successful_reads = 0
    for symbol in selected:
        if performance_history_cache.is_rate_limited():
            warnings.append("history_rate_limit_cooldown")
            break
        if time.monotonic() >= deadline:
            warnings.append(_warning("deadline_reached", symbol, start, until_ms))
            break
        market = markets.get(symbol)
        if (
            not isinstance(market, dict) or market.get("symbol") != symbol
            or market.get("contract") is not True or market.get("linear") is not True
            or not (market.get("swap") is True or market.get("future") is True)
            or market.get("option") is True
        ):
            warnings.append(_warning("unsupported_symbol", symbol, start, until_ms))
            continue
        if not _private_logging_disabled(client):
            warnings.append("private_logging_enabled")
            break
        try:
            read = _read_symbol(client, symbol, market.get("id"), start, until_ms, page_limit,
                                deadline, fills, warnings)
        except Exception as exc:
            result["retry_after_seconds"] = _retry_after_seconds(exc, client)
            performance_history_cache.start_cooldown(result)
            break
        if read:
            successful_reads += 1
    result["fills"] = sorted(
        fills.values(), key=lambda fill: (fill["timestamp"], fill["symbol"], int(fill["trade_id"]))
    )
    if successful_reads:
        warnings.append(
            "normalized_history_unverified: CCXT may omit malformed raw records, "
            "including records with missing timestamps"
        )
        result["status"] = "partial"
        result["observed_at"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    else:
        result.update(status="unavailable", source="unavailable")
    return result
