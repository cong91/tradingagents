"""Read-only futures account snapshots for the portfolio API."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FuturesPosition:
    """Normalized venue position; fields absent from the venue remain null."""

    symbol: str | None = None
    side: str | None = None
    contracts: float | None = None
    quantity: float | None = None
    entry_price: float | None = None
    mark_price: float | None = None
    liquidation_price: float | None = None
    unrealized_pnl: float | None = None
    notional: float | None = None
    margin: float | None = None
    leverage: float | None = None
    margin_mode: str | None = None
    price_currency: str | None = None
    notional_currency: str | None = None
    margin_currency: str | None = None
    unrealized_pnl_currency: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FuturesAccountSnapshot:
    """Normalized, read-only futures account response."""

    equity: float | None = None
    wallet_balance: float | None = None
    available_balance: float | None = None
    unrealized_pnl: float | None = None
    venue_profile: str | None = None
    equity_currency: str | None = None
    wallet_balance_currency: str | None = None
    available_balance_currency: str | None = None
    unrealized_pnl_currency: str | None = None
    account_scope: str = "derivatives"
    positions: tuple[FuturesPosition, ...] = ()
    status: str = "ok"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "equity": self.equity,
            "wallet_balance": self.wallet_balance,
            "available_balance": self.available_balance,
            "unrealized_pnl": self.unrealized_pnl,
            "venue_profile": self.venue_profile,
            "equity_currency": self.equity_currency,
            "wallet_balance_currency": self.wallet_balance_currency,
            "available_balance_currency": self.available_balance_currency,
            "unrealized_pnl_currency": self.unrealized_pnl_currency,
            "account_scope": self.account_scope,
            "positions": [position.as_dict() for position in self.positions],
            "status": self.status,
            "error": self.error,
        }


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in data:
            value = _number(data[key])
            if value is not None:
                return value
    return None


def _normalize_position(raw: Any, client: Any) -> FuturesPosition | None:
    if not isinstance(raw, dict):
        return None
    contracts = _first_number(raw, "contracts", "positionAmt", "position_amount", "amount", "size")
    side = str(raw.get("side") or raw.get("positionSide") or raw.get("position_side") or "").lower()
    if side not in {"long", "short"}:
        side = "long" if contracts is not None and contracts > 0 else "short" if contracts is not None and contracts < 0 else "unknown"
    if contracts is None or contracts == 0:
        return None
    signed_contracts = abs(contracts) * (1 if side == "long" else -1)
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    market = {}
    market_for = getattr(client, "market", None)
    if callable(market_for) and raw.get("symbol"):
        try:
            value = market_for(raw["symbol"])
            if isinstance(value, dict):
                market = value
        except Exception:
            pass
    quote_currency = market.get("quote")
    settlement_currency = info.get("marginAsset") or info.get("ccy") or market.get("settle")
    contract_size = _number(market.get("contractSize")) or 1.0
    entry_price = _first_number(raw, "entryPrice", "entry_price", "average", "avgPrice")
    mark_price = _first_number(raw, "markPrice", "mark_price", "mark")
    if market.get("inverse") is True:
        signed_quantity = signed_contracts * contract_size / entry_price if entry_price else None
    else:
        signed_quantity = signed_contracts * contract_size
    notional_currency = (
        "USD" if info.get("notionalUsd") is not None
        else market.get("settle") if market.get("inverse") is True
        else quote_currency or settlement_currency
    )
    margin = _first_number(raw, "initialMargin", "initial_margin", "margin", "collateral")
    if margin is None:
        margin = _first_number(info, "initialMargin", "isolatedMargin", "positionInitialMargin")
    notional = _first_number(raw, "notional")
    if notional is not None:
        notional = abs(notional)
    return FuturesPosition(
        symbol=raw.get("symbol"),
        side=side,
        contracts=abs(contracts),
        quantity=signed_quantity,
        entry_price=entry_price,
        mark_price=mark_price,
        liquidation_price=_first_number(raw, "liquidationPrice", "liquidation_price"),
        unrealized_pnl=_first_number(raw, "unrealizedPnl", "unrealized_pnl", "unrealizedProfit"),
        notional=notional,
        margin=margin,
        leverage=_first_number(raw, "leverage"),
        margin_mode=raw.get("marginMode") or raw.get("margin_mode"),
        price_currency=quote_currency,
        notional_currency=notional_currency,
        margin_currency=settlement_currency,
        unrealized_pnl_currency=settlement_currency,
    )


def _account_value(balance: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _first_number(balance, *keys)
    if value is not None:
        return value
    info = balance.get("info") if isinstance(balance.get("info"), dict) else {}
    return _first_number(info, *keys)


def _venue_profile(bridge: Any) -> str | None:
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    if exchange_id == "binance":
        base_url = str(bridge.config.get("exec_base_url") or "https://demo-api.binance.com")
        host = urlparse(base_url).hostname
        if host in {"demo-api.binance.com", "demo-fapi.binance.com"}:
            return "demo"
        if host in {"api.binance.com", "fapi.binance.com"}:
            return "production"
    return None


def _account_records(balance: dict[str, Any]) -> list[dict[str, Any]]:
    info = balance.get("info") if isinstance(balance.get("info"), dict) else {}
    if isinstance(info.get("data"), list):
        return [row for row in info["data"] if isinstance(row, dict)]
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    if isinstance(result.get("list"), list):
        return [row for row in result["list"] if isinstance(row, dict)]
    return [info] if info else []


def _account_values(balance: dict[str, Any], exchange_id: str) -> dict[str, Any]:
    records = _account_records(balance)
    if exchange_id == "okx":
        record = next((item for item in records if _first_number(item, "totalEq") is not None), {})
        details = record.get("details") if isinstance(record.get("details"), list) else []
        currencies = {item.get("ccy") for item in details if isinstance(item, dict) and item.get("ccy")}
        quote = details[0] if len(currencies) == 1 and details and isinstance(details[0], dict) else {}
        return {
            "equity": _first_number(record, "totalEq"),
            "wallet_balance": _first_number(quote, "cashBal", "eq"),
            "available_balance": _first_number(quote, "availEq", "availBal"),
            "unrealized_pnl": _first_number(record, "upl"),
            "equity_currency": "USD" if record else None,
            "wallet_balance_currency": quote.get("ccy"),
            "available_balance_currency": quote.get("ccy"),
            "unrealized_pnl_currency": "USD" if _first_number(record, "upl") is not None else None,
            "account_scope": "trading",
        }
    if exchange_id == "bybit":
        account_types = {
            str(item["accountType"]).strip().upper()
            for item in records
            if item.get("accountType")
        }
        record = records[0] if len(account_types) == 1 and len(records) == 1 else {}
        account_type = next(iter(account_types)) if len(account_types) == 1 else ""
        if account_type == "CONTRACT":
            coins = record.get("coin") if isinstance(record.get("coin"), list) else []
            coin = coins[0] if len(coins) == 1 and isinstance(coins[0], dict) else {}
            currency = coin.get("coin")
            values = {
                "equity": _first_number(coin, "equity"),
                "wallet_balance": _first_number(coin, "walletBalance"),
                "available_balance": _first_number(
                    coin, "availableToWithdraw", "availableBalance", "free"
                ),
                "unrealized_pnl": _first_number(coin, "unrealisedPnl", "unrealizedPnl"),
            }
            return {
                **values,
                "equity_currency": currency if values["equity"] is not None else None,
                "wallet_balance_currency": currency if values["wallet_balance"] is not None else None,
                "available_balance_currency": currency if values["available_balance"] is not None else None,
                "unrealized_pnl_currency": currency if values["unrealized_pnl"] is not None else None,
                "account_scope": "derivatives",
            }

        unified = account_type == "UNIFIED"
        if not unified:
            return {
                "equity": None,
                "wallet_balance": None,
                "available_balance": None,
                "unrealized_pnl": None,
                "equity_currency": None,
                "wallet_balance_currency": None,
                "available_balance_currency": None,
                "unrealized_pnl_currency": None,
                "account_scope": "derivatives",
            }
        return {
            "equity": _first_number(record, "totalEquity", "totalMarginBalance"),
            "wallet_balance": _first_number(record, "totalWalletBalance"),
            "available_balance": _first_number(record, "totalAvailableBalance"),
            "unrealized_pnl": _first_number(record, "totalPerpUPL"),
            "equity_currency": "USD",
            "wallet_balance_currency": "USD",
            "available_balance_currency": "USD",
            "unrealized_pnl_currency": "USD",
            "account_scope": "unified",
        }
    info = balance.get("info") if isinstance(balance.get("info"), dict) else {}
    return {
        "equity": _account_value(balance, ("equity", "totalEquity", "totalMarginBalance")),
        "wallet_balance": _account_value(balance, ("walletBalance", "totalWalletBalance")),
        "available_balance": _account_value(balance, ("availableBalance",)),
        "unrealized_pnl": _account_value(balance, ("unrealizedPnl", "totalUnrealizedProfit")),
        "equity_currency": info.get("asset") or info.get("currency"),
        "wallet_balance_currency": info.get("asset") or info.get("currency"),
        "available_balance_currency": info.get("asset") or info.get("currency"),
        "unrealized_pnl_currency": info.get("asset") or info.get("currency"),
        "account_scope": "derivatives",
    }


def _futures_params(exchange_id: str, config: dict) -> tuple[dict[str, str], dict[str, str]]:
    if exchange_id == "binance":
        params = {"type": "future", "subType": "linear"}
        return params, params.copy()
    if exchange_id == "bybit":
        quote = str(config.get("exec_quote_currency") or "USDT").strip().upper()
        return {"type": "swap", "subType": "linear"}, {
            "type": "swap", "subType": "linear", "settleCoin": quote,
        }
    if exchange_id == "okx":
        return {"type": "trading"}, {"instType": "SWAP"}
    raise ValueError(f"futures account reads are not configured for {exchange_id!r}")


def read_futures_positions(bridge) -> tuple[FuturesPosition, ...]:
    """Read and normalize only the configured derivatives positions."""
    client = bridge.client()
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    _, position_params = _futures_params(exchange_id, bridge.config)
    positions = client.fetch_positions(None, params=position_params)
    if not isinstance(positions, list):
        raise ValueError("exchange returned an invalid futures positions response")
    return tuple(
        position for raw in positions if (position := _normalize_position(raw, client)) is not None
    )


def _binance_multi_assets(client: Any) -> bool:
    """True when the Binance USD-M account pools margin across assets.

    An unreadable mode answers False: the per-asset reading is the smaller,
    conservative equity, so a failed probe can only make sizing stricter.
    """
    try:
        response = client.fapiPrivateGetMultiAssetsMargin()
    except Exception as exc:  # noqa: BLE001 - mode probe must never block a read
        logger.warning("futures account: multi-assets mode unavailable (%r)", exc)
        return False
    return isinstance(response, dict) and response.get("multiAssetsMargin") in (True, "true")


def _binance_settle_values(balance: dict[str, Any], settle: str) -> dict[str, Any] | None:
    """Binance single-asset mode: the settle asset's own row is its margin book.

    The account totals only count USDT there, so a USDC contract must be sized
    against the USDC row (marginBalance/walletBalance/availableBalance).
    """
    info = balance.get("info") if isinstance(balance.get("info"), dict) else {}
    assets = info.get("assets") if isinstance(info.get("assets"), list) else []
    row = next(
        (item for item in assets
         if isinstance(item, dict) and str(item.get("asset") or "").upper() == settle),
        None,
    )
    if row is None:
        return None
    return {
        "equity": _first_number(row, "marginBalance"),
        "wallet_balance": _first_number(row, "walletBalance"),
        "available_balance": _first_number(row, "availableBalance"),
        "unrealized_pnl": _first_number(row, "unrealizedProfit"),
        "equity_currency": settle,
        "wallet_balance_currency": settle,
        "available_balance_currency": settle,
        "unrealized_pnl_currency": settle,
        "account_scope": "derivatives",
    }


def read_futures_settle_balances(bridge) -> dict[str, float]:
    """Non-zero futures wallet balances by asset (e.g. {"USDT": 5000, "USDC": 5000})."""
    client = bridge.client()
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    balance_params, _ = _futures_params(exchange_id, bridge.config)
    balance = client.fetch_balance(params=balance_params)
    if not isinstance(balance, dict):
        raise ValueError("exchange returned an invalid futures account response")
    totals = balance.get("total") if isinstance(balance.get("total"), dict) else {}
    out: dict[str, float] = {}
    for asset, amount in totals.items():
        value = _number(amount)
        if value is not None and value > 0:
            out[str(asset).upper()] = value
    return out


def read_futures_account(bridge, settle: str | None = None) -> FuturesAccountSnapshot:
    """Read the configured futures account without changing client mode or state.

    Venue-specific params keep CCXT on derivatives endpoints while preserving
    the venue's current client configuration. ``settle`` (e.g. "USDC") scopes
    a Binance single-asset-mode account to that asset's margin book when it
    differs from ``exec_quote_currency``; otherwise, and in multi-assets mode,
    the account totals are used.
    """
    client = bridge.client()
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    profile = _venue_profile(bridge)
    balance_params, _ = _futures_params(exchange_id, bridge.config)

    balance = client.fetch_balance(params=balance_params)
    if not isinstance(balance, dict):
        raise ValueError("exchange returned an invalid futures account response")
    normalized = read_futures_positions(bridge)
    values = None
    settle = settle.strip().upper() if settle else None
    quote = str(bridge.config.get("exec_quote_currency") or "USDT").strip().upper()
    # The account totals already are the quote asset's book in single-asset
    # mode; only another settle asset (USDC) needs its own row.
    if settle and settle != quote and exchange_id == "binance" and not _binance_multi_assets(client):
        values = _binance_settle_values(balance, settle)
        if values is None:
            raise ValueError(f"futures wallet has no {settle} balance row")
    if values is None:
        values = _account_values(balance, exchange_id)
    return FuturesAccountSnapshot(
        equity=values["equity"],
        wallet_balance=values["wallet_balance"],
        available_balance=values["available_balance"],
        unrealized_pnl=values["unrealized_pnl"],
        venue_profile=profile,
        equity_currency=values["equity_currency"],
        wallet_balance_currency=values["wallet_balance_currency"],
        available_balance_currency=values["available_balance_currency"],
        unrealized_pnl_currency=values["unrealized_pnl_currency"],
        account_scope=values["account_scope"],
        positions=normalized,
    )
