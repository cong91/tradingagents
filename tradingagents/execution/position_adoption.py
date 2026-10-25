"""Plan the adoption of a pre-existing protective ladder into one bracket.

Positions opened before durable linkage existed can carry several protective
legs on one net position. This module decides what the single replacement stop
and target should be; it performs no venue action and is therefore testable
without a network or a fake exchange.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AdoptionPlan:
    symbol: str
    side: str
    contracts: float
    close_side: str
    entry: float | None
    stop: float | None
    target: float | None
    kept_legs: tuple[str, ...]
    dropped_legs: tuple[str, ...]
    stop_shortfall: float


def plan_adoption(position, legs, *, fallback_stop: float | None = None,
                  tolerance: float = 1e-6) -> AdoptionPlan:
    """Collapse a protective ladder into one full-size stop and target.

    Weighted averaging preserves the ladder's total loss (stop) and total
    profit (target) at the same position size, so adoption changes the
    management model without silently tightening or loosening the economics.

    ``fallback_stop`` is the stop the bot recorded when it opened the position.
    It is only used when the venue ladder has no stop left, so a missing leg is
    restored from the original decision instead of an invented price.
    """
    contracts = abs(float(position.contracts))
    if not math.isfinite(contracts) or contracts <= 0:
        raise ValueError("position contracts must be finite and positive")
    side = str(position.side)
    if side not in {"long", "short"}:
        raise ValueError("position side must be long or short")
    close_side = "sell" if side == "long" else "buy"

    stops = [leg for leg in (_leg(item) for item in legs if _type(item) == "STOP_MARKET") if leg]
    targets = [leg for leg in (_leg(item) for item in legs if _type(item) == "TAKE_PROFIT_MARKET") if leg]

    kept_stops, dropped_stops = _trim(stops, contracts, side, tolerance)
    kept_targets, dropped_targets = _trim(targets, contracts, side, tolerance)

    covered = sum(leg[0] for leg in kept_stops)
    shortfall = max(0.0, contracts - covered)
    if shortfall <= contracts * tolerance:
        shortfall = 0.0
    stop = _weighted(kept_stops)
    if stop is None and shortfall > 0:
        restored = _finite(fallback_stop)
        if restored is not None and _protects(side, _finite(position.entry_price), restored):
            stop = restored
            shortfall = 0.0
    return AdoptionPlan(
        symbol=str(position.symbol),
        side=side,
        contracts=contracts,
        close_side=close_side,
        entry=_finite(position.entry_price),
        stop=stop,
        target=_weighted(kept_targets),
        kept_legs=tuple(leg[2] for leg in kept_stops + kept_targets),
        dropped_legs=tuple(leg[2] for leg in dropped_stops + dropped_targets),
        stop_shortfall=shortfall,
    )


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _protects(side: str, entry: float | None, stop: float) -> bool:
    """A usable stop must sit on the losing side of the entry."""
    if entry is None:
        return False
    return stop > entry if side == "short" else stop < entry


def _type(leg: dict) -> str:
    info = leg.get("info") if isinstance(leg.get("info"), dict) else leg
    return str(info.get("orderType") or leg.get("type") or "").upper()


def _leg(leg: dict) -> tuple[float, float, str] | None:
    info = leg.get("info") if isinstance(leg.get("info"), dict) else leg
    try:
        quantity = abs(float(info.get("quantity") or leg.get("amount")))
        price = float(info.get("triggerPrice") or leg.get("triggerPrice"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(price) or price <= 0:
        return None
    return quantity, price, str(info.get("algoId") or leg.get("id") or "")


def _trim(legs, contracts, side, tolerance):
    """Drop the least favourable legs until the ladder fits the position."""
    if not legs:
        return [], []
    total = sum(leg[0] for leg in legs)
    if total <= contracts * (1 + tolerance):
        return list(legs), []
    # Worst end: a short loses more on a higher stop and earns less on a higher
    # target; a long mirrors both. Dropping there keeps the intended protection.
    higher_is_worse = side == "short"
    ordered = sorted(legs, key=lambda leg: leg[1], reverse=higher_is_worse)
    kept, dropped = [], []
    remaining = total
    for leg in ordered:
        if remaining - leg[0] >= contracts * (1 - tolerance):
            dropped.append(leg)
            remaining -= leg[0]
        else:
            kept.append(leg)
    return kept, dropped


def _weighted(legs) -> float | None:
    total = sum(leg[0] for leg in legs)
    if total <= 0:
        return None
    return sum(leg[0] * leg[1] for leg in legs) / total
