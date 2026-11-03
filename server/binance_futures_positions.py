"""Display-only Binance USD-M position-risk reads, independent of CCXT filtering."""

from __future__ import annotations

import copy
import logging
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import ccxt
import requests

from server import performance_history_cache

_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value)
    if not _DECIMAL.fullmatch(text):
        return None
    try:
        exact = Decimal(text)
        converted = float(exact)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if not math.isfinite(converted) or (converted == 0 and exact != 0):
        return None
    return converted


class BinanceProfileError(ValueError):
    """The Binance client cannot be shown to match the configured profile."""


class BinanceAccountTypeError(ValueError):
    """The account cannot be shown to use the regular USD-M endpoint."""


class _NoRedirectSession(requests.Session):
    def request(self, method, url, **kwargs):
        if performance_history_cache.is_rate_limited():
            raise RuntimeError("venue read deferred during rate limit cooldown")
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if response.status_code in (418, 429):
            error = requests.HTTPError("Binance rate limited", response=response)
            performance_history_cache.start_cooldown({
                "retry_after_seconds": performance_history_cache.retry_after_from_exception(error),
            })
            limited = RuntimeError("Binance rate limited")
            limited.response = response
            raise limited from error
        if 300 <= response.status_code < 400:
            raise BinanceProfileError("Binance redirect refused")
        return response


def isolated_client(client: Any) -> Any:
    """Give real CCXT display reads a private, non-redirecting HTTP session."""
    if not isinstance(client, ccxt.Exchange):
        return client
    session = getattr(client, "session", None)
    if not isinstance(session, requests.Session):
        raise BinanceProfileError("Binance HTTP transport is not verified")
    if session.auth is not None or session.cookies or session.hooks.get("response"):
        raise BinanceProfileError("Binance HTTP session settings are not supported")
    transport = _NoRedirectSession()
    transport.headers.clear()
    transport.headers.update(session.headers)
    transport.trust_env = session.trust_env
    transport.proxies.update(session.proxies)
    transport.verify = session.verify
    transport.cert = session.cert
    reader = copy.copy(client)
    reader.session = transport
    reader.options = copy.deepcopy(client.options)
    return reader


def _matches_endpoint(value: Any, host: str, path: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https" and url.hostname == host and url.port in (None, 443)
            and url.username is None and url.password is None and url.path == path
            and not url.query and not url.fragment
        )
    except ValueError:
        return False


def validate_client(client: Any, profile: str | None, base_url: Any) -> None:
    """Fail before any account request if the CCXT private route is uncertain."""
    if profile not in {"demo", "production"}:
        raise BinanceProfileError("Binance profile is not verified")
    configured_hosts = (
        {"demo-api.binance.com", "demo-fapi.binance.com"}
        if profile == "demo" else {"api.binance.com", "fapi.binance.com"}
    )
    if not isinstance(base_url, str):
        raise BinanceProfileError("Binance configured route is not verified")
    try:
        configured = urlsplit(base_url)
        if not (
            configured.scheme == "https" and configured.hostname in configured_hosts
            and configured.port in (None, 443) and configured.path in ("", "/")
            and configured.username is None and configured.password is None
            and not configured.query and not configured.fragment
        ):
            raise BinanceProfileError("Binance configured route is not verified")
    except ValueError as exc:
        raise BinanceProfileError("Binance configured route is not verified") from exc
    urls = getattr(client, "urls", None)
    api = urls.get("api") if isinstance(urls, dict) else None
    options = getattr(client, "options", None)
    if not isinstance(api, dict) or not isinstance(options, dict):
        raise BinanceProfileError("Binance client routes are not verified")
    future_host = "demo-fapi.binance.com" if profile == "demo" else "fapi.binance.com"
    expected = {
        "fapiPrivate": (future_host, "/fapi/v1"),
        "fapiPrivateV2": (future_host, "/fapi/v2"),
        "fapiPrivateV3": (future_host, "/fapi/v3"),
    }
    if profile == "production":
        expected["sapi"] = ("api.binance.com", "/sapi/v1")
    if any(not _matches_endpoint(api.get(key), host, path) for key, (host, path) in expected.items()):
        raise BinanceProfileError("Binance private routes do not match the configured profile")
    if (
        options.get("enableDemoTrading", False) is not (profile == "demo")
        or options.get("sandboxMode", False) is not False
        or getattr(client, "verbose", False) is not False
    ):
        raise BinanceProfileError("Binance client mode is not verified")
    logger = getattr(client, "logger", None)
    if logger is not None and (
        not callable(getattr(logger, "isEnabledFor", None)) or logger.isEnabledFor(logging.DEBUG)
    ):
        raise BinanceProfileError("Binance client debug logging is not permitted")
    for option_set in (options, options.get("fetchBalance"), options.get("fetchPositionsRisk")):
        if option_set is not None and not isinstance(option_set, dict):
            raise BinanceProfileError("Binance account mode is not verified")
        if isinstance(option_set, dict) and any(
            option_set.get(name) not in (None, False) for name in (
                "papi", "defaultPapi", "portfolioMargin", "defaultPortfolioMargin",
            )
        ):
            raise BinanceProfileError("Binance portfolio-margin account mode is not supported")
    if not callable(getattr(client, "fapiPrivateV3GetPositionRisk", None)):
        raise BinanceProfileError("Binance V3 position endpoint is unavailable")


def _market_for_id(client: Any, market_id: str) -> dict | None:
    markets = getattr(client, "markets_by_id", None)
    if not isinstance(markets, dict):
        return None
    matches = markets.get(market_id)
    if isinstance(matches, dict):
        matches = [matches]
    if not isinstance(matches, list):
        return None
    candidates = [
        market for market in matches
        if isinstance(market, dict) and market.get("id") == market_id
        and market.get("contract") is True and market.get("linear") is True
        and market.get("inverse") is False
        and isinstance(market.get("symbol"), str) and market["symbol"].strip()
        and isinstance(market.get("quote"), str) and market["quote"].strip()
        and isinstance(market.get("settle"), str) and market["settle"].strip()
        and (market.get("settleId") is None or isinstance(market["settleId"], str))
    ]
    return candidates[0] if len(candidates) == 1 else None


def prepare_rows(rows: list, client: Any) -> list[tuple[dict | None, dict | None, str | None]]:
    """Map raw rows using loaded CCXT markets; do not return venue data in issues."""
    identities = []
    modes: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        symbol = row.get("symbol") if isinstance(row, dict) else None
        side = row.get("positionSide") if isinstance(row, dict) else None
        identity = (symbol, side) if (
            isinstance(symbol, str) and symbol and isinstance(side, str)
            and side in {"BOTH", "LONG", "SHORT"}
        ) else None
        identities.append(identity)
        if identity:
            modes[symbol].add("net" if side == "BOTH" else "hedge")
    counts = Counter(identity for identity in identities if identity is not None)
    prepared = []
    for row, identity in zip(rows, identities, strict=True):
        if not isinstance(row, dict):
            prepared.append((None, None, "invalid_row"))
            continue
        if any(name in row for name in ("nextPageCursor", "nextPageToken", "nextCursor")):
            prepared.append((None, None, "invalid_row"))
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
            prepared.append((None, None, "missing_symbol"))
            continue
        if identity is None:
            prepared.append((None, None, "invalid_side"))
            continue
        if len(modes[symbol]) > 1:
            prepared.append((None, None, "inconsistent_position_mode"))
            continue
        if counts[identity] > 1:
            prepared.append((None, None, "duplicate_position"))
            continue
        market = _market_for_id(client, symbol)
        if market is None:
            prepared.append((None, None, "unknown_market"))
            continue
        contract_size = _number(market.get("contractSize"))
        if contract_size is None or contract_size <= 0:
            market = {**market, "contractSize": None}
        margin_asset = row.get("marginAsset")
        settle_id = market.get("settleId")
        if margin_asset is not None and (
            not isinstance(margin_asset, str)
            or margin_asset != market["settle"] and (
                not isinstance(settle_id, str) or margin_asset != settle_id
            )
        ):
            prepared.append((None, None, "invalid_margin_currency"))
            continue
        amount = row.get("positionAmt")
        notional = row.get("notional")
        numeric_amount = _number(amount)
        if amount not in (None, "") and numeric_amount is None:
            prepared.append((None, None, "invalid_size"))
            continue
        if numeric_amount == 0 and notional not in (None, "") and _number(notional) is None:
            prepared.append((None, None, "invalid_notional"))
            continue
        mapped = {
            "symbol": market["symbol"],
            "positionAmt": amount,
            "positionSide": row["positionSide"],
            "info": {"marginAsset": market["settle"] if margin_asset else None},
        }
        for source, target in (
            ("entryPrice", "entryPrice"), ("markPrice", "markPrice"),
            ("liquidationPrice", "liquidationPrice"), ("unRealizedProfit", "unrealizedPnl"),
            ("notional", "notional"), ("leverage", "leverage"),
        ):
            if source in row:
                mapped[target] = _number(row[source])
        if "positionInitialMargin" in row:
            mapped["initialMargin"] = _number(row["positionInitialMargin"])
        margin_mode = row.get("marginType")
        if isinstance(margin_mode, str) and margin_mode in ("cross", "isolated"):
            mapped["marginMode"] = margin_mode
        prepared.append((mapped, market, None))
    return prepared
