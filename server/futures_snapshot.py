"""Read-only portfolio futures snapshots with position-read diagnostics."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from server import binance_futures_positions, futures_balance, performance_history_cache
from tradingagents.execution import futures_account

_SIZE_KEYS = ("contracts", "positionAmt", "position_amount", "amount", "size")
_INFO_SIZE_KEYS = ("positionAmt", "position_amount", "pos", "size")
_SIDE_KEYS = ("side", "positionSide", "position_side")
_INFO_SIDE_KEYS = ("side", "positionSide", "position_side", "posSide")
_NOTIONAL_KEYS = ("notional", "notionalUsd", "notionalValue", "positionValue")
_POSITION_NUMBERS = (
    "entryPrice", "entry_price", "average", "avgPrice", "markPrice", "mark_price", "mark",
    "liquidationPrice", "liquidation_price", "unrealizedPnl", "unrealized_pnl",
    "unrealizedProfit", "notional", "initialMargin", "initial_margin", "margin",
    "collateral", "leverage",
)


class _PositionCooldownError(RuntimeError):
    """A concurrent rate limit prevents a new position read."""


def _monitor_coverage_issues(
    monitor_states: Callable[[], list],
    exchange_id: str,
    profile: str,
    venue_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Positions the monitor manages but the venue snapshot does not show.

    Only states bound to this exchange and venue profile are attributable to
    the snapshot's account; unbound legacy rows and other books are skipped.
    A state whose key cannot be parsed is the protection poll's diagnostic,
    not the display snapshot's.
    """
    from tradingagents.execution.futures_protection import _position_identity

    try:
        states = monitor_states()
    except Exception:
        return [{"symbol": None, "reason": "monitor_state_unreadable"}]
    if not isinstance(states, list):
        return [{"symbol": None, "reason": "monitor_state_unreadable"}]
    missing: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for state in states:
        if not isinstance(state, dict):
            continue
        if (
            state.get("account_identity_exchange") != exchange_id
            or state.get("account_identity_profile") != profile
        ):
            continue
        try:
            key = _position_identity(
                str(state.get("position_key") or ""), state.get("snapshot") or {},
            )
        except (KeyError, TypeError, ValueError):
            continue
        if key not in venue_keys and key not in seen:
            seen.add(key)
            missing.append({"symbol": key[0], "reason": "monitor_position_missing"})
    return missing


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


def _position_size(raw: dict, info: dict) -> tuple[float | None, list[tuple[str, float]], str | None]:
    values = []
    directional = []
    for data, keys in ((raw, _SIZE_KEYS), (info, _INFO_SIZE_KEYS)):
        for key in keys:
            if key not in data or data[key] is None or data[key] == "":
                continue
            number = _finite_number(data[key])
            if number is None:
                return None, [], "invalid_size"
            values.append(number)
            if key in {"positionAmt", "position_amount", "pos"} or number < 0:
                directional.append((key, number))
    if not values:
        return None, [], "missing_size"
    if any(not math.isclose(abs(number), abs(values[0]), rel_tol=1e-12, abs_tol=0.0) for number in values[1:]):
        return None, [], "inconsistent_size"
    return values[0], directional, None


def _position_side(
    raw: dict, info: dict, size: float, directional: list[tuple[str, float]], exchange_id: str,
) -> tuple[str | None, str | None]:
    sides = []
    net = False
    hedged = raw.get("hedged")
    if hedged is not None and not isinstance(hedged, bool):
        return None, "invalid_side"
    hedge = hedged is True
    for data, keys in ((raw, _SIDE_KEYS), (info, _INFO_SIDE_KEYS)):
        for key in keys:
            value = data.get(key)
            if value is None or value == "":
                continue
            if not isinstance(value, str):
                return None, "invalid_side"
            value = value.strip().lower()
            if value in {"long", "buy"}:
                sides.append("long")
            elif value in {"short", "sell"}:
                sides.append("short")
            elif value in {"both", "net"}:
                net = True
            else:
                return None, "invalid_side"
            if key in {"positionSide", "position_side", "posSide"} and value in {"long", "short"}:
                hedge = True

    index = info.get("positionIdx")
    if index is not None and index != "":
        if isinstance(index, bool) or str(index) not in {"0", "1", "2"}:
            return None, "invalid_side"
        if str(index) == "0":
            net = True
        else:
            hedge = True
            sides.append("long" if str(index) == "1" else "short")
    if raw.get("hedged") is False:
        net = True
    if net and hedge:
        return None, "inconsistent_side"
    if sides and any(side != sides[0] for side in sides[1:]):
        return None, "inconsistent_side"
    side = sides[0] if sides else None
    inferred_side = None
    for key, number in directional:
        if number == 0:
            continue
        inferred = "long" if number > 0 else "short"
        # Binance positionAmt is signed in hedge mode; other venues may use magnitudes.
        signed_binance_amount = exchange_id == "binance" and key in {"positionAmt", "position_amount"}
        if net or not hedge or number < 0 or signed_binance_amount:
            if inferred_side is not None and inferred_side != inferred:
                return None, "inconsistent_side"
            inferred_side = inferred
            if side is None:
                side = inferred
            elif side != inferred:
                return None, "inconsistent_side"
    if size < 0:
        if side == "long":
            return None, "inconsistent_side"
        side = "short"
    if side is None and size != 0:
        return None, "missing_side"
    return side, None


def _flat_notional_issue(raw: dict, info: dict) -> str | None:
    for data in (raw, info):
        for key in _NOTIONAL_KEYS:
            value = data.get(key)
            if value is None or value == "":
                continue
            number = _finite_number(value)
            if number is None:
                return "invalid_notional"
            if number != 0:
                return "inconsistent_notional"
    return None


def _market(client: Any, symbol: str) -> dict:
    market_for = getattr(client, "market", None)
    if not callable(market_for):
        return {}
    try:
        market = market_for(symbol)
    except Exception:
        return {}
    return market if isinstance(market, dict) else {}


def _normalize(
    raw: dict, info: dict, size: float, side: str, client: Any, market: dict | None = None,
) -> dict:
    if market is None:
        market = _market(client, raw["symbol"])
    clean_market = dict(market)
    for key in ("quote", "settle"):
        if not isinstance(clean_market.get(key), str) or not clean_market[key].strip():
            clean_market[key] = None
    contract_size = _finite_number(market.get("contractSize"))
    raw_contract_size = raw.get("contractSize")
    if raw_contract_size is not None and raw_contract_size != "":
        reported_size = _finite_number(raw_contract_size)
        if reported_size is None or reported_size <= 0:
            contract_size = None
        elif contract_size is None and market.get("contractSize") is None:
            contract_size = reported_size
        elif contract_size is None or not math.isclose(
            reported_size, contract_size, rel_tol=1e-12, abs_tol=0.0
        ):
            contract_size = None
    inverse = market.get("inverse")
    linear = market.get("linear")
    usable_market = (
        contract_size is not None and contract_size > 0
        and isinstance(inverse, bool)
        and (linear is None or isinstance(linear, bool) and linear is not inverse)
    )
    clean_market["contractSize"] = contract_size if usable_market else None
    clean_raw = dict(raw)
    clean_raw.update({"contracts": abs(size), "side": side})
    for key in _POSITION_NUMBERS:
        if key in clean_raw:
            clean_raw[key] = _finite_number(clean_raw[key])
    for key in ("marginMode", "margin_mode"):
        if key in clean_raw and not isinstance(clean_raw[key], str):
            clean_raw[key] = None
    clean_info = dict(info)
    for key in ("marginAsset", "ccy"):
        if not isinstance(clean_info.get(key), str) or not clean_info[key].strip():
            clean_info[key] = None
    for key in ("initialMargin", "isolatedMargin", "positionInitialMargin", "notionalUsd"):
        if key in clean_info:
            clean_info[key] = _finite_number(clean_info[key])
    clean_raw["info"] = clean_info

    class _Metadata:
        def market(self, symbol):
            return clean_market

    position = futures_account._normalize_position(clean_raw, _Metadata())
    quantity = position.quantity if usable_market else None
    if inverse is True and (position.entry_price is None or position.entry_price <= 0):
        quantity = None
    if quantity is not None and not math.isfinite(quantity):
        quantity = None
    notional_currency = position.notional_currency
    if inverse is not True and inverse is not False and clean_info.get("notionalUsd") is None:
        notional_currency = None
    return replace(position, quantity=quantity, notional_currency=notional_currency).as_dict()


def read_futures_snapshot(
    bridge: Any, *, monitor_states: Callable[[], list] | None = None,
) -> dict[str, Any]:
    """Read one portfolio account snapshot with sanitized position diagnostics.

    The account fields match ``FuturesAccountSnapshot.as_dict()``. The added
    ``positions_read`` reports whether the venue position list could be proven
    complete. A Binance raw V3 read whose every row is interpretable and whose
    client survived the read unchanged certifies the account-wide book — the
    same contract the protection monitor settles state deletion on — and when
    ``monitor_states`` is supplied the certified list must also cover every
    monitored position bound to this venue profile; a missing one surfaces as
    a ``monitor_position_missing`` issue instead of a claimed-complete read.
    Uncertified venues and uncertified reads stay ``partial`` with
    ``scope_unverified``. It contains a skipped-row count, sanitized issues,
    and the UTC observation time after the positions call returns. Invalid
    balances and unsupported venues raise.
    """
    exchange_id = str(getattr(bridge, "exchange_id", "")).lower()
    balance_params, position_params = futures_account._futures_params(exchange_id, bridge.config)
    profile = futures_account._venue_profile(bridge)
    if performance_history_cache.is_rate_limited():
        raise RuntimeError("futures read deferred during rate limit cooldown")
    client = bridge.client()
    venue_client = client
    if exchange_id == "binance":
        binance_futures_positions.validate_client(
            client, profile, bridge.config.get("exec_base_url") or "https://demo-api.binance.com",
        )
        client = binance_futures_positions.isolated_client(client)
    if performance_history_cache.is_rate_limited():
        raise RuntimeError("futures read deferred during rate limit cooldown")
    balance = client.fetch_balance(params=balance_params)
    if exchange_id == "binance":
        binance_futures_positions.validate_client(
            client, profile, bridge.config.get("exec_base_url") or "https://demo-api.binance.com",
        )
    futures_balance.validate_balance(balance, exchange_id)
    if exchange_id == "binance" and not isinstance(balance.get("info"), list):
        balance_info = balance.get("info")
        if not isinstance(balance_info, dict) or not isinstance(balance_info.get("positions"), list):
            raise ValueError("Binance futures account type is not verified")
    if exchange_id == "binance" and isinstance(balance.get("info"), list):
        values = futures_account._account_values({}, exchange_id, bridge.config)
    else:
        values = futures_account._account_values(
            futures_balance.sanitize_balance(balance), exchange_id, bridge.config
        )
    for key in (
        "equity_currency", "wallet_balance_currency", "available_balance_currency",
        "unrealized_pnl_currency",
    ):
        if not isinstance(values[key], str) or not values[key].strip():
            values[key] = None
    try:
        if performance_history_cache.is_rate_limited():
            raise _PositionCooldownError
        if exchange_id == "binance":
            if isinstance(balance.get("info"), list):
                raise binance_futures_positions.BinanceAccountTypeError
            binance_futures_positions.validate_client(
                client, profile, bridge.config.get("exec_base_url") or "https://demo-api.binance.com",
            )
            rows = client.fapiPrivateV3GetPositionRisk({})
        else:
            rows = client.fetch_positions(None, params=position_params)
    except _PositionCooldownError:
        rows = None
        observed_at = None
        response_error = "rate_limit_cooldown"
    except binance_futures_positions.BinanceAccountTypeError:
        rows = None
        observed_at = None
        response_error = "account_type_unverified"
    except Exception as exc:
        if performance_history_cache.is_rate_limit_error(exc):
            raise
        rows = None
        observed_at = None
        response_error = "read_failed"
    else:
        observed_at = datetime.now(UTC).isoformat()
        response_error = "invalid_response"

    positions = []
    issues = []
    skipped_count = 0
    if not isinstance(rows, list):
        skipped_count = None
        issues.append({"symbol": None, "reason": response_error})
    else:
        prepared = (
            binance_futures_positions.prepare_rows(rows, client)
            if exchange_id == "binance" else [(row, None, None) for row in rows]
        )
        certified_read = (
            exchange_id == "binance"
            and bridge.client() is venue_client
            and all(reason is None for _, _, reason in prepared)
        )
        for raw, market, reason in prepared:
            if reason is not None:
                issues.append({"symbol": None, "reason": reason})
                skipped_count += 1
                continue
            if not isinstance(raw, dict) or (
                raw.get("info") is not None and not isinstance(raw.get("info"), dict)
            ):
                reason = "invalid_row"
            else:
                info = raw.get("info") or {}
                symbol = raw.get("symbol")
                if not isinstance(symbol, str) or not symbol.strip():
                    reason = "missing_symbol"
                else:
                    size, directional, reason = _position_size(raw, info)
                    if reason is None:
                        side, reason = _position_side(raw, info, size, directional, exchange_id)
                        if reason is None and size == 0:
                            reason = _flat_notional_issue(raw, info)
                        elif reason is None:
                            positions.append(_normalize(raw, info, size, side, client, market))
            if reason is not None:
                issues.append({"symbol": None, "reason": reason})
                skipped_count += 1
        certifiable = certified_read and skipped_count == 0
        if not certifiable:
            # The venue response itself could not prove account-wide scope.
            issues.insert(0, {"symbol": None, "reason": "scope_unverified"})
        elif monitor_states is not None:
            venue_keys = {(item["symbol"], item["side"]) for item in positions}
            issues.extend(
                _monitor_coverage_issues(monitor_states, exchange_id, profile, venue_keys)
            )

    snapshot = futures_account.FuturesAccountSnapshot(**values, venue_profile=profile).as_dict()
    snapshot["positions"] = positions
    snapshot["positions_read"] = {
        "status": "partial" if issues else "complete",
        "skipped": skipped_count,
        "issues": issues,
        "observed_at": observed_at,
    }
    return snapshot
