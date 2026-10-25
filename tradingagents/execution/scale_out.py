"""Deliberate scale-out: two profit tranches plus a trailed runner.

Distinct from a per-entry ladder: the split is computed once from the position's
own risk (R), the quantities always fit the position, and the remainder is a
runner managed by the trailing stop. No venue call happens here.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ScaleOutLevel:
    multiple: float
    price: float
    quantity: float


@dataclass(frozen=True)
class ScaleOutPlan:
    side: str
    entry: float
    initial_stop: float
    risk: float
    contracts: float
    levels: tuple[ScaleOutLevel, ...]
    runner_quantity: float


def plan_scale_out(
    *,
    side: str,
    entry: float,
    initial_stop: float,
    contracts: float,
    first_r: float = 1.0,
    first_fraction: float = 0.33,
    second_r: float = 2.0,
    second_fraction: float = 0.33,
) -> ScaleOutPlan:
    """Split a position into two profit tranches and a trailing runner.

    Fractions are fractions of the *position*, never of the remaining size, so
    the tranches and the runner always sum back to the original contracts.
    """
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    entry = _positive(entry, "entry")
    initial_stop = _positive(initial_stop, "initial_stop")
    contracts = _positive(contracts, "contracts")
    risk = entry - initial_stop if side == "long" else initial_stop - entry
    if risk <= 0:
        raise ValueError("initial stop must protect the position")
    first_r = _positive(first_r, "first_r")
    second_r = _positive(second_r, "second_r")
    if not first_r < second_r:
        raise ValueError("scale-out R levels must be strictly increasing")
    first_fraction = _positive(first_fraction, "first_fraction")
    second_fraction = _positive(second_fraction, "second_fraction")
    if first_fraction + second_fraction >= 1:
        raise ValueError("scale-out fractions must leave a positive runner")

    levels = []
    for multiple, fraction in ((first_r, first_fraction), (second_r, second_fraction)):
        quantity = contracts * fraction
        if quantity <= 0:
            raise ValueError("scale-out tranche quantity must be positive")
        price = entry + multiple * risk if side == "long" else entry - multiple * risk
        levels.append(ScaleOutLevel(multiple=multiple, price=price, quantity=quantity))
    runner = contracts - sum(level.quantity for level in levels)
    if runner <= 0:
        raise ValueError("scale-out leaves no runner")
    return ScaleOutPlan(
        side=side, entry=entry, initial_stop=initial_stop, risk=risk,
        contracts=contracts, levels=tuple(levels), runner_quantity=runner,
    )


def _positive(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number
