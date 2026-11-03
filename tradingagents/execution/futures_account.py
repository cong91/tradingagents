"""Read-only futures account snapshots for the portfolio API."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse


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
    positions_scope_complete: bool = False

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


@dataclass(frozen=True)
class FuturesPositionsObservation:
    """Positions plus the symbols whose rows could not be interpreted safely."""

    positions: tuple[FuturesPosition, ...]
    unknown_symbols: frozenset[str]
    unknown_scope: bool
    scope_complete: bool = False


def _position_rows(bridge, client) -> list:
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    _, position_params = _futures_params(exchange_id, bridge.config)
    positions = client.fetch_positions(None, params=position_params)
    if not isinstance(positions, list):
        raise ValueError("exchange returned an invalid futures positions response")
    return positions


def _binance_position_rows(bridge, client) -> tuple[list, bool]:
    """Read raw V3 position-risk rows for Binance; errors propagate.

    The V3 endpoint returns every symbol that has an open position or open
    orders, so a response whose rows are all interpretable certifies the
    account-wide book. Returns one (mapped_row, raw_row, reason) entry per
    response row plus whether the bridge still serves the validated client.
    """
    from server import binance_futures_positions

    profile = _venue_profile(bridge)
    base_url = bridge.config.get("exec_base_url") or "https://demo-api.binance.com"
    binance_futures_positions.validate_client(client, profile, base_url)
    reader = binance_futures_positions.isolated_client(client)
    raw = reader.fapiPrivateV3GetPositionRisk({})
    if not isinstance(raw, list):
        raise ValueError("exchange returned an invalid futures positions response")
    if bridge.client() is not client:
        return [], False
    prepared = binance_futures_positions.prepare_rows(raw, reader)
    return [
        (mapped, raw_row, reason)
        for raw_row, (mapped, _, reason) in zip(raw, prepared, strict=True)
    ], True


def _observed_size(raw: dict, info: dict) -> float | None:
    values = []
    for source, keys in (
        (raw, ("contracts", "positionAmt", "position_amount", "amount", "size")),
        (info, ("positionAmt", "position_amount", "pos", "size")),
    ):
        for key in keys:
            value = source.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, str) and "_" in value:
                return None
            if isinstance(value, bool):
                return None
            try:
                exact = Decimal(str(value))
                number = float(exact)
            except (ValueError, TypeError, OverflowError, InvalidOperation):
                return None
            if not math.isfinite(number) or (number == 0 and exact != 0):
                return None
            values.append(number)
    if not values or any(
        not math.isclose(abs(value), abs(values[0]), rel_tol=1e-12, abs_tol=0.0)
        for value in values[1:]
    ):
        return None
    return values[0]


def _flat_notional_conflicts(raw: dict, info: dict) -> bool:
    for source in (raw, info):
        for key in ("notional", "notionalUsd", "notionalValue", "positionValue"):
            value = source.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, bool) or isinstance(value, str) and "_" in value:
                return True
            try:
                number = Decimal(str(value))
            except (ValueError, TypeError, InvalidOperation):
                return True
            if not number.is_finite() or number != 0:
                return True
    return False


def observe_futures_positions(bridge) -> FuturesPositionsObservation:
    """Read position evidence without treating absent or malformed rows as flat."""
    client = bridge.client()
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    unknown_symbols: set[str] = set()
    unknown_scope = False
    scope_complete = False
    if exchange_id == "binance":
        entries, scope_complete = _binance_position_rows(bridge, client)
        if not scope_complete:
            # The bridge rebound its client during the read; the validated
            # route no longer describes the client the rest of the run uses.
            unknown_scope = True
    else:
        rows = _position_rows(bridge, client)
        entries = [(row, row, None) for row in rows]
    positions = []
    seen: set[tuple[str, str]] = set()
    for raw, raw_row, reason in entries:
        if reason is not None:
            symbol = raw_row.get("symbol") if isinstance(raw_row, dict) else None
            if isinstance(symbol, str) and symbol.strip():
                unknown_symbols.add(symbol)
            else:
                unknown_scope = True
            continue
        symbol = raw.get("symbol") if isinstance(raw, dict) else None
        if not isinstance(symbol, str) or not symbol.strip():
            unknown_scope = True
            continue
        if raw.get("info") is not None and not isinstance(raw.get("info"), dict):
            unknown_symbols.add(symbol)
            continue
        raw_info = raw.get("info") or {}
        contracts = _observed_size(raw, raw_info)
        raw_sides = [str(raw[key]).lower() for key in ("side", "positionSide", "position_side")
                     if raw.get(key) not in (None, "")]
        if contracts is None or any(side not in {"long", "short", "both"} for side in raw_sides):
            unknown_symbols.add(symbol)
            continue
        directions = {side for side in raw_sides if side in {"long", "short"}}
        if len(directions) > 1:
            unknown_symbols.add(symbol)
            continue
        side = next(iter(directions), "")
        info_symbol = raw_info.get("symbol")
        if (
            str(getattr(bridge, "exchange_id", "")).lower() == "binance"
            and info_symbol and str(info_symbol).upper() != symbol.split(":", 1)[0].replace("/", "").upper()
        ):
            # Either symbol could be the row's real identity.
            unknown_scope = True
            continue
        info_side = str(raw_info.get("positionSide") or "").lower()
        if info_side not in {"", "both", "long", "short"}:
            unknown_symbols.add(symbol)
            continue
        if info_side in {"long", "short"} and side in {"long", "short"} and info_side != side:
            unknown_symbols.add(symbol)
            continue
        resolved_side = side if side in {"long", "short"} else info_side
        if resolved_side == "long" and contracts < 0:
            unknown_symbols.add(symbol)
            continue
        signed_values = [
            _number(source[key])
            for source, keys in (
                (raw, ("positionAmt", "position_amount")),
                (raw_info, ("positionAmt", "position_amount", "pos")),
            )
            for key in keys if source.get(key) not in (None, "")
        ]
        negative_size = any(
            _number(source[key]) < 0
            for source, keys in (
                (raw, ("contracts", "amount", "size")),
                (raw_info, ("size",)),
            )
            for key in keys if source.get(key) not in (None, "")
        )
        if any(
            signed is None
            or (resolved_side == "long" and signed < 0)
            or (resolved_side == "short" and signed > 0)
            for signed in signed_values
        ) or (
            any(signed is not None and signed > 0 for signed in signed_values)
            and any(signed is not None and signed < 0 for signed in signed_values)
        ) or (
            negative_size and (
                resolved_side == "long"
                or any(signed is not None and signed > 0 for signed in signed_values)
            )
        ):
            unknown_symbols.add(symbol)
            continue
        if resolved_side not in {"long", "short"}:
            signed = next((value for value in signed_values if value), 0)
            if not signed and negative_size:
                signed = -abs(contracts)
            resolved_side = "long" if signed > 0 else "short" if signed < 0 else ""
            if not resolved_side and contracts != 0:
                unknown_symbols.add(symbol)
                continue
        if contracts == 0:
            if _flat_notional_conflicts(raw, raw_info):
                unknown_symbols.add(symbol)
            # A flat row contributes no position; a contradictory notional
            # above keeps the row and the account scope unknown.
            continue
        values = [
            value for key in ("contracts", "positionAmt", "position_amount", "amount", "size")
            if (value := _number(raw.get(key))) is not None
        ]
        if not values:
            raw = {**raw, "contracts": contracts}
        position = _normalize_position({**raw, "side": resolved_side}, client)
        if position is None or position.side not in {"long", "short"}:
            unknown_symbols.add(symbol)
            continue
        key = (symbol, position.side)
        if key in seen:
            unknown_symbols.add(symbol)
        seen.add(key)
        positions.append(position)
    return FuturesPositionsObservation(
        tuple(positions), frozenset(unknown_symbols), unknown_scope,
        scope_complete and not unknown_symbols and not unknown_scope,
    )


def read_futures_balance(bridge) -> FuturesAccountSnapshot:
    """Read account totals without making a second, potentially inconsistent position read."""
    client = bridge.client()
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    balance_params, _ = _futures_params(exchange_id, bridge.config)
    balance = client.fetch_balance(params=balance_params)
    if not isinstance(balance, dict):
        raise ValueError("exchange returned an invalid futures account response")
    return FuturesAccountSnapshot(**_account_values(balance, exchange_id), venue_profile=_venue_profile(bridge))


def read_futures_positions(bridge) -> tuple[FuturesPosition, ...]:
    """Read positions only when every returned row is interpretable."""
    observation = observe_futures_positions(bridge)
    if observation.unknown_scope or observation.unknown_symbols:
        raise ValueError("exchange returned malformed futures position rows")
    return observation.positions


def read_futures_account(bridge) -> FuturesAccountSnapshot:
    """Read the configured futures account without changing client mode or state.

    Venue-specific params keep CCXT on derivatives endpoints while preserving
    the venue's current client configuration.
    """
    snapshot = read_futures_balance(bridge)
    observation = observe_futures_positions(bridge)
    if observation.unknown_scope or observation.unknown_symbols:
        raise ValueError("exchange returned malformed futures position rows")
    return replace(
        snapshot, positions=observation.positions,
        positions_scope_complete=observation.scope_complete,
    )
