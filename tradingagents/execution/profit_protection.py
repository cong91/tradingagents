"""Pure futures profit-protection policy, expressed in initial-risk units."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProfitProtectionDecision:
    stop: float
    target: float | None
    phase: str
    exit_reason: str | None = None


def evaluate_profit_protection(
    *,
    side: Literal["long", "short"],
    entry: float,
    initial_stop: float,
    current_stop: float,
    current_target: float | None,
    mark: float | None,
    break_even_r: float = 1.0,
    lock_profit_r: float = 2.0,
    trail_start_r: float = 3.0,
    trail_distance_r: float = 1.0,
    target_extension_r: float = 1.0,
    min_stop_move_r: float = 0.1,
    round_trip_fee_rate: float = 0.0,
    held_hours: float | None = None,
    max_hold_hours: float | None = None,
    time_stop_min_r: float = 1.0,
    cumulative_funding_r: float | None = None,
    round_trip_fee_r: float | None = None,
) -> ProfitProtectionDecision:
    """Return monotonic stop/target levels without performing venue actions.

    The time-stop boundary alone reads funding: ``cumulative_funding_r``
    carries the venue's sign (positive = the position is being paid) and
    ``round_trip_fee_r`` the exit fee in initial-risk units, so the deadline
    verdict uses ``effective_r = favorable_r + funding - fee``. Break-even,
    lock and trailing stops stay purely price-based because the venue trigger
    is a price, not an account figure.
    """
    if mark is None or not _positive_finite(mark):
        return ProfitProtectionDecision(current_stop, current_target, "unchanged")
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    if not all(_positive_finite(value) for value in (entry, initial_stop, current_stop)):
        raise ValueError("entry and stops must be finite positive prices")
    if current_target is not None and not _positive_finite(current_target):
        raise ValueError("target must be a finite positive price")
    if not all(_positive_finite(value) for value in (
        break_even_r, lock_profit_r, trail_start_r, trail_distance_r,
        target_extension_r,
    )):
        raise ValueError("profit-protection thresholds must be finite and positive")
    if not _non_negative_finite(min_stop_move_r):
        raise ValueError("minimum stop movement must be finite and non-negative")
    if not (_non_negative_finite(round_trip_fee_rate) and float(round_trip_fee_rate) < 1):
        raise ValueError("round-trip fee rate must be finite and within [0, 1)")
    fee_rate = float(round_trip_fee_rate)
    if max_hold_hours is not None and not _positive_finite(max_hold_hours):
        raise ValueError("maximum hold duration must be finite and positive")
    if held_hours is not None and not _non_negative_finite(held_hours):
        raise ValueError("held duration must be finite and non-negative")
    if not _positive_finite(time_stop_min_r):
        raise ValueError("time-stop minimum R must be finite and positive")
    if cumulative_funding_r is not None and not _finite(cumulative_funding_r):
        raise ValueError("cumulative funding R must be finite")
    if round_trip_fee_r is not None and not _finite(round_trip_fee_r):
        raise ValueError("round-trip fee R must be finite")
    if not break_even_r < lock_profit_r < trail_start_r:
        raise ValueError("profit-protection thresholds must be strictly increasing")

    long = side == "long"
    risk = entry - initial_stop if long else initial_stop - entry
    current_geometry_ok = current_stop >= initial_stop if long else current_stop <= initial_stop
    if risk <= 0 or not current_geometry_ok:
        raise ValueError("initial stop and current stop must protect the position")

    favorable = mark - entry if long else entry - mark
    favorable_r = favorable / risk
    funding_r = 0.0 if cumulative_funding_r is None else float(cumulative_funding_r)
    fee_r = 0.0 if round_trip_fee_r is None else float(round_trip_fee_r)
    effective_r = favorable_r + funding_r - fee_r
    past_deadline = (
        max_hold_hours is not None
        and held_hours is not None
        and held_hours >= max_hold_hours
    )
    if past_deadline and effective_r < time_stop_min_r:
        return ProfitProtectionDecision(current_stop, current_target, "time_stop", "time_stop")
    candidate = current_stop
    phase = "unchanged"
    if favorable_r >= trail_start_r:
        candidate = mark - trail_distance_r * risk if long else mark + trail_distance_r * risk
        phase = "trailing"
    elif favorable_r >= lock_profit_r:
        candidate = entry + risk if long else entry - risk
        phase = "profit_lock"
    elif favorable_r >= break_even_r:
        candidate = entry * (1 + fee_rate) if long else entry * (1 - fee_rate)
        phase = "break_even"

    stop = max(current_stop, candidate) if long else min(current_stop, candidate)
    stop_advance = stop - current_stop if long else current_stop - stop
    if stop_advance < min_stop_move_r * risk:
        stop = current_stop
        phase = "unchanged"
    target = current_target
    target_reached = current_target is not None and (
        mark >= current_target if long else mark <= current_target
    )
    if target_reached:
        extension = target_extension_r * risk
        if long:
            steps = max(1, math.floor((mark - current_target) / extension) + 1)
            target = current_target + steps * extension
        else:
            steps = max(1, math.floor((current_target - mark) / extension) + 1)
            target = current_target - steps * extension
        if phase == "unchanged":
            phase = "target_extended"

    if past_deadline and favorable_r < time_stop_min_r and effective_r >= time_stop_min_r:
        # The price boundary tripped, yet funding and fees kept the effective R
        # above the minimum: the position stays, and the phase says why.
        phase = "time_stop_blocked_by_funding"

    return ProfitProtectionDecision(stop, target, phase)


def _finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _positive_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _non_negative_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0
