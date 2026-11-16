"""Credential-free public Futures market-data clients for evaluation."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _BybitTargetExchange:
    """Bound the shared candle adapter to one historical Bybit candle."""

    def __init__(self, exchange: Any, target_open: int) -> None:
        self._exchange = exchange
        self._target_open = target_open

    def __getattr__(self, name: str) -> Any:
        return getattr(self._exchange, name)

    def fetch_ohlcv(self, symbol: str, *, timeframe: str, since: int, limit: int) -> Any:
        if since != self._target_open or limit != 1:
            raise ValueError("invalid Bybit target candle bounds")
        try:
            return self._exchange.fetch_ohlcv(
                symbol, timeframe=timeframe, since=since, limit=limit,
                params={"end": self._target_open},
            )
        except TypeError as exc:
            # The shared adapter retries TypeError without since; never drop bounds.
            raise ValueError("Bybit bounded candle request is unsupported") from exc


def target_candle_exchange(exchange: Any, target_open: int) -> Any:
    if getattr(exchange, "id", None) == "bybit":
        return _BybitTargetExchange(exchange, target_open)
    return exchange


def close_client(exchange: Any) -> None:
    close = getattr(exchange, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("failed to close public forecast evaluation client", exc_info=True)


def public_futures_exchange(scope: dict[str, Any]) -> Any:
    """Construct a public-only client matching the recorded Futures venue."""
    import ccxt

    exchange_id = str(scope.get("exchange_id") or "").strip().lower()
    if not exchange_id:
        raise ValueError("forecast venue scope is missing exchange_id")
    exchange_class = getattr(ccxt, exchange_id, None)
    if not (isinstance(exchange_class, type) and issubclass(exchange_class, ccxt.Exchange)):
        raise ValueError(f"forecast venue {exchange_id!r} is unsupported")

    options = {
        "enableRateLimit": True,
        "options": {"defaultType": "future" if exchange_id == "binance" else "swap"},
    }
    exchange = exchange_class(options)
    try:
        if exchange_id == "binance":
            from tradingagents.execution.exchange_client import _binance_profile

            profile = scope.get("venue_profile")
            if profile is None:
                profile = _binance_profile(str(scope.get("base_url") or ""))
            elif profile not in {"demo", "production"}:
                raise ValueError(f"invalid recorded Binance venue profile: {profile!r}")
            if profile == "demo":
                enable_demo = getattr(exchange, "enable_demo_trading", None)
                if not callable(enable_demo):
                    raise RuntimeError("ccxt Binance client does not support Demo Trading")
                enable_demo(True)
        elif scope.get("sandbox") is True:
            exchange.set_sandbox_mode(True)
        if any(getattr(exchange, field, None) for field in ("apiKey", "secret", "password", "uid")):
            raise RuntimeError("forecast evaluation requires a credential-free public client")
        exchange.load_markets()
        return exchange
    except BaseException:
        close_client(exchange)
        raise


__all__ = ["close_client", "public_futures_exchange"]
