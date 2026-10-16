"""Futures safety contracts shared by planning and execution."""

from __future__ import annotations

import math
from dataclasses import dataclass


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


def risk_limited_quantity(*, equity: float | None, entry: float | None,
                          stop_loss: float | None, leverage: float,
                          risk_fraction: float, margin_fraction: float,
                          side: str = "buy", take_profit: float | None = None,
                          amount_step: float | None = None,
                          min_quantity: float = 0.0,
                          max_quantity: float | None = None) -> float:
    """Return a fail-closed quantity bounded by stop risk and margin budget."""
    if take_profit is None:
        if entry is None or not math.isfinite(float(entry)) or float(entry) <= 0:
            raise FuturesPlanError("derivatives sizing requires a positive entry")
        distance = abs(float(entry) - float(stop_loss)) if stop_loss is not None else 0.0
        take_profit = float(entry) + distance if side == "buy" else float(entry) - distance
    validate_bracket(side=side, entry=entry, stop_loss=stop_loss, take_profit=take_profit)
    if equity is None or not math.isfinite(equity) or equity <= 0:
        raise FuturesPlanError("derivatives sizing requires positive computable equity")
    if (not all(math.isfinite(value) and value > 0 for value in
                (leverage, risk_fraction, margin_fraction))):
        raise FuturesPlanError("derivatives sizing parameters must be positive")
    assert entry is not None and stop_loss is not None
    distance = abs(entry - stop_loss)
    risk_qty = equity * risk_fraction / distance
    margin_qty = equity * margin_fraction * leverage / entry
    quantity = min(risk_qty, margin_qty)
    if max_quantity is not None:
        if not math.isfinite(max_quantity) or max_quantity <= 0:
            raise FuturesPlanError("maximum quantity must be positive")
        quantity = min(quantity, max_quantity)
    if amount_step is not None:
        if not math.isfinite(amount_step) or amount_step <= 0:
            raise FuturesPlanError("amount step must be positive")
        quantity = math.floor(quantity / amount_step) * amount_step
    if not math.isfinite(min_quantity) or min_quantity < 0 or quantity < min_quantity:
        raise FuturesPlanError("risk-sized quantity is below exchange minimum")
    # The budgets are what the quantity was derived from, so without amount
    # rounding this recheck can only trip on float roundtrip noise (one ulp);
    # a tiny relative tolerance keeps that noise from refusing valid plans
    # while any real breach (flooring aside) still fails closed.
    tolerance = equity * 1e-9
    if (quantity * distance > equity * risk_fraction + tolerance
            or quantity * entry / leverage > equity * margin_fraction + tolerance):
        raise FuturesPlanError("precision-rounded quantity exceeds risk or margin budget")
    if not math.isfinite(quantity) or quantity <= 0:
        raise FuturesPlanError("derivatives sizing produced no executable quantity")
    return quantity
