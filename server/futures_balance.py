"""Validate and sanitize read-only futures account balance responses."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

_BINANCE_PORTFOLIO_BALANCE_KEYS = (
    "umWalletBalance", "umUnrealizedPNL", "cmWalletBalance", "cmUnrealizedPNL",
    "crossMarginAsset", "crossMarginFree", "crossMarginLocked", "crossMarginBorrowed",
    "crossMarginInterest", "totalWalletBalance", "negativeBalance",
)
_BALANCE_NUMBERS = frozenset({
    "equity", "totalEquity", "totalMarginBalance", "walletBalance", "totalWalletBalance",
    "availableBalance", "unrealizedPnl", "totalUnrealizedProfit", "totalEq", "upl",
    "cashBal", "eq", "availEq", "availBal", "availableToWithdraw", "free",
    "unrealisedPnl", "totalAvailableBalance", "totalPerpUPL",
})


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    if number == 0:
        try:
            if Decimal(str(value)) != 0:
                return None
        except InvalidOperation:
            return None
    return number


def sanitize_balance(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key in _BALANCE_NUMBERS and not isinstance(item, dict):
                cleaned[key] = _finite_number(item)
            elif (
                key in {"ccy", "asset", "currency", "accountType"} and not isinstance(item, str)
                or key == "coin" and not isinstance(item, (str, list))
            ):
                cleaned[key] = None
            else:
                cleaned[key] = sanitize_balance(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_balance(item) for item in value]
    return value


def validate_balance(balance: Any, exchange_id: str) -> None:
    invalid = ValueError("exchange returned an invalid futures account response")
    if not isinstance(balance, dict) or not balance:
        raise invalid
    info = balance.get("info")
    portfolio_rows = None
    if isinstance(info, list) and exchange_id == "binance":
        portfolio_rows = info
        if not portfolio_rows or any(
            not isinstance(row, dict)
            or not isinstance(row.get("asset"), str)
            or not row["asset"].strip()
            or not any(_finite_number(row.get(key)) is not None for key in _BINANCE_PORTFOLIO_BALANCE_KEYS)
            for row in portfolio_rows
        ):
            raise invalid
        info = {}
    elif "info" in balance and not isinstance(info, dict):
        raise invalid
    info = info or {}
    for key in ("free", "used", "total"):
        if key in balance and balance[key] is not None and not isinstance(balance[key], dict):
            raise invalid
    if exchange_id == "okx" and "data" in info:
        rows = info["data"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise invalid
        for row in rows:
            details = row.get("details")
            if details is not None and (
                not isinstance(details, list) or any(not isinstance(item, dict) for item in details)
            ):
                raise invalid
    if exchange_id == "bybit" and "result" in info:
        result = info["result"]
        if not isinstance(result, dict):
            raise invalid
        rows = result.get("list")
        if rows is not None:
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise invalid
            for row in rows:
                coins = row.get("coin")
                if coins is not None and (
                    not isinstance(coins, list) or any(not isinstance(item, dict) for item in coins)
                ):
                    raise invalid

    # Account totals can legitimately be unknown (for example, mixed coins).
    # Require at least one recognizable balance/account envelope nonetheless.
    def has_account_data(data: dict) -> bool:
        return any(_finite_number(data.get(key)) is not None for key in _BALANCE_NUMBERS)

    recognized = portfolio_rows is not None or has_account_data(balance) or has_account_data(info)
    recognized |= any(
        isinstance(balance.get(key), dict)
        and any(_finite_number(value) is not None for value in balance[key].values())
        for key in ("free", "used", "total")
    )
    if exchange_id == "okx":
        rows = info.get("data")
        recognized |= isinstance(rows, list) and any(has_account_data(row) for row in rows)
    if exchange_id == "bybit":
        result = info.get("result") or {}
        rows = result.get("list") or []
        recognized |= any(
            isinstance(row.get("accountType"), str)
            and row["accountType"].strip().upper() in {"UNIFIED", "CONTRACT"}
            for row in rows
        )
    if not recognized:
        raise invalid
