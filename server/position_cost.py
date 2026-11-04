"""Holding cost of an open futures position (display only).

The portfolio response reports an estimated round-trip fee per position,
carrying the currency the snapshot itself proved for its notional. Held time,
the stored entry timestamp and funding are no longer displayed: durable
position states carry ``venue_key`` — a venue/profile identity, not an
account — so pairing a venue position with a stored ``opened_at`` or a
funding window could attribute another account's history to this snapshot
(data contract §6.5). Restoring them requires the durable account-binding
pass. Nothing here decides anything — the monitor's time stop keeps its own
funding reader (tradingagents/execution/futures_funding.py).

A missing or malformed input stays ``None`` instead of becoming a guessed
number: a null says "unknown", while a zero would claim "free".
"""

from __future__ import annotations

import math

UNAVAILABLE_COST = {
    "opened_at": None,
    "held_hours": None,
    "funding": None,
    "funding_currency": None,
    "round_trip_fee": None,
    "round_trip_fee_currency": None,
}


def _finite_number(value) -> float | None:
    """A finite float or None; venue payloads and config values are untrusted."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def attach_position_costs(futures: dict, config: dict) -> None:
    """Attach the notional-currency round-trip fee to each futures position.

    The fee needs only the notional the snapshot reported and the configured
    round-trip rate (the same number profit_protection places the break-even
    stop at; it must not be doubled). Positions whose notional or notional
    currency is unproven keep ``None`` for both fields.
    """
    positions = futures.get("positions")
    if not isinstance(positions, list) or not positions:
        return
    rate = _finite_number(config.get("risk_derivatives_round_trip_fee_rate", 0.001))
    if rate is not None and rate < 0:
        rate = None
    for position in positions:
        if not isinstance(position, dict):
            continue
        position.update(UNAVAILABLE_COST)
        size = _finite_number(position.get("notional"))
        currency = position.get("notional_currency")
        if rate is None or size is None or not isinstance(currency, str) or not currency.strip():
            continue
        position["round_trip_fee"] = abs(size) * rate
        position["round_trip_fee_currency"] = currency
