"""PostgreSQL persistence for the outcome ledger.

One concern per the approved seam: durable, idempotent writes of the
attribution chain (position -> fills -> exits -> funding), window coverage
rows, the worker's restart cursor, and read-only aggregates. Writes are
``INSERT ... ON CONFLICT DO NOTHING`` against the migration-0008 uniqueness
keys, so re-reading the same venue window changes no total. There is no
backfill and no rewrite of existing rows: ingestion starts when the worker's
ingest flag is first enabled.

``account_id`` is the logical venue-account identity string built from the
entry journal's marker (``exchange:profile:identity_hash``); it is never
guessed from timing or price.
"""

from __future__ import annotations

import json as _json_module
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from server import db
from tradingagents.outcomes.netting import REALIZED_PNL_EXCLUDES_FEE

# The worker's restart-catch-up cursor row key in outcome_ledger_state.
LEDGER_CURSOR_KEY = "outcome_ledger"
DEFAULT_BATCH_SIZE = 50

# Terminal futures_cycles statuses the ledger scans. Must stay identical to
# job_store._FUTURES_TERMINAL_STATUSES; tests/test_outcome_ledger_worker.py
# pins the two lists together so a drift breaks a test, not the funnel.
TERMINAL_CYCLE_STATUSES = ("completed", "rejected", "failed", "interrupted")


def _json(value: Any) -> str:
    return _json_module.dumps(value, default=str)


def _uuid() -> str:
    return str(uuid.uuid4())


def record_outcome(
    *,
    account_id: str,
    position: dict[str, Any],
    fills: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    funding: list[dict[str, Any]],
) -> dict[str, int]:
    """Insert one position with its fills/exits/funding; idempotent, atomic.

    Returns the new-vs-duplicate counts so the worker can report coverage
    honestly. A replayed ingest of the same window returns all-duplicates and
    leaves every total unchanged.
    """
    with db.session() as conn:
        position_row = conn.execute(
            """INSERT INTO outcome_positions
               (outcome_id, account_id, cycle_id, client_order_id, venue_order_id,
                symbol, market_type, side, planned_quantity, filled_quantity,
                opened_at, closed_at, attribution, strategy_version, provenance)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (account_id, client_order_id) DO NOTHING
               RETURNING outcome_id""",
            (
                _uuid(), account_id, position.get("cycle_id"),
                position["client_order_id"], position.get("venue_order_id"),
                position["symbol"], position["market_type"], position["side"],
                position.get("planned_quantity"), position["filled_quantity"],
                position.get("opened_at"), position.get("closed_at"),
                position["attribution"], position.get("strategy_version"),
                _json(position.get("provenance") or {}),
            ),
        ).fetchone()
        if position_row is None:
            outcome_row = conn.execute(
                """SELECT outcome_id FROM outcome_positions
                   WHERE account_id = %s AND client_order_id = %s""",
                (account_id, position["client_order_id"]),
            ).fetchone()
            positions_new = 0
        else:
            outcome_row = position_row
            positions_new = 1
        outcome_id = outcome_row["outcome_id"]

        fills_new = fills_duplicate = 0
        for fill in fills:
            cursor = conn.execute(
                """INSERT INTO outcome_fills
                   (fill_id, account_id, outcome_id, venue_trade_id, venue_order_id,
                    symbol, side, role, quantity, price, fee, fee_currency,
                    realized_pnl, realized_pnl_currency, realized_pnl_semantics,
                    occurred_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, venue_trade_id) DO NOTHING""",
                (
                    _uuid(), account_id, outcome_id, fill["venue_trade_id"],
                    fill.get("venue_order_id"), fill["symbol"], fill["side"],
                    fill["role"], fill["quantity"], fill["price"],
                    fill.get("fee"), fill.get("fee_currency"),
                    fill.get("realized_pnl"), fill.get("realized_pnl_currency"),
                    fill.get("realized_pnl_semantics"),
                    fill["occurred_at"],
                ),
            )
            if cursor.rowcount == 1:
                fills_new += 1
            else:
                fills_duplicate += 1

        exits_new = 0
        for exit_row in exits:
            cursor = conn.execute(
                """INSERT INTO outcome_exits
                   (exit_id, account_id, outcome_id, leg, exit_reason, quantity,
                    price, net_per_currency, r_multiple, closed_at, provenance)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                   ON CONFLICT (account_id, outcome_id, leg, price, quantity, closed_at)
                   DO NOTHING""",
                (
                    _uuid(), account_id, outcome_id, exit_row["leg"],
                    exit_row.get("exit_reason"), exit_row["quantity"],
                    exit_row["price"], _json(exit_row.get("net_per_currency") or []),
                    exit_row.get("r_multiple"), exit_row.get("closed_at"),
                    _json(exit_row.get("provenance") or {}),
                ),
            )
            exits_new += cursor.rowcount

        funding_new = 0
        for event in funding:
            cursor = conn.execute(
                """INSERT INTO outcome_funding
                   (funding_id, account_id, outcome_id, funding_at, amount,
                    currency, venue_semantics)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, outcome_id, funding_at, currency)
                   DO NOTHING""",
                (
                    _uuid(), account_id, outcome_id, event["funding_at"],
                    event["amount"], event["currency"], event["venue_semantics"],
                ),
            )
            funding_new += cursor.rowcount

    return {
        "positions_new": positions_new,
        "fills_new": fills_new,
        "fills_duplicate": fills_duplicate,
        "exits_new": exits_new,
        "funding_new": funding_new,
    }


def upsert_coverage(row: dict[str, Any]) -> None:
    """Insert or refresh one coverage row keyed by (account, window)."""
    with db.session() as conn:
        conn.execute(
            """INSERT INTO outcome_coverage
               (account_id, window_start, window_end, fills_found,
                ledger_realized_total, venue_realized_total, realized_diff,
                realized_diff_ratio, status, warnings,
                unattributed_manual_detected, partial_entry_count, snapshot,
                updated_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s,
                       %s::jsonb, %s, %s, %s::jsonb, %s)
               ON CONFLICT (account_id, window_start, window_end) DO UPDATE SET
                   fills_found = EXCLUDED.fills_found,
                   ledger_realized_total = EXCLUDED.ledger_realized_total,
                   venue_realized_total = EXCLUDED.venue_realized_total,
                   realized_diff = EXCLUDED.realized_diff,
                   realized_diff_ratio = EXCLUDED.realized_diff_ratio,
                   status = EXCLUDED.status,
                   warnings = EXCLUDED.warnings,
                   unattributed_manual_detected = EXCLUDED.unattributed_manual_detected,
                   partial_entry_count = EXCLUDED.partial_entry_count,
                   snapshot = EXCLUDED.snapshot,
                   updated_at = EXCLUDED.updated_at""",
            (
                row["account_id"], row["window_start"], row["window_end"],
                row["fills_found"], _json(row["ledger_realized_total"]),
                _json(row["venue_realized_total"]), _json(row["realized_diff"]),
                row.get("realized_diff_ratio"), row["status"],
                _json(row.get("warnings") or []),
                row.get("unattributed_manual_detected", 0),
                row.get("partial_entry_count", 0),
                _json(row.get("snapshot") or {}), datetime.now(UTC).isoformat(),
            ),
        )


def get_cursor(key: str = LEDGER_CURSOR_KEY) -> str | None:
    """The worker's persisted cursor value, or None before the first run."""
    with db.read() as conn:
        row = conn.execute(
            "SELECT cursor_value FROM outcome_ledger_state WHERE state_key = %s",
            (key,),
        ).fetchone()
    return row["cursor_value"] if row is not None else None


def set_cursor(value: str, key: str = LEDGER_CURSOR_KEY) -> None:
    """Persist the cursor so a restart catches up from where it left off."""
    with db.session() as conn:
        conn.execute(
            """INSERT INTO outcome_ledger_state (state_key, cursor_value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (state_key) DO UPDATE SET
                   cursor_value = EXCLUDED.cursor_value,
                   updated_at = EXCLUDED.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )


def get_state(key: str) -> str | None:
    """A worker bookkeeping value (e.g. a per-cycle retry counter)."""
    return get_cursor(key)


def set_state(value: str, key: str) -> None:
    """Persist one worker bookkeeping value under an explicit key."""
    set_cursor(value, key=key)


def clear_state(key: str) -> None:
    """Drop one worker bookkeeping value (e.g. after a successful retry)."""
    with db.session() as conn:
        conn.execute(
            "DELETE FROM outcome_ledger_state WHERE state_key = %s", (key,),
        )


def terminal_cycles_after(
    cursor_updated_at: str | None,
    cursor_cycle_id: str | None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """The next bounded batch of terminal cycles in stable (updated_at, id) order.

    The cursor is the last processed (updated_at, cycle_id) pair, so a restart
    resumes exactly where it stopped: nothing reprocessed, nothing skipped,
    whatever batch size the caller bounds. This is the durable pagination the
    plan requires — never a ``LIMIT``-from-top read that silently drops rows.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    query = (
        "SELECT cycle_id, symbol, four_hour_open, one_hour_open, strategy_version, "
        "status, phase, payload, created_at, updated_at, finished_at "
        "FROM futures_cycles WHERE status IN (" +
        ", ".join(f"'{status}'" for status in TERMINAL_CYCLE_STATUSES) + ")"
    )
    params: list[Any] = []
    if cursor_updated_at is not None:
        query += " AND (updated_at, cycle_id) > (%s, %s)"
        params.extend([cursor_updated_at, cursor_cycle_id or ""])
    query += " ORDER BY updated_at ASC, cycle_id ASC LIMIT %s"
    params.append(batch_size)
    with db.read() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def find_journal_tasks(symbol: str, venue_order_id: str | None = None) -> list[dict[str, Any]]:
    """Read-only attribution evidence from the durable entry journal.

    Matches the entry-intent rows of one symbol; with ``venue_order_id`` given,
    only tasks whose ``placed_ids`` contain that venue order id (the recovery
    path records the ids it found there). The journal is never modified here.
    """
    query = (
        "SELECT task_id, symbol, entry_order_id, close_side, quantity, legs, "
        "placed_ids, status, venue_key, account_identity_exchange, "
        "account_identity_profile, account_identity_type, account_identity_hash "
        "FROM bracket_recovery_tasks WHERE symbol = %s"
    )
    params: list[Any] = [symbol]
    if venue_order_id is not None:
        query += " AND placed_ids @> %s::jsonb"
        params.append(_json([venue_order_id]))
    with db.read() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def find_bracket_task(symbol: str, entry_order_id: str) -> dict[str, Any] | None:
    """The position's bracket task — the durable EXIT evidence.

    Bracket tasks are keyed ``bracket:{symbol}:{entry_order_id}`` and their
    ``placed_ids`` / per-leg ``order_id`` values hold the stop, target and
    scale-out tranche order ids as the runtime places them (brackets._save,
    scale_out_apply). Read-only; the task is never modified here.
    """
    with db.read() as conn:
        row = conn.execute(
            """SELECT task_id, symbol, entry_order_id, close_side, quantity, legs,
                      placed_ids, status, venue_key, account_identity_exchange,
                      account_identity_profile, account_identity_type,
                      account_identity_hash
               FROM bracket_recovery_tasks
               WHERE symbol = %s AND entry_order_id = %s AND task_id LIKE 'bracket:%%'
               LIMIT 1""",
            (symbol, entry_order_id),
        ).fetchone()
    return dict(row) if row is not None else None


def _fetch_component_totals(conn: Any, column: str, currency_column: str) -> dict:
    """Per-currency sums of one fill component; a missing currency keeps its
    own ``None`` bucket instead of being silently dropped from the totals."""
    rows = conn.execute(
        f"""SELECT {currency_column} AS currency, SUM({column}) AS total
            FROM outcome_fills WHERE {column} IS NOT NULL
            GROUP BY {currency_column}"""
    ).fetchall()
    return {row["currency"]: str(row["total"]) for row in rows}


def summary(window_start: str | None = None, window_end: str | None = None) -> dict[str, Any]:
    """Read-only ledger aggregate; an empty ledger reports unavailable, not zero.

    Totals are per currency, computed from the stored numeric columns. The
    net figure folds realized - fee + signed funding per currency through the
    pure outcomes layer, with two honesty gates:

    - fees are subtracted only when every realized-PnL fill of that currency
      carries ``realized_pnl_semantics`` saying the venue's realized value
      EXCLUDES the fee (ingestion records the contract per plan §2.2); mixed
      or missing semantics flags ``fee_known: false`` and subtracts nothing;
    - funding is folded only for currencies where funding rows actually
      exist; currencies with realized PnL but no funding rows are listed in
      ``funding_unknown_currencies`` and their net is not claimed
      (``funding_known: false``).

    ``window_start``/``window_end`` (optional ISO timestamps) filter the
    coverage rows to those intersecting the window. Coverage rows are
    PER-CYCLE reconciliation snapshots: they overlap by construction and must
    never be summed — each row reconciles one cycle's own fill window.
    When nothing has been ingested yet, ``status`` is ``unavailable`` with
    the reason attached — a missing ledger is never presented as zero PnL.
    """
    from tradingagents.outcomes.currency import sum_components, to_json

    with db.read() as conn:
        position_row = conn.execute(
            """SELECT COUNT(*) AS positions,
                      COUNT(*) FILTER (WHERE closed_at IS NOT NULL) AS closed,
                      MIN(opened_at) AS first_opened, MAX(closed_at) AS last_closed
               FROM outcome_positions"""
        ).fetchone()
        fill_row = conn.execute("SELECT COUNT(*) AS fills FROM outcome_fills").fetchone()
        realized = _fetch_component_totals(conn, "realized_pnl", "realized_pnl_currency")
        fees = _fetch_component_totals(conn, "fee", "fee_currency")
        semantics_rows = conn.execute(
            """SELECT realized_pnl_currency AS currency, realized_pnl_semantics AS semantics,
                      COUNT(*) AS fills
               FROM outcome_fills
               WHERE realized_pnl IS NOT NULL
               GROUP BY realized_pnl_currency, realized_pnl_semantics"""
        ).fetchall()
        funding_rows = conn.execute(
            """SELECT currency,
                      SUM(amount) FILTER (WHERE amount >= 0) AS received,
                      SUM(amount) FILTER (WHERE amount < 0) AS paid,
                      COUNT(*) AS events,
                      MIN(venue_semantics) AS venue_semantics
               FROM outcome_funding GROUP BY currency"""
        ).fetchall()
        coverage_rows = conn.execute(
            """SELECT account_id, window_start, window_end, fills_found,
                      ledger_realized_total, venue_realized_total, realized_diff,
                      realized_diff_ratio, status, warnings,
                      unattributed_manual_detected, partial_entry_count, snapshot
               FROM outcome_coverage
               ORDER BY window_end DESC LIMIT 20"""
        ).fetchall()

    # Fee honesty per currency: exactly one recorded semantics saying the
    # venue's realized value excludes the fee, else the fee is not claimed.
    fee_known_currencies = {
        row["currency"]
        for row in semantics_rows
        if row["semantics"] == REALIZED_PNL_EXCLUDES_FEE
    }
    currencies_with_realized = {
        row["currency"] for row in semantics_rows if row["currency"] is not None
    }
    fee_unknown_currencies = sorted(currencies_with_realized - fee_known_currencies)

    positions = int(position_row["positions"] or 0)
    fills = int(fill_row["fills"] or 0)
    selected_coverage = _filter_coverage(coverage_rows, window_start, window_end)
    if positions == 0 and fills == 0 and not selected_coverage:
        return {
            "status": "unavailable",
            "reason": "no outcome data has been ingested yet; the ledger is empty, "
                      "which is not zero PnL",
            "positions": None,
            "fills": None,
            "totals": None,
            "funding_unknown_currencies": [],
            "fee_unknown_currencies": [],
            "coverage": [],
        }

    funding: dict[str | None, Any] = {}
    for row in funding_rows:
        # Rows exist for this currency, so an empty sign bucket is a genuine
        # zero (every row the venue returned is held), not a missing value.
        funding[row["currency"]] = {
            "received": str(row["received"]) if row["received"] is not None else "0",
            "paid": str(row["paid"]) if row["paid"] is not None else "0",
            "events": int(row["events"]),
            "venue_semantics": row["venue_semantics"],
        }

    def bucket(pairs: dict[str | None, str | None]) -> dict[str | None, Any]:
        return {
            currency: None if amount is None else Decimal(amount)
            for currency, amount in pairs.items()
        }

    realized_bucket = bucket(realized)
    fee_bucket = bucket(fees)
    # Funding amounts already carry the venue sign recorded at ingest time
    # (positive = received); received + paid is the signed window total.
    funding_bucket = {
        currency: Decimal(totals["received"]) + Decimal(totals["paid"])
        for currency, totals in funding.items()
    }
    # Fees fold into the net only where the recorded semantics allow it;
    # elsewhere the fee component is OMITTED (not None-poisoned): the net is
    # realized + funding with fee_known=false carrying the honesty.
    fee_fold = {
        currency: -amount
        for currency, amount in fee_bucket.items()
        if currency in fee_known_currencies and amount is not None
    }
    net = sum_components([realized_bucket, fee_fold, funding_bucket])
    # A currency that holds realized or fee money but no funding rows cannot
    # have a net claimed for it: its funding read may simply have failed.
    funding_unknown_currencies = sorted(
        currency
        for currency in set(realized) | set(fees)
        if currency not in funding
    )
    net_rows = to_json(net)
    for row in net_rows:
        row["funding_known"] = row["currency"] in funding
        row["fee_known"] = (
            row["currency"] in fee_known_currencies
            or row["currency"] not in currencies_with_realized
        )
    return {
        "status": "partial",
        "positions": {
            "total": positions,
            "closed": int(position_row["closed"] or 0),
            "first_opened": position_row["first_opened"],
            "last_closed": position_row["last_closed"],
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
        "coverage": [dict(row) for row in selected_coverage],
    }


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
