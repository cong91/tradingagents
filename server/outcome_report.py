"""Read-only outcome-ledger reporting: the aggregate behind GET /api/outcomes.

One concern: turn the durable ledger rows into the read-only summary the API
and the operating brief expose. No writes, no venue calls, no ingestion — the
store owns persistence (outcome_store.py); this module only reads and shapes.

An empty ledger reports ``unavailable`` with the reason, never a fabricated
zero. Component totals preserve every currency and expose recorded amounts
only. Funding rows do not prove the funding read was complete: no durable
funding coverage is currently stored, so net remains unknown even when one or
more funding events were recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server import db
from tradingagents.outcomes.currency import to_json
from tradingagents.outcomes.netting import REALIZED_PNL_EXCLUDES_FEE


def summary(window_start: str | None = None, window_end: str | None = None) -> dict[str, Any]:
    """The ledger aggregate over positions, fills, funding and coverage.

    ``window_start``/``window_end`` (optional ISO timestamps) filter the
    coverage rows to those intersecting the window and filter unattributed
    funding observations by event time. Coverage rows are PER-CYCLE
    reconciliation snapshots: they overlap by construction and must never be
    summed — each row reconciles one cycle's own fill window. When nothing has
    been ingested yet, ``status`` is ``unavailable`` with the reason attached —
    a missing ledger is never presented as zero PnL.
    """
    start = _parse_window_bound(window_start)
    end = _parse_window_bound(window_end)
    with db.read() as conn:
        position_row = conn.execute(
            """SELECT COUNT(*) AS positions,
                      COUNT(*) FILTER (
                          WHERE closed_at IS NOT NULL
                            AND provenance->>'closure_proof_version' = '2'
                      ) AS verified_closed,
                      COUNT(*) FILTER (
                          WHERE COALESCE(closed_at, NULLIF(provenance->>'recorded_closed_at', '')::timestamptz)
                                IS NOT NULL
                      ) AS recorded_closed,
                      MIN(opened_at) AS first_opened,
                      MAX(COALESCE(
                          closed_at,
                          NULLIF(provenance->>'recorded_closed_at', '')::timestamptz
                      )) AS last_recorded_closed,
                      MAX(COALESCE(
                          closed_at,
                          NULLIF(provenance->>'recorded_closed_at', '')::timestamptz
                      )) FILTER (
                          WHERE provenance->>'closure_proof_version' = '2'
                      ) AS last_verified_closed
               FROM outcome_positions"""
        ).fetchone()
        fill_row = conn.execute("SELECT COUNT(*) AS fills FROM outcome_fills").fetchone()
        realized = _fetch_component_totals(conn, "realized_pnl", "realized_pnl_currency")
        fees = _fetch_component_totals(conn, "fee", "fee_currency")
        fee_coverage_rows = conn.execute(
            """SELECT account_id, outcome_id, realized_pnl_currency, fee_currency,
                      fee, realized_pnl, realized_pnl_semantics
               FROM outcome_fills"""
        ).fetchall()
        funding_rows = conn.execute(
            """SELECT currency,
                      SUM(amount) FILTER (WHERE amount >= 0) AS received,
                      SUM(amount) FILTER (WHERE amount < 0) AS paid,
                      COUNT(*) AS events,
                      MIN(venue_semantics) AS venue_semantics
               FROM outcome_funding GROUP BY currency"""
        ).fetchall()
        funding_position_rows = conn.execute(
            "SELECT DISTINCT account_id, outcome_id, currency FROM outcome_funding"
        ).fetchall()
        observation_query = (
            "SELECT account_id, venue_key, account_type, symbol, venue_income_id, "
            "funding_at, amount, currency, venue_semantics FROM outcome_funding_observations WHERE TRUE"
        )
        observation_params: list[Any] = []
        if start is not None:
            observation_query += " AND funding_at >= %s"
            observation_params.append(start)
        if end is not None:
            observation_query += " AND funding_at < %s"
            observation_params.append(end)
        observation_query += " ORDER BY funding_at DESC, account_id, symbol, venue_income_id LIMIT 101"
        observation_rows = conn.execute(observation_query, tuple(observation_params)).fetchall()
        coverage_rows = conn.execute(
            """SELECT account_id, window_start, window_end, fills_found,
                      ledger_realized_total, venue_realized_total, realized_diff,
                      realized_diff_ratio, status, warnings,
                      unattributed_manual_detected, partial_entry_count, snapshot
               FROM outcome_coverage
               ORDER BY window_end DESC LIMIT 20"""
        ).fetchall()

    # Semantics belong to a position/account, not the global fee currency:
    # entry fees can be in a different asset from exit realized P&L.
    position_currencies: dict[tuple[Any, Any], set[str | None]] = {}
    position_fee_complete: dict[tuple[Any, Any], bool] = {}
    for row in fee_coverage_rows:
        key = (row["account_id"], row["outcome_id"])
        currencies = position_currencies.setdefault(key, set())
        if row["realized_pnl"] is not None:
            currencies.add(row["realized_pnl_currency"])
        if row["fee"] is not None or row["fee_currency"] is not None:
            currencies.add(row["fee_currency"])
        fee_complete = row["fee"] is not None and row["fee_currency"] is not None
        if row["realized_pnl"] is not None:
            fee_complete = fee_complete and row["realized_pnl_semantics"] == REALIZED_PNL_EXCLUDES_FEE
        position_fee_complete[key] = position_fee_complete.get(key, True) and fee_complete

    for row in funding_position_rows:
        key = (row["account_id"], row["outcome_id"])
        position_currencies.setdefault(key, set()).add(row["currency"])
        position_fee_complete.setdefault(key, False)

    fee_unknown = {
        currency
        for key, currencies in position_currencies.items()
        if not position_fee_complete[key]
        for currency in currencies
    }
    fee_unknown_currencies = sorted(fee_unknown, key=lambda currency: (currency is None, currency or ""))

    positions = int(position_row["positions"] or 0)
    fills = int(fill_row["fills"] or 0)
    selected_coverage = _filter_coverage(coverage_rows, window_start, window_end)
    observations = {
        "status": "partial",
        "events": [{**dict(row), "amount": str(row["amount"])} for row in observation_rows[:100]],
        "truncated": len(observation_rows) > 100,
    }
    if positions == 0 and fills == 0 and not selected_coverage and not observation_rows:
        return {
            "status": "unavailable",
            "reason": "no outcome data has been ingested yet; the ledger is empty, "
                      "which is not zero PnL",
            "positions": None,
            "fills": None,
            "totals": None,
            "funding_unknown_currencies": [],
            "fee_unknown_currencies": [],
            "funding_observations": observations,
            "coverage": [],
        }

    funding: dict[str | None, Any] = {}
    for row in funding_rows:
        # These are sums of recorded events, not proof that the read covered
        # every event; absent signs are zero only within the recorded subset.
        funding[row["currency"]] = {
            "received": str(row["received"]) if row["received"] is not None else "0",
            "paid": str(row["paid"]) if row["paid"] is not None else "0",
            "events": int(row["events"]),
            "venue_semantics": row["venue_semantics"],
        }

    # A position with a missing fee can have no monetary currency bucket yet.
    # Keep its unknown currency visible instead of manufacturing a zero.
    currencies = set(realized) | set(fees) | set(funding) | fee_unknown
    if any(not complete and not position_currencies[key]
           for key, complete in position_fee_complete.items()):
        currencies.add(None)
        fee_unknown.add(None)
        fee_unknown_currencies = sorted(
            fee_unknown, key=lambda currency: (currency is None, currency or "")
        )
    # None-valued net rows are deliberate: the current schema has no durable
    # evidence proving funding coverage for the aggregated positions.
    net_rows = to_json(dict.fromkeys(currencies))
    for row in net_rows:
        row["funding_known"] = False
        row["fee_known"] = row["currency"] is not None and row["currency"] not in fee_unknown
    funding_unknown_currencies = [row["currency"] for row in net_rows]
    return {
        "status": "partial",
        "positions": {
            "total": positions,
            # `closed` is retained as a recorded timestamp count for API
            # compatibility. Verified closure is exposed separately below.
            "closed": int(position_row["recorded_closed"] or 0),
            "verified_closed": int(position_row["verified_closed"] or 0),
            "recorded_closed": int(position_row["recorded_closed"] or 0),
            "first_opened": position_row["first_opened"],
            "last_closed": position_row["last_verified_closed"],
            "last_recorded_closed": position_row["last_recorded_closed"],
            "closure_evidence": {
                "status": (
                    "complete"
                    if int(position_row["positions"] or 0)
                    == int(position_row["verified_closed"] or 0)
                    else "partial"
                ),
                "verified_closed": int(position_row["verified_closed"] or 0),
                "unverified_positions": max(
                    0,
                    int(position_row["positions"] or 0)
                    - int(position_row["verified_closed"] or 0),
                ),
                "unverified_closed_timestamps": max(
                    0,
                    int(position_row["recorded_closed"] or 0)
                    - int(position_row["verified_closed"] or 0),
                ),
                "reason": (
                    "Closure is verified only against complete, identity-bound stored fill history."
                    if int(position_row["positions"] or 0)
                    != int(position_row["verified_closed"] or 0)
                    else None
                ),
            },
        },
        "fills": fills,
        "totals": {
            "realized_by_currency": realized,
            "fee_by_currency": fees,
            "funding_by_currency": funding,
            "net_by_currency": net_rows,
        },
        "funding_unknown_currencies": funding_unknown_currencies,
        "fee_unknown_currencies": fee_unknown_currencies,
        "funding_observations": observations,
        "coverage": [dict(row) for row in selected_coverage],
    }


def _fetch_component_totals(conn: Any, column: str, currency_column: str) -> dict:
    """Per-currency sums of one fill component; a missing currency keeps its
    own ``None`` bucket instead of being silently dropped from the totals."""
    rows = conn.execute(
        f"""SELECT {currency_column} AS currency, SUM({column}) AS total
            FROM outcome_fills WHERE {column} IS NOT NULL
            GROUP BY {currency_column}"""
    ).fetchall()
    return {row["currency"]: str(row["total"]) for row in rows}


def _filter_coverage(rows: list[dict[str, Any]], window_start: str | None,
                     window_end: str | None) -> list[dict[str, Any]]:
    """Keep coverage rows intersecting [window_start, window_end) when given.

    Malformed filter values are refused (400-grade input), never silently
    treated as "no filter": a typo'd timestamp narrowing to nothing would
    otherwise read as an empty ledger.
    """
    if window_start is None and window_end is None:
        return [dict(row) for row in rows]
    start = _parse_window_bound(window_start)
    end = _parse_window_bound(window_end)
    selected: list[dict[str, Any]] = []
    for row in rows:
        row_start = _parse_window_bound(row.get("window_start"))
        row_end = _parse_window_bound(row.get("window_end"))
        if row_start is None or row_end is None:
            continue
        if start is not None and row_end <= start:
            continue
        if end is not None and row_start >= end:
            continue
        selected.append(dict(row))
    return selected


def _parse_window_bound(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        if value is None:
            return None
        raise ValueError(f"invalid window bound: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid window bound: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
