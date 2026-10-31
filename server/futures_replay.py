"""Deterministic replay harness for futures confirmation rejections.

Replays the persisted ``futures_cycles`` snapshots (exported verbatim from
PostgreSQL into ``tests/fixtures/futures_cycles_replay.json``) against a
confirmation rule — the production rule from ``server.autoloop`` and any
counterfactual candidates — and groups the outcomes by signal, 1h direction,
4h direction, and reject phase. No network and no look-ahead: every replay
reads only the closed-candle pair that was snapshotted at cycle time, so a
run dated in the past decides exactly as it would have that day.

The harness is analysis-only. It never mutates production state and never
relaxes a gate; a counterfactual that looks better on this sample is a
proposal, not a change.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradingagents.dataflows.market_data import Candle

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "futures_cycles_replay.json"

_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
_LONG_SIGNALS = {_BUY, _OVERWEIGHT}
_SHORT_SIGNALS = {_SELL, _UNDERWEIGHT}


def _direction(signal: str | None) -> str:
    return "short" if signal in _SHORT_SIGNALS else "long"


def _candle_direction(candle: Candle) -> str:
    if candle.close > candle.open:
        return "bullish"
    if candle.close < candle.open:
        return "bearish"
    return "doji"


def _candle_from_payload(raw: dict, timeframe: str, symbol: str) -> Candle | None:
    if not isinstance(raw, dict):
        return None
    try:
        return Candle(
            symbol=str(raw.get("symbol") or symbol),
            timeframe=str(raw.get("timeframe") or timeframe),
            open_time=int(raw["open_time"]),
            close_time=int(raw["close_time"]),
            open=float(raw["open"]),
            high=float(raw["high"]),
            low=float(raw["low"]),
            close=float(raw["close"]),
            volume=float(raw.get("volume") or 0.0),
            is_closed=True,
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ReplayedCycle:
    """One persisted cycle rebuilt from the snapshot, ready for rules."""

    cycle_id: str
    symbol: str
    status: str
    phase: str
    signal: str | None
    confirmation_reason: str | None
    signal_reason: str | None
    context: dict[str, Any] | None

    @property
    def had_snapshot(self) -> bool:
        return self.context is not None


def _rebuild_context(snapshot: dict | None, symbol: str) -> dict[str, Any] | None:
    """Rebuild the autoloop context dict from a persisted snapshot payload."""
    if not isinstance(snapshot, dict):
        return None
    candle_1h = _candle_from_payload(snapshot.get("one_hour"), "1h", symbol)
    candle_4h = _candle_from_payload(snapshot.get("four_hour"), "4h", symbol)
    if candle_1h is None or candle_4h is None:
        return None
    context = dict(snapshot)
    context["one_hour"] = candle_1h
    context["four_hour"] = candle_4h
    context["one_hour_direction"] = _candle_direction(candle_1h)
    context["four_hour_direction"] = _candle_direction(candle_4h)
    context["one_hour_open"] = candle_1h.open_time
    context["one_hour_close"] = candle_1h.close
    context["four_hour_open"] = candle_4h.open_time
    context["four_hour_close"] = candle_4h.close
    return context


def load_cycles(fixture_path: Path = FIXTURE_PATH) -> list[ReplayedCycle]:
    """Load the exported futures_cycles rows; snapshot-free rows have no context."""
    with open(fixture_path, encoding="utf-8") as fh:
        rows = json.load(fh)
    cycles = []
    for row in rows:
        cycles.append(ReplayedCycle(
            cycle_id=str(row["cycle_id"]),
            symbol=str(row.get("symbol") or row.get("cycle_id", "")),
            status=str(row.get("status") or ""),
            phase=str(row.get("phase") or ""),
            signal=row.get("signal"),
            confirmation_reason=row.get("confirmation_reason"),
            signal_reason=row.get("signal_reason"),
            context=_rebuild_context(row.get("snapshot"), str(row.get("symbol") or "")),
        ))
    return cycles


def candle_body_range(candle: Candle) -> float:
    return abs(candle.close - candle.open)


def lower_wick(candle: Candle) -> float:
    return min(candle.open, candle.close) - candle.low


def upper_wick(candle: Candle) -> float:
    return candle.high - max(candle.open, candle.close)


def htf_dominance_pullback_confirms(context: dict, signal: str) -> tuple[bool, str]:
    """Counterfactual: higher-timeframe dominance with a bounded pullback.

    The 4h candle must agree with the signal's direction (hard requirement).
    The 1h candle may either confirm outright or be a bounded pullback against
    the direction: retracement and wick dominance are compared against the 4h
    range, so a strong 4h trend with a shallow 1h retracement still confirms,
    while a 1h bar that retraced more than a fifth of the 4h range does not.
    A doji on either timeframe, a missing direction, or a missing context
    never confirms. Non-tradeable signals need no confirmation, matching the
    production gate.
    """
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        return True, "not_required_for_non_tradeable_signal"
    directions = (context.get("one_hour_direction"), context.get("four_hour_direction"))
    if any(direction not in ("bullish", "bearish", "doji") for direction in directions):
        return False, "missing_candle_direction"
    if directions[0] == "doji" or directions[1] == "doji":
        return False, "doji_candle"
    direction = _direction(signal)
    expected = "bearish" if direction == "short" else "bullish"
    htf_direction = directions[1]
    ltf_direction = directions[0]
    if htf_direction != expected:
        return False, "htf_direction_mismatch"
    if ltf_direction == expected:
        return True, "ltf_aligned"
    # LTF pulls back against the direction: bound the retracement.
    candle_4h: Candle = context["four_hour"]
    candle_1h: Candle = context["one_hour"]
    htf_range = candle_4h.high - candle_4h.low
    if htf_range <= 0:
        return False, "htf_zero_range"
    if direction == "long":
        retrace = candle_4h.high - candle_1h.close
        wick = lower_wick(candle_1h)
    else:
        retrace = candle_1h.close - candle_4h.low
        wick = upper_wick(candle_1h)
    body = candle_body_range(candle_1h)
    if retrace / htf_range > 0.2:
        return False, "ltf_pullback_too_deep"
    if wick / max(body, 1e-12) > 1.5:
        return False, "ltf_pullback_wick_dominant"
    return True, "bounded_pullback"


def baseline_confirms(context: dict, signal: str) -> tuple[bool, str]:
    """Mirror of server.autoloop._confirmation_state (the production rule)."""
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        return True, "not_required_for_non_tradeable_signal"
    expected = "bearish" if _direction(signal) == "short" else "bullish"
    directions = (context.get("one_hour_direction"), context.get("four_hour_direction"))
    if any(direction == "doji" for direction in directions):
        return False, "doji_candle"
    if directions != (expected, expected):
        return False, "direction_mismatch"
    return True, "aligned_direction"


RULES: dict[str, Callable[[dict, str], tuple[bool, str]]] = {
    "baseline": baseline_confirms,
    "htf_dominance_bounded_pullback": htf_dominance_pullback_confirms,
}


def replay_rule(
    cycles: list[ReplayedCycle], rule: Callable[[dict, str], tuple[bool, str]],
) -> list[dict[str, Any]]:
    """Run one confirmation rule over every cycle with a rebuilt context."""
    outcomes = []
    for cycle in cycles:
        context, signal = cycle.context, cycle.signal
        if context is None or not signal:
            continue
        confirmed, reason = rule(context, signal)
        recorded = cycle.confirmation_reason
        outcomes.append({
            "cycle_id": cycle.cycle_id,
            "symbol": cycle.symbol,
            "phase": cycle.phase,
            "status": cycle.status,
            "signal": signal,
            "one_hour_direction": context.get("one_hour_direction"),
            "four_hour_direction": context.get("four_hour_direction"),
            "recorded_confirmation_reason": recorded,
            "rule_result": confirmed,
            "rule_reason": reason,
        })
    return outcomes


def group_counts(outcomes: list[dict[str, Any]], *keys: str) -> dict[tuple, int]:
    counts: Counter[tuple] = Counter()
    for outcome in outcomes:
        counts[tuple(outcome[key] for key in keys)] += 1
    return dict(counts)


def summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a replay: decision counts, flips, and reason distributions."""
    flipped = [
        outcome for outcome in outcomes
        if outcome["rule_result"] and outcome["recorded_confirmation_reason"] in ("direction_mismatch", "doji_candle")
    ]
    new_rejections = [
        outcome for outcome in outcomes
        if not outcome["rule_result"] and outcome["recorded_confirmation_reason"] in ("aligned_direction", None, "")
    ]
    return {
        "total": len(outcomes),
        "confirmed_by_rule": sum(1 for o in outcomes if o["rule_result"]),
        "rejected_by_rule": sum(1 for o in outcomes if not o["rule_result"]),
        "flips_to_confirm": flipped,
        "flips_to_reject": new_rejections,
        "by_signal": dict(sorted(Counter(o["signal"] for o in outcomes).items())),
        "by_one_hour_direction": dict(sorted(Counter(o["one_hour_direction"] for o in outcomes).items())),
        "by_four_hour_direction": dict(sorted(Counter(o["four_hour_direction"] for o in outcomes).items())),
        "rule_reasons": dict(sorted(Counter(o["rule_reason"] for o in outcomes).items())),
        "recorded_reasons": dict(sorted(Counter(
            o["recorded_confirmation_reason"] or "" for o in outcomes
        ).items())),
    }


__all__ = [
    "FIXTURE_PATH",
    "RULES",
    "ReplayedCycle",
    "baseline_confirms",
    "candle_body_range",
    "group_counts",
    "htf_dominance_pullback_confirms",
    "load_cycles",
    "replay_rule",
    "summary",
    "upper_wick",
    "lower_wick",
]
