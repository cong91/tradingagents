"""Point-in-time scoring for recorded forward Kronos forecasts."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

from server.forecast_evaluation_venue import target_candle_exchange
from tradingagents.dataflows.market_data import fetch_closed_candles

logger = logging.getLogger(__name__)
_MAX_DB_INTEGER = 2**31 - 1
_RECORDED_AT_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-][01][0-9]:[0-5][0-9])$"
)


def digest(block: Any) -> str:
    material = json.dumps(block, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def target_close_time(block: dict[str, Any]) -> datetime | None:
    """Close of the last predicted bar, anchored to the last input candle."""
    open_time = block.get("data_open_time")
    horizon = block.get("horizon_bars")
    timeframe = block.get("timeframe")
    if (
        isinstance(open_time, bool) or not isinstance(open_time, int)
        or isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1
        or not isinstance(timeframe, str) or not timeframe
    ):
        return None
    from tradingagents.auto_timeframes import timeframe_ms

    try:
        duration_ms = timeframe_ms(timeframe)
    except ValueError:
        return None
    # The first forecast bar starts when the last input bar closes. Its close
    # is one further duration later; horizon bars end at first_close + (N - 1).
    target_ms = open_time + duration_ms * (horizon + 1)
    try:
        return datetime.fromtimestamp(target_ms / 1000, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def base_evaluation(cycle: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    as_of = parse_time(block.get("as_of"))
    raw_recorded_at = block.get("recorded_at")
    recorded_at = (
        parse_time(raw_recorded_at)
        if isinstance(raw_recorded_at, str)
        and _RECORDED_AT_PATTERN.fullmatch(raw_recorded_at)
        and (raw_recorded_at.endswith("Z") or int(raw_recorded_at[-5:-3]) <= 15)
        else None
    )
    target_as_of = target_close_time(block)
    timeframe = block.get("timeframe")
    horizon_bars = block.get("horizon_bars")
    open_time = block.get("data_open_time")
    horizon_key = "|".join((
        str(timeframe or "unknown"), str(open_time if open_time is not None else "unknown"),
        str(horizon_bars if horizon_bars is not None else "unknown"),
    ))
    raw_band = block.get("band")
    band = raw_band if isinstance(raw_band, dict) else {}
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    venue_scope = cycle.get("venue_scope") or payload.get("kronos_venue_scope") or {}
    raw_ratio = block.get("direction_ratio")
    return {
        "cycle_id": str(cycle.get("cycle_id") or ""),
        "symbol": str(cycle.get("symbol") or ""),
        "forecast_digest": digest(block),
        "horizon_key": horizon_key,
        "venue_scope": venue_scope if isinstance(venue_scope, dict) else {},
        "forecast_as_of": as_of,
        "forecast_created_at": parse_time(block.get("created_at")),
        "forecast_recorded_at": recorded_at,
        "target_as_of": target_as_of,
        "timeframe": timeframe if isinstance(timeframe, str) else None,
        "profile": block.get("profile") if isinstance(block.get("profile"), str) else None,
        "effective_lookback_bars": block.get("effective_lookback_bars"),
        "horizon_bars": horizon_bars if type(horizon_bars) is int else None,
        "horizon_hours": number(block.get("horizon_hours")),
        "generation_kind": block.get("generation_kind"),
        "direction": block.get("direction"),
        "direction_ratio": raw_ratio if isinstance(raw_ratio, dict) else {},
        "band": band,
        "reference_close": number(block.get("reference_close")),
        "predicted_close": number(band.get("p50")),
        "provenance": {
            "source": "forecast_evaluation_worker",
            "data_open_time": open_time if type(open_time) is int else None,
            "timestamp_evidence": "self_reported_unverified",
        },
        "last_attempted_at": datetime.now(UTC),
    }


def _constraint_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Keep malformed evidence representable in the nullable DB columns."""
    safe = dict(row)
    for key in ("effective_lookback_bars", "horizon_bars"):
        value = safe.get(key)
        if value is not None and (
            type(value) is not int or not 0 < value <= _MAX_DB_INTEGER
        ):
            safe[key] = None
    for key in ("reference_close", "predicted_close", "target_close"):
        value = safe.get(key)
        if value is not None and (number(value) is None or number(value) <= 0):
            safe[key] = None
    for key in ("absolute_error", "absolute_error_pct"):
        value = safe.get(key)
        if value is not None and (number(value) is None or number(value) < 0):
            safe[key] = None
    return safe


def invalid(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **_constraint_safe(row), "status": "invalid", "reason": reason, "evaluated_at": None,
    }


def unavailable(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **_constraint_safe(row), "status": "unavailable", "reason": reason, "evaluated_at": None,
    }


def malformed(cycle: dict[str, Any], reason: str) -> dict[str, Any]:
    """Quarantine malformed persisted evidence without poisoning the scan."""
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    scope = payload.get("kronos_venue_scope")
    return {
        "cycle_id": str(cycle.get("cycle_id") or ""),
        "symbol": str(cycle.get("symbol") or ""),
        "forecast_digest": digest(payload.get("kronos_forecast")),
        "horizon_key": "malformed",
        "venue_scope": scope if isinstance(scope, dict) else {},
        "status": "invalid",
        "reason": reason,
        "direction_ratio": {},
        "band": {},
        "warnings": ["malformed_forecast_evidence"],
        "provenance": {"source": "forecast_evaluation_worker"},
        "last_attempted_at": datetime.now(UTC),
    }


def evaluate(
    cycle: dict[str, Any], block: dict[str, Any], *, exchange: Any | None,
    now: datetime | None = None,
    candle_fetcher=fetch_closed_candles,
) -> dict[str, Any]:
    row = base_evaluation(cycle, block)
    if block.get("symbol") != row["symbol"]:
        return invalid(row, "forecast symbol does not match its Futures cycle")
    if block.get("status") != "ok":
        source_status = block.get("status")
        reason = block.get("reason") or f"forecast status is {source_status or 'missing'}"
        return unavailable(row, str(reason)) if source_status in {
            "unavailable", "timeout", "error",
        } else invalid(row, str(reason))
    if block.get("generation_kind") != "forward":
        return invalid(row, "only forward forecasts are evaluated")
    cycle_timeframe = cycle.get("cycle_timeframe")
    cycle_open = cycle.get("cycle_selected_open")
    if not isinstance(cycle_timeframe, str) or not cycle_timeframe or type(cycle_open) is not int:
        return invalid(row, "recorded Futures cycle identity is missing or invalid")
    if block.get("timeframe") != cycle_timeframe:
        return invalid(row, "forecast timeframe does not match its Futures cycle")
    if block.get("data_open_time") != cycle_open:
        return invalid(row, "forecast input candle does not match its Futures cycle")
    raw_ratio = block.get("direction_ratio")
    if raw_ratio is not None and not isinstance(raw_ratio, dict):
        return invalid(row, "forecast direction ratios are malformed")
    raw_band = block.get("band")
    if raw_band is not None and not isinstance(raw_band, dict):
        return invalid(row, "forecast band is malformed")
    lookback = block.get("effective_lookback_bars")
    if lookback is not None and (
        type(lookback) is not int or not 0 < lookback <= _MAX_DB_INTEGER
    ):
        return invalid(row, "effective input lookback is malformed")
    horizon = block.get("horizon_bars")
    if type(horizon) is not int or not 0 < horizon <= _MAX_DB_INTEGER:
        return invalid(row, "forecast horizon or input candle identity is invalid")
    if row["target_as_of"] is None:
        return invalid(row, "forecast horizon or input candle identity is invalid")
    if row["forecast_as_of"] is None or row["forecast_created_at"] is None:
        return invalid(row, "forecast timestamps are missing or invalid")

    from tradingagents.auto_timeframes import timeframe_ms

    try:
        duration_ms = timeframe_ms(str(row["timeframe"]))
        input_open_ms = block["data_open_time"]
        if isinstance(input_open_ms, bool) or not isinstance(input_open_ms, int):
            raise ValueError("input open time must be integer milliseconds")
        input_close_ms = input_open_ms + duration_ms
        first_predicted_close_ms = input_close_ms + duration_ms
        input_close = datetime.fromtimestamp(input_close_ms / 1000, UTC)
        first_predicted_close = datetime.fromtimestamp(first_predicted_close_ms / 1000, UTC)
    except (KeyError, OSError, TypeError, ValueError, OverflowError):
        return invalid(row, "forecast input candle boundary is invalid")
    as_of = row["forecast_as_of"]
    created = row["forecast_created_at"]
    recorded = row["forecast_recorded_at"]
    if recorded is None:
        return invalid(row, "forecast recording timestamp is missing or invalid")
    if not input_close <= as_of < first_predicted_close:
        return invalid(row, "forecast cutoff does not match its last closed input candle")
    if not as_of <= created < first_predicted_close:
        return invalid(row, "forecast creation timestamp is not forward of its input cutoff")
    if not created <= recorded < first_predicted_close:
        return invalid(row, "forecast was not recorded before its first predicted candle closed")

    if (
        row["reference_close"] is None or row["reference_close"] <= 0
        or row["predicted_close"] is None or row["predicted_close"] <= 0
    ):
        return invalid(row, "forecast reference or median is missing or invalid")
    band = row["band"]
    low, high = number(band.get("low")), number(band.get("high"))
    if low is None or high is None or low <= 0 or high < low:
        return invalid(row, "forecast band is missing or invalid")
    if row["predicted_close"] < low or row["predicted_close"] > high:
        return invalid(row, "forecast median is outside its band")

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if current < row["target_as_of"]:
        return {**row, "status": "pending", "reason": "forecast horizon has not closed"}
    if exchange is None:
        return unavailable(row, "read-only Futures market-data client is unavailable")

    target_ms = int(row["target_as_of"].timestamp() * 1000)
    try:
        candles = candle_fetcher(
            target_candle_exchange(exchange, target_ms - duration_ms), row["symbol"], row["timeframe"],
            as_of=row["target_as_of"], limit=1,
        )
    except Exception as exc:
        logger.info("forecast evaluation candle read failed for %s (%s)", row["symbol"], type(exc).__name__)
        return unavailable(row, f"target candle read failed: {type(exc).__name__}")
    candle = candles[-1] if candles else None
    if candle is None or candle.close_time != target_ms:
        return unavailable(row, "exact target horizon candle is not available yet")
    expected_open = target_ms - duration_ms
    if (
        candle.open_time != expected_open or candle.symbol != row["symbol"]
        or candle.timeframe != row["timeframe"] or candle.is_closed is not True
    ):
        return unavailable(row, "target candle identity or close state is invalid")
    ohlc = tuple(number(getattr(candle, field, None)) for field in ("open", "high", "low", "close"))
    candle_open, candle_high, candle_low, candle_close = ohlc
    if (
        any(value is None or value <= 0 for value in ohlc)
        or candle_high < max(candle_open, candle_close)
        or candle_low > min(candle_open, candle_close)
        or candle_high < candle_low
    ):
        return unavailable(row, "target candle OHLC values are invalid")

    target_close = candle_close
    reference = row["reference_close"]
    predicted = row["predicted_close"]
    error = abs(target_close - predicted)
    error_pct = error / abs(predicted) * 100.0
    actual_direction = "up" if target_close > reference else "down" if target_close < reference else "flat"
    direction = row["direction"]
    direction_correct = actual_direction == direction if direction in {"up", "down", "flat"} else None
    return {
        **row,
        "status": "evaluated",
        "target_close": target_close,
        "target_return": (target_close - reference) / reference,
        "absolute_error": error,
        "absolute_error_pct": error_pct,
        "directional_correct": direction_correct,
        "band_contains_target": low <= target_close <= high,
        "reason": None,
        "evaluated_at": current,
        "provenance": {
            **row["provenance"],
            "target_candle_open_time": candle.open_time,
            "target_candle_close_time": candle.close_time,
            "error_pct_denominator": "predicted_close",
        },
    }


__all__ = [
    "base_evaluation",
    "digest",
    "evaluate",
    "invalid",
    "malformed",
    "number",
    "parse_time",
    "target_close_time",
    "unavailable",
]
