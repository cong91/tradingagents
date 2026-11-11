"""Evaluate a stored observation against the candles that actually followed it.

Every metric here answers a question the research report asks for (report §5D):
did the direction match, did the realized path stay inside the forecast band, and
would the forecast's own band have hit the stop or the target first. The rules:

- only candles that closed AFTER the forecast's data cutoff are used, and only
  once the whole horizon has realized — otherwise the evaluation is
  ``unavailable`` with the reason, never a partial score dressed as a result;
- the stop/target ordering reuses the simulation's own ``match_exit`` so the
  ambiguity policy (worst-first when one candle contains both levels) is the
  same rule the backtest uses, and ``ambiguity_count`` is published;
- ``net_r`` is only claimed when the ledger actually recorded it; it is a
  pass-through with its own reason string when unknown — never a computed PnL
  from prices this module does not own;
- deterministic rules have no band: ``band_coverage`` stays ``None`` with the
  reason recorded instead of a made-up ``True``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradingagents.dataflows.market_data import Candle, timeframe_ms
from tradingagents.shadow.provider import (
    DISTRIBUTION_RULE,
    STATUS_OK,
    direction_of,
)
from tradingagents.simulation.fills import MarketRules, match_exit

STATUS_EVAL_OK = "ok"
STATUS_EVAL_PARTIAL = "partial"
STATUS_EVAL_UNAVAILABLE = "unavailable"

_MIN_RULES = MarketRules(lot_step=1e-9, min_notional=0.0, tick_size=1e-9)


def _ms(moment: str) -> int:
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def realized_candles(
    candles: list[Candle] | tuple[Candle, ...],
    *,
    timeframe: str,
    after_open_time_ms: int,
    horizon_hours: int,
) -> tuple[Candle, ...]:
    """The contiguous closed candles covering the horizon after the cutoff.

    Returns () when the horizon has not fully realized: a partial window would
    score a forecast against an outcome that has not happened yet.
    """
    duration = timeframe_ms(timeframe)
    horizon_ms = int(horizon_hours * 3_600_000)
    ordered = sorted(
        (candle for candle in candles if candle.is_closed and candle.timeframe == timeframe),
        key=lambda candle: candle.open_time,
    )
    window = [candle for candle in ordered if candle.open_time >= after_open_time_ms]
    if not window:
        return ()
    if window[0].open_time != after_open_time_ms:
        return ()  # the series does not resume at the cutoff: cannot attribute
    for previous, current in zip(window, window[1:], strict=False):
        if current.open_time != previous.close_time:
            return ()
    if window[-1].close_time - after_open_time_ms < horizon_ms:
        return ()
    needed = max(1, horizon_ms // duration)
    return tuple(window[:needed])


def evaluate_forecast(
    forecast: dict[str, Any],
    candles: list[Candle] | tuple[Candle, ...],
    *,
    stop: float | None = None,
    target: float | None = None,
    side: str | None = None,
    net_r: float | None = None,
    net_r_reason: str | None = None,
) -> dict[str, Any]:
    """Score one stored forecast row; never fabricates a missing input."""
    if forecast.get("status") != STATUS_OK:
        return {
            "status": STATUS_EVAL_UNAVAILABLE,
            "reason": (
                f"the observation itself is {forecast.get('status')}; there is no "
                "forecast to evaluate"
            ),
            "realized_direction": None,
            "realized_direction_hit": None,
            "band_coverage": None,
            "stop_first_rate": None,
            "net_r": net_r,
            "net_r_reason": net_r_reason or "the ledger did not record a net R for this cycle",
            "realized": {},
        }
    reference = (forecast.get("provenance") or {}).get("reference_close")
    if not isinstance(reference, (int, float)):
        return {
            "status": STATUS_EVAL_UNAVAILABLE,
            "reason": "the observation row carries no reference close",
            "realized_direction": None,
            "realized_direction_hit": None,
            "band_coverage": None,
            "stop_first_rate": None,
            "net_r": net_r,
            "net_r_reason": net_r_reason or "the ledger did not record a net R for this cycle",
            "realized": {},
        }
    cutoff_ms = _ms(str(forecast["data_open_time"])) + timeframe_ms(str(forecast["timeframe"]))
    window = realized_candles(
        candles,
        timeframe=str(forecast["timeframe"]),
        after_open_time_ms=cutoff_ms,
        horizon_hours=int(forecast["horizon_hours"]),
    )
    if not window:
        return {
            "status": STATUS_EVAL_UNAVAILABLE,
            "reason": (
                "the horizon has not fully realized (or the after-cutoff candles are "
                "missing/gapped); no evaluation is claimed"
            ),
            "realized_direction": None,
            "realized_direction_hit": None,
            "band_coverage": None,
            "stop_first_rate": None,
            "net_r": net_r,
            "net_r_reason": net_r_reason or "the ledger did not record a net R for this cycle",
            "realized": {},
        }

    realized_close = window[-1].close
    realized_direction = direction_of(float(reference), realized_close)
    predicted = forecast.get("direction")
    hit = None if predicted in (None, "flat") else predicted == realized_direction
    band = _band(forecast)
    coverage = None
    band_path_inside = None
    if band is not None:
        low, high = band
        # The band is the p10..p90 distribution of END-OF-HORIZON closes, so the
        # question it answers is where the price ENDED — not how it wandered on
        # the way. Comparing every bar against it made coverage almost always
        # False for a real Kronos band (a ±0.1% band around the reference cannot
        # contain 24 hourly closes), which would have read as "the model is
        # always wrong" no matter what the model said. The path question is
        # reported separately and honestly labelled.
        coverage = low <= realized_close <= high
        band_path_inside = all(low <= candle.close <= high for candle in window)
    stop_first_rate, ambiguity, measured_paths = _stop_first(
        forecast, window, stop=stop, target=target, side=side,
    )
    status = STATUS_EVAL_OK if predicted not in (None,) else STATUS_EVAL_PARTIAL
    return {
        "status": status,
        "reason": None if status == STATUS_EVAL_OK else "the observation recorded no direction",
        "realized_direction": realized_direction,
        "realized_direction_hit": hit,
        "band_coverage": coverage,
        "stop_first_rate": stop_first_rate,
        "net_r": net_r,
        "net_r_reason": net_r_reason or (
            "the ledger recorded a net R for this cycle" if net_r is not None
            else "the ledger did not record a net R for this cycle"
        ),
        "realized": {
            "reference_close": float(reference),
            "realized_close": realized_close,
            "bars": len(window),
            "first_open_time": datetime.fromtimestamp(window[0].open_time / 1000, UTC).isoformat(),
            "last_close_time": datetime.fromtimestamp(window[-1].close_time / 1000, UTC).isoformat(),
            "high": max(candle.high for candle in window),
            "low": min(candle.low for candle in window),
            "stop_first_ambiguity_count": ambiguity,
            # The denominator the rate was computed over: paths that actually
            # reached a level. A path that touched neither is not evidence.
            "stop_first_measured_paths": measured_paths,
            "stop_first_semantics": (
                "share of the model's OWN paths that reached the recorded stop "
                "before the recorded target (hypothetical, not a traded outcome); "
                "a path that touched neither level is excluded from the denominator"
            ),
            "distribution_kind": forecast.get("distribution_kind"),
            "band_available": band is not None,
            "band_semantics": (
                "coverage asks whether the END-OF-HORIZON close fell inside the "
                "forecast band (the band is the p10..p90 of per-path end closes)"
                if band is not None else None
            ),
            # Published separately: did every intermediate close ALSO stay inside?
            # A band built from end-point percentiles is not a path corridor, so
            # this is context, never the headline metric.
            "band_held_throughout": band_path_inside,
            "band_reason": None if band is not None else (
                "a deterministic rule has no percentile band; coverage is not claimed"
            ),
        },
    }


def _band(forecast: dict[str, Any]) -> tuple[float, float] | None:
    if forecast.get("distribution_kind") == DISTRIBUTION_RULE:
        return None
    samples = forecast.get("samples") or {}
    band = samples.get("band") if isinstance(samples, dict) else None
    if not isinstance(band, dict):
        return None
    low, high = band.get("low"), band.get("high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return float(low), float(high)


def _stop_first(
    forecast: dict[str, Any],
    window: tuple[Candle, ...],
    *,
    stop: float | None,
    target: float | None,
    side: str | None,
) -> tuple[float | None, int]:
    """Share of the model's OWN paths that reached the stop before the target.

    This asks a hypothetical: had the recorded stop/target been resting while the
    path unfolded as the model imagined it, which level would the path have
    touched first? Each path contributes its own high/low sequence (so the answer
    is about that path, not one averaged level), the levels are the cycle's
    recorded ones, and gap handling plus the worst-first rule come from
    ``fills.match_exit`` — the same matcher the backtest uses. ``ambiguity_count``
    is published rather than resolved silently.

    ``None`` (unknown) when there is nothing honest to measure: no stop/target,
    no side, no sampled paths, paths whose bar count does not match the realized
    horizon, or paths that touch NEITHER level.

    The denominator is the paths that resolved the question, not every path: a
    path that never comes near the stop or the target says nothing about which
    came first, so counting it as "target first" would report 0.5 for a set where
    the only path that reached a level reached the stop (probed: 1/2 instead of
    1/1). ``stop_first_measured_paths`` is published so the denominator is
    visible. A deterministic rule never has paths, so it is always unknown here —
    its stop-first answer comes from the realized path, which the caller reads
    from ``realized_candles`` separately.
    """
    if stop is None or target is None or side not in ("long", "short"):
        return None, 0, 0
    paths = forecast.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return None, 0, 0
    stop_first = 0
    measured = 0
    ambiguity = 0
    for path in paths:
        if not isinstance(path, dict):
            continue
        highs = path.get("highs") or []
        lows = path.get("lows") or []
        closes = path.get("closes") or []
        if len(highs) != len(window) or len(lows) != len(window) or len(closes) != len(window):
            continue
        touched: str | None = None
        for candle, high, low, close in zip(window, highs, lows, closes, strict=True):
            probe = Candle(
                symbol=candle.symbol, timeframe=candle.timeframe,
                open_time=candle.open_time, close_time=candle.close_time,
                open=float(close), high=float(high), low=float(low),
                close=float(close), volume=candle.volume, is_closed=True,
            )
            match = match_exit(
                probe, side, stop=stop, target=target,
                quantity=1.0, rules=_MIN_RULES, ambiguity_policy="worst-first",
            )
            if match.ambiguous:
                ambiguity += 1
            if match.fill is None:
                continue
            touched = match.fill.kind
            break
        if touched is None:
            continue  # this path never resolved stop-vs-target: no evidence either way
        measured += 1
        stop_first += touched == "stop"
    if not measured:
        return None, ambiguity, 0
    return stop_first / measured, ambiguity, measured


__all__ = [
    "STATUS_EVAL_OK",
    "STATUS_EVAL_PARTIAL",
    "STATUS_EVAL_UNAVAILABLE",
    "evaluate_forecast",
    "realized_candles",
]
