"""Currency buckets for the outcome ledger.

The ledger never converts between currencies: venue amounts are kept verbatim
with their own currency, totals are aggregated per bucket, and any attempt to
collapse different buckets into one number is refused. A missing amount or a
missing currency stays ``None`` — a null means "unknown"; a zero would claim
"free" or "no fee" without evidence (same contract as server.position_cost).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# JSONB wire format for currency components: Decimal exactness travels as text.
_MISSING = None


@dataclass(frozen=True)
class CurrencyAmount:
    """One venue amount and the currency it was reported in."""

    currency: str | None
    amount: Decimal | None


def unknown() -> CurrencyAmount:
    """An amount that is simply not known (kept None, never a zero)."""
    return CurrencyAmount(currency=None, amount=None)


def add_to_bucket(
    bucket: dict[str | None, Decimal | None],
    amount: CurrencyAmount,
) -> dict[str | None, Decimal | None]:
    """Fold one amount into a per-currency bucket, in place and returned.

    ``None`` poisons its bucket: once one value in a currency is unknown, the
    bucket's total for that currency is unknown too. A missing currency label
    keeps the amount under the ``None`` bucket instead of inventing a currency.
    """
    if amount.amount is None:
        bucket[amount.currency] = None
        return bucket
    if amount.currency not in bucket:
        bucket[amount.currency] = Decimal(0)
    current = bucket[amount.currency]
    if current is None:
        return bucket
    bucket[amount.currency] = current + amount.amount
    return bucket


def sum_components(
    components: list[dict[str | None, Decimal | None]],
) -> dict[str | None, Decimal | None]:
    """Add per-currency buckets together; unknown poisons per currency."""
    total: dict[str | None, Decimal | None] = {}
    for component in components:
        for currency, amount in component.items():
            add_to_bucket(total, CurrencyAmount(currency, amount))
    return total


class MixedCurrencySumError(ValueError):
    """A single-currency total was requested but another bucket held money."""


def single_currency_total(
    bucket: dict[str | None, Decimal | None],
    currency: str,
) -> Decimal | None:
    """The bucket total for ``currency``, or None when unknown.

    Refuses instead of silently dropping money in any other currency bucket:
    a mixed-currency total presented as one number would be fabricated.
    """
    total = bucket.get(currency, _MISSING)
    for other, amount in bucket.items():
        if other == currency:
            continue
        # An unknown-currency bucket holding a real number is money too: a
        # single-currency total must refuse it, not drop it silently.
        if other is None:
            if amount is not None and amount != 0:
                raise MixedCurrencySumError(
                    f"cannot sum into {currency}: a bucket with unknown currency holds {amount}"
                )
            continue
        if amount is not None and amount != 0:
            raise MixedCurrencySumError(
                f"cannot sum mixed currencies: {other} holds {amount}"
            )
    return total


def to_json(bucket: dict[str | None, Decimal | None]) -> list[dict[str, Any]]:
    """JSONB wire shape: [{"currency": "USDT"|"null", "amount": "1.23"|"null"}]."""
    return [
        {
            "currency": currency,
            "amount": None if amount is None else str(amount),
        }
        for currency, amount in sorted(
            bucket.items(), key=lambda item: (item[0] is None, item[0] or "")
        )
    ]


def from_json(rows: Any) -> dict[str | None, Decimal | None]:
    """Rebuild a bucket from its JSONB wire shape; malformed rows are skipped."""
    bucket: dict[str | None, Decimal | None] = {}
    if not isinstance(rows, list):
        return bucket
    for row in rows:
        if not isinstance(row, dict) or "currency" not in row or "amount" not in row:
            continue
        currency = row["currency"]
        raw = row["amount"]
        if currency is not None and not isinstance(currency, str):
            continue
        if raw is None:
            bucket[currency] = None
            continue
        try:
            bucket[currency] = Decimal(str(raw))
        except (ArithmeticError, ValueError):
            continue
    return bucket
