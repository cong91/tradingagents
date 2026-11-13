"""Results projection for the read-only operating brief.

This module composes the durable outcome-ledger summary with the portfolio's
unreconciled figures. The ledger payload is passed through unchanged so new
reporting evidence, including closure evidence summaries, remains available to
the brief without a second projection contract.
"""

from __future__ import annotations

import logging
from typing import Any

from server import outcome_report

logger = logging.getLogger(__name__)


def _estimated_results(portfolio_body: dict[str, Any] | None) -> dict[str, Any]:
    """Figures that are NOT reconciled against the venue — labelled as such.

    The Spot realized number is the audit-log FIFO replay the portfolio card
    already shows; the futures unrealized number is what the venue last
    reported for open positions. Both are estimates by contract
    (docs/ui-api-contract.md §6), so they never sit in the verified block.
    """
    body = portfolio_body or {}
    realized = body.get("realized_pnl")
    realized = realized if isinstance(realized, dict) else None
    futures = body.get("futures")
    futures = futures if isinstance(futures, dict) else None
    futures_ok = futures is not None and futures.get("status") == "ok"
    return {
        "spot_audit_realized_total": (realized or {}).get("total"),
        "spot_audit_realized_today": (realized or {}).get("today"),
        "spot_currency": body.get("currency"),
        "spot_closed_trades": (realized or {}).get("closed_trades"),
        "spot_unknown_basis_closes": (realized or {}).get("unknown_basis_closes"),
        "futures_unrealized": (futures or {}).get("unrealized_pnl") if futures_ok else None,
        "futures_unrealized_currency": (
            (futures or {}).get("unrealized_pnl_currency") if futures_ok else None
        ),
    }


def _unattributed_from_coverage(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Manual/unlinked fills, kept per coverage row with its own window.

    Coverage rows reconcile one cycle's fill window each and overlap by
    construction, so their counts are reported per row and never summed into a
    single headline number (see ``coverage_note``).
    """
    rows: list[dict[str, Any]] = []
    for row in coverage:
        manual = row.get("unattributed_manual_detected")
        if manual:
            rows.append({
                "window_start": row.get("window_start"),
                "window_end": row.get("window_end"),
                "count": manual,
                "account_id": row.get("account_id"),
            })
    return rows


def _results_section(portfolio_body: dict[str, Any] | None) -> dict[str, Any]:
    try:
        ledger = outcome_report.summary()
    except Exception as exc:
        logger.warning("brief: outcome ledger unreadable: %r", exc)
        ledger = {"status": "unavailable", "reason": (
            f"the outcome ledger could not be read: {type(exc).__name__}"
        )}
    coverage = ledger.get("coverage") if isinstance(ledger.get("coverage"), list) else []
    return {
        "ledger": ledger,
        "estimated": _estimated_results(portfolio_body),
        "unattributed_manual": _unattributed_from_coverage(coverage),
        "coverage_note": (
            "Each coverage row reconciles one cycle's own fill window; rows overlap "
            "and must never be summed into one total."
        ),
        "ledger_note": (
            "Totals are per currency and never FX-converted. A currency whose funding "
            "rows are missing has no net claimed for it."
        ),
    }
