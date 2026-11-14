"""Window reconciliation between the ledger and the venue's own totals.

Coverage answers "how much of this window does the ledger actually cover" and
publishes the error instead of hiding it: the venue's realized total (all
fills the venue reports, bot or not) against the ledger's attributed total.
A non-empty difference is evidence — of manual trades, of unattributed fills,
of venue-side omissions — and it must be published, never netted away.

Status vocabulary mirrors FuturesTradeHistory.status exactly:
'ok' | 'partial' | 'error' | 'unsupported' | 'unavailable'. 'unsupported' is a
real outcome (non-Binance venue), not an error, and must survive to the
coverage row unchanged.
"""

from __future__ import annotations

from decimal import Decimal

from tradingagents.outcomes.currency import (
    CurrencyAmount,
    add_to_bucket,
    sum_components,
    to_json,
)

COVERAGE_STATUSES = ("ok", "partial", "error", "unsupported", "unavailable")


def build_coverage(
    *,
    account_id: str,
    window_start: str,
    window_end: str,
    fills_found: int,
    attributed_fill_count: int,
    venue_realized: list[CurrencyAmount],
    ledger_realized: list[CurrencyAmount],
    unattributed_manual_detected: int,
    partial_entry_count: int,
    source_status: str,
    warnings: list[str],
    snapshot: dict | None = None,
) -> dict:
    """Build one coverage row; totals are computed per currency, never mixed.

    ``source_status`` is the fill source's own status (FuturesTradeHistory
    vocabulary) and is validated against the same set; an unknown status is
    a programming error and raises. ``ledger_realized`` over
    ``venue_realized`` gives the per-currency difference; the ratio is
    relative to the venue total's magnitude and stays None when either side
    is unknown, so a missing denominator cannot fabricate a percentage.
    """
    if source_status not in COVERAGE_STATUSES:
        raise ValueError(f"unknown coverage source status: {source_status!r}")
    if fills_found < 0 or attributed_fill_count < 0:
        raise ValueError("fill counts cannot be negative")
    if attributed_fill_count > fills_found:
        raise ValueError("attributed fills cannot exceed the fills the venue reported")

    venue_bucket: dict[str | None, Decimal | None] = {}
    for amount in venue_realized:
        add_to_bucket(venue_bucket, amount)
    ledger_bucket: dict[str | None, Decimal | None] = {}
    for amount in ledger_realized:
        add_to_bucket(ledger_bucket, amount)
    diff = sum_components([
        ledger_bucket,
        {currency: (-amount if amount is not None else None) for currency, amount in venue_bucket.items()},
    ])

    ratio: Decimal | None = None
    # Only a single-currency difference can carry a meaningful ratio; a mixed
    # or unknown diff stays ratio-less rather than assuming a currency.
    nonzero = [(c, a) for c, a in diff.items() if a is not None and a != 0]
    if len(nonzero) == 1:
        currency, amount = nonzero[0]
        venue_amount = venue_bucket.get(currency)
        if venue_amount is not None and venue_amount != 0 and currency is not None:
            ratio = abs(amount) / abs(venue_amount)

    status = source_status
    if status in ("ok", "partial") and (
        unattributed_manual_detected or attributed_fill_count != fills_found or warnings
    ):
        status = "partial"

    return {
        "account_id": account_id,
        "window_start": window_start,
        "window_end": window_end,
        "fills_found": fills_found,
        "attributed_fill_count": attributed_fill_count,
        "ledger_realized_total": to_json(ledger_bucket),
        "venue_realized_total": to_json(venue_bucket),
        "realized_diff": to_json(diff),
        "realized_diff_ratio": ratio,
        "status": status,
        "warnings": list(warnings),
        "unattributed_manual_detected": unattributed_manual_detected,
        "partial_entry_count": partial_entry_count,
        "snapshot": dict(snapshot or {}),
    }
