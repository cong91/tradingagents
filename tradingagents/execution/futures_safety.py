"""Futures safety contracts shared by planning and execution."""

from __future__ import annotations

import math
from dataclasses import dataclass

# This is a leverage-based planning envelope, not a venue liquidation-price
# calculation. It reserves 20% of the theoretical 1/leverage price move, but
# cannot model maintenance-margin tiers, cross collateral, fees, or slippage.
LIQUIDATION_BUFFER = 0.8


@dataclass(frozen=True)
class FuturesPlanError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


def validate_bracket(*, side: str | None, entry: float | None,
                     stop_loss: float | None, take_profit: float | None) -> None:
    """Reject any derivatives order without a complete valid protective bracket."""
    values = (entry, stop_loss, take_profit)
    if side not in {"buy", "sell"}:
        raise FuturesPlanError("derivatives bracket requires buy or sell side")
    if any(value is None or not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        raise FuturesPlanError("derivatives orders require finite positive entry, stop_loss and take_profit")
    assert entry is not None and stop_loss is not None and take_profit is not None
    if side == "buy" and not (stop_loss < entry < take_profit):
        raise FuturesPlanError("long derivatives bracket requires stop_loss < entry < take_profit")
    if side == "sell" and not (stop_loss > entry > take_profit):
        raise FuturesPlanError("short derivatives bracket requires stop_loss > entry > take_profit")
    if stop_loss == entry or take_profit == entry:
        raise FuturesPlanError("derivatives bracket distance must be non-zero")


def validate_liquidation_buffer(*, entry: float, stop_loss: float, leverage: float) -> None:
    """Apply the leverage-based planning envelope to the stop distance.

    This is deliberately not presented as a venue liquidation-price calculation:
    maintenance-margin tiers, cross collateral, fees, and slippage are outside
    this pure function. The exact venue liquidation price must still come from
    the authenticated account/venue when it is available.
    """
    if not all(math.isfinite(float(value)) and float(value) > 0
               for value in (entry, stop_loss, leverage)):
        raise FuturesPlanError("liquidation buffer requires positive entry, stop and leverage")
    distance = abs(float(entry) - float(stop_loss))
    max_distance = float(entry) / float(leverage) * LIQUIDATION_BUFFER
    if distance > max_distance:
        raise FuturesPlanError(
            "stop_loss sits beyond the liquidation buffer: |entry-stop| "
            f"{distance:g} exceeds {LIQUIDATION_BUFFER:g} x entry/leverage "
            f"{max_distance:g} at {float(leverage):g}x — reduce leverage or move the stop"
        )


def budget_limited_quantity(*, equity: float | None, entry: float | None,
                            budget_fraction: float, leverage: float,
                            side: str = "buy", stop_loss: float | None = None,
                            take_profit: float | None = None,
                            amount_step: float | None = None,
                            min_quantity: float = 0.0,
                            max_quantity: float | None = None) -> float:
    """Return a fail-closed quantity sized by the agent's TOTAL-budget allocation.

    ``budget_fraction`` is the agent-owned share of the total futures budget
    (equity) this trade's notional represents: notional = equity * fraction,
    quantity = notional / entry, margin = notional / leverage. The stop must
    sit inside the liquidation buffer implied by the leverage.
    """
    if take_profit is None:
        if entry is None or not math.isfinite(float(entry)) or float(entry) <= 0:
            raise FuturesPlanError("derivatives sizing requires a positive entry")
        distance = abs(float(entry) - float(stop_loss)) if stop_loss is not None else 0.0
        take_profit = float(entry) + distance if side == "buy" else float(entry) - distance
    validate_bracket(side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit)
    if equity is None or not math.isfinite(equity) or equity <= 0:
        raise FuturesPlanError("derivatives sizing requires positive computable equity")
    if isinstance(budget_fraction, bool) or isinstance(leverage, bool):
        raise FuturesPlanError("derivatives sizing parameters must be numeric, not boolean")
    if not math.isfinite(leverage) or leverage <= 0:
        raise FuturesPlanError("derivatives sizing parameters must be positive")
    if not math.isfinite(budget_fraction) or not 0 < budget_fraction <= 1:
        raise FuturesPlanError(
            f"budget_fraction {budget_fraction!r} is outside (0, 1] of the total budget"
        )
    assert entry is not None
    validate_liquidation_buffer(entry=entry, stop_loss=stop_loss, leverage=leverage)
    quantity = equity * budget_fraction / entry
    if max_quantity is not None:
        if not math.isfinite(max_quantity) or max_quantity <= 0:
            raise FuturesPlanError("maximum quantity must be positive")
        quantity = min(quantity, max_quantity)
    if amount_step is not None:
        if not math.isfinite(amount_step) or amount_step <= 0:
            raise FuturesPlanError("amount step must be positive")
        quantity = math.floor(quantity / amount_step) * amount_step
    if not math.isfinite(min_quantity) or min_quantity < 0 or quantity < min_quantity:
        raise FuturesPlanError("budget-sized quantity is below exchange minimum")
    if not math.isfinite(quantity) or quantity <= 0:
        raise FuturesPlanError("derivatives sizing produced no executable quantity")
    return quantity
