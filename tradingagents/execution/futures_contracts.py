"""Validated linear futures contract metadata and unit conversion."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


class FuturesContractError(ValueError):
    """Raised when a futures market cannot support safe unit conversion."""


@dataclass(frozen=True, slots=True)
class FuturesContractSpec:
    """The venue contract used to convert base exposure into contract amount."""

    symbol: str
    contract_size: float
    linear: bool
    inverse: bool

    @classmethod
    def from_market(cls, symbol: str, market: Any) -> FuturesContractSpec:
        if not isinstance(market, dict):
            raise FuturesContractError(
                f"futures contract metadata unavailable for {symbol!r}"
            )
        raw_size = market.get("contractSize")
        if isinstance(raw_size, bool):
            raise FuturesContractError(
                f"futures contract metadata has invalid contractSize for {symbol!r}"
            )
        try:
            contract_size = float(raw_size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise FuturesContractError(
                f"futures contract metadata has invalid contractSize for {symbol!r}"
            ) from exc
        if not math.isfinite(contract_size) or contract_size <= 0:
            raise FuturesContractError(
                f"futures contract metadata has invalid contractSize for {symbol!r}"
            )
        if market.get("linear") is not True or market.get("inverse") is not False:
            raise FuturesContractError(
                f"futures contract metadata is not a supported linear contract for {symbol!r}"
            )
        return cls(
            symbol=symbol,
            contract_size=contract_size,
            linear=True,
            inverse=False,
        )

    def contracts_for_base(self, quantity: float) -> float:
        """Convert a positive base-asset quantity into venue contracts."""
        value = _positive_finite(quantity, "base quantity")
        contracts = value / self.contract_size
        if not math.isfinite(contracts) or contracts <= 0:
            raise FuturesContractError("base quantity produced no executable contracts")
        return contracts

    def base_for_contracts(self, contracts: float) -> float:
        """Convert a positive venue contract amount into base-asset quantity."""
        value = _positive_finite(contracts, "contract quantity")
        quantity = value * self.contract_size
        if not math.isfinite(quantity) or quantity <= 0:
            raise FuturesContractError("contract quantity produced no base quantity")
        return quantity


def _positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise FuturesContractError(f"{name} must be finite and positive")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FuturesContractError(f"{name} must be finite and positive") from exc
    if not math.isfinite(number) or number <= 0:
        raise FuturesContractError(f"{name} must be finite and positive")
    return number
