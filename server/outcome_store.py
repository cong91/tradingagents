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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from server import db, outcome_poll_lock
from server.outcome_closure import verified_closure

# The worker's restart-catch-up cursor row key in outcome_ledger_state.
LEDGER_CURSOR_KEY = "outcome_ledger"
DEFAULT_BATCH_SIZE = 50

# Terminal futures_cycles statuses the ledger scans. Must stay identical to
# futures_cycle_store._FUTURES_TERMINAL_STATUSES; tests/test_outcome_ledger_worker.py
# pins the two lists together so a drift breaks a test, not the funnel.
TERMINAL_CYCLE_STATUSES = ("completed", "rejected", "failed", "interrupted")


def _json(value: Any) -> str:
    return _json_module.dumps(value, default=str)


def _stable_json(value: Any) -> str:
    return _json_module.dumps(value, sort_keys=True, default=str)


def _closure_evidence_current(conn: Any, position: dict[str, Any]) -> bool:
    """Lock and compare the journal evidence captured before the venue read."""
    snapshot = position.get("closure_evidence_snapshot")
    if not isinstance(snapshot, dict):
        return True
    journal = snapshot.get("journal")
    journal_task_id = journal.get("task_id") if isinstance(journal, dict) else None
    current_journal = conn.execute(
        """SELECT task_id, symbol, entry_order_id, close_side, quantity, legs,
                  placed_ids, status, venue_key, account_identity_exchange,
                  account_identity_profile, account_identity_type, account_identity_hash
           FROM bracket_recovery_tasks WHERE task_id = %s FOR UPDATE""",
        (journal_task_id,),
    ).fetchone()
    if current_journal is None:
        return False

    expected_cycle = snapshot.get("cycle")
    cycle = conn.execute(
        "SELECT cycle_id, symbol, payload FROM futures_cycles WHERE cycle_id = %s FOR UPDATE",
        (position.get("cycle_id"),),
    ).fetchone()
    if cycle is None:
        return False
    payload = cycle["payload"] if isinstance(cycle["payload"], dict) else {}
    current_cycle = {
        "cycle_id": cycle["cycle_id"],
        "symbol": cycle["symbol"],
        "order_id": payload.get("order_id"),
        "entry_intent_id": payload.get("entry_intent_id"),
        "entry_provenance": payload.get("entry_provenance"),
    }
    if _stable_json(current_cycle) != _stable_json(expected_cycle):
        return False

    def evidence_row(row: Any) -> dict[str, Any]:
        return {
            "task_id": row["task_id"], "symbol": row["symbol"],
            "entry_order_id": row.get("entry_order_id"), "close_side": row["close_side"],
            "quantity": str(row["quantity"]) if row.get("quantity") is not None else None,
            "legs": row["legs"],
            "placed_ids": sorted(str(item) for item in (row["placed_ids"] or [])),
            "status": row["status"], "venue_key": row["venue_key"],
            "account_identity_exchange": row["account_identity_exchange"],
            "account_identity_profile": row["account_identity_profile"],
            "account_identity_type": row["account_identity_type"],
            "account_identity_hash": row["account_identity_hash"],
        }

    if _stable_json(evidence_row(current_journal)) != _stable_json(journal):
        return False

    bracket = snapshot.get("bracket")
    bracket_task_id = bracket.get("task_id") if isinstance(bracket, dict) else None
    current_bracket = conn.execute(
        """SELECT task_id, symbol, entry_order_id, close_side, legs, placed_ids,
                  status, venue_key, account_identity_exchange,
                  account_identity_profile, account_identity_type, account_identity_hash
           FROM bracket_recovery_tasks WHERE task_id = %s FOR UPDATE""",
        (bracket_task_id,),
    ).fetchone() if bracket_task_id is not None else None
    if (bracket is None) != (current_bracket is None):
        return False
    return bracket is None or _stable_json(evidence_row(current_bracket)) == _stable_json(bracket)


def _uuid() -> str:
    return str(uuid.uuid4())


@contextmanager
def _connection(*, write: bool = False) -> Iterator[Any]:
    """Use the poll owner's session, or an independent session outside a poll."""
    guard = outcome_poll_lock.current_guard()
    if guard is not None:
        with guard.transaction() as conn:
            yield conn
    else:
        with (db.session() if write else db.read()) as conn:
            yield conn


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
    with _connection(write=True) as conn:
        evidence_current = _closure_evidence_current(conn, position)
        if position.get("reconcile") and not fills:
            outcome_row = conn.execute(
                """SELECT outcome_id, symbol, side, market_type, planned_quantity,
                          venue_order_id, cycle_id FROM outcome_positions
                   WHERE account_id = %s AND client_order_id = %s FOR UPDATE""",
                (account_id, position["client_order_id"]),
            ).fetchone()
            if outcome_row is None:
                return {
                    "positions_new": 0, "fills_new": 0, "fills_duplicate": 0,
                    "exits_new": 0, "funding_new": 0,
                }
            positions_new = 0
        else:
            inserted = conn.execute(
                """INSERT INTO outcome_positions
                   (outcome_id, account_id, cycle_id, client_order_id, venue_order_id,
                    symbol, market_type, side, planned_quantity, filled_quantity,
                    opened_at, closed_at, attribution, strategy_version, provenance)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (account_id, client_order_id) DO NOTHING
                   RETURNING outcome_id, symbol, side, market_type, planned_quantity,
                             venue_order_id, cycle_id""",
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
            if inserted is None:
                outcome_row = conn.execute(
                    """SELECT outcome_id, symbol, side, market_type, planned_quantity,
                              venue_order_id, cycle_id FROM outcome_positions
                       WHERE account_id = %s AND client_order_id = %s FOR UPDATE""",
                    (account_id, position["client_order_id"]),
                ).fetchone()
                positions_new = 0
            else:
                outcome_row = inserted
                positions_new = 1
        identity_fields = (
            "symbol", "side", "market_type", "planned_quantity", "venue_order_id", "cycle_id",
        )
        if any(outcome_row[key] != position.get(key) for key in identity_fields):
            raise ValueError("conflicting position identity")
        outcome_id = outcome_row["outcome_id"]

        fills_new = fills_duplicate = 0
        fill_ids = {}
        for fill in fills:
            if fill["symbol"] != position["symbol"]:
                raise ValueError("fill symbol does not match position")
            cursor = conn.execute(
                """INSERT INTO outcome_fills
                   (fill_id, account_id, outcome_id, venue_trade_id, venue_order_id,
                    symbol, side, role, quantity, price, fee, fee_currency,
                    realized_pnl, realized_pnl_currency, realized_pnl_semantics,
                    occurred_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, symbol, venue_trade_id) DO NOTHING
                   RETURNING fill_id""",
                (
                    _uuid(), account_id, outcome_id, fill["venue_trade_id"],
                    fill.get("venue_order_id"), fill["symbol"], fill["side"],
                    fill["role"], fill["quantity"], fill["price"],
                    fill.get("fee"), fill.get("fee_currency"),
                    fill.get("realized_pnl"), fill.get("realized_pnl_currency"),
                    fill.get("realized_pnl_semantics"), fill["occurred_at"],
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                fills_new += 1
                fill_ids[fill["venue_trade_id"]] = inserted["fill_id"]
            else:
                existing = conn.execute(
                    """SELECT fill_id, outcome_id, venue_order_id, side, role, quantity, price,
                              fee, fee_currency, realized_pnl, realized_pnl_currency,
                              realized_pnl_semantics, occurred_at = %s::timestamptz AS same_time
                       FROM outcome_fills
                       WHERE account_id = %s AND symbol = %s AND venue_trade_id = %s""",
                    (fill["occurred_at"], account_id, fill["symbol"], fill["venue_trade_id"]),
                ).fetchone()
                if existing["outcome_id"] != outcome_id or not existing["same_time"] or any(
                    existing[key] != fill.get(key) for key in (
                        "venue_order_id", "side", "role", "quantity", "price", "fee",
                        "fee_currency", "realized_pnl", "realized_pnl_currency",
                        "realized_pnl_semantics",
                    )
                ):
                    raise ValueError("conflicting venue trade identity")
                fills_duplicate += 1
                fill_ids[fill["venue_trade_id"]] = existing["fill_id"]

        exits_new = 0
        for exit_row in exits:
            fill_id = fill_ids.get(exit_row.get("venue_trade_id"))
            if fill_id is None:
                conflict = "(account_id, outcome_id, leg, price, quantity, closed_at) WHERE fill_id IS NULL"
            else:
                legacy = conn.execute(
                    """SELECT 1 FROM outcome_exits WHERE account_id = %s AND outcome_id = %s
                       AND fill_id IS NULL AND price = %s AND quantity = %s AND closed_at = %s""",
                    (account_id, outcome_id, exit_row["price"], exit_row["quantity"],
                     exit_row.get("closed_at")),
                ).fetchone()
                if legacy is not None:
                    continue
                conflict = "(fill_id)"
            cursor = conn.execute(
                f"""INSERT INTO outcome_exits
                   (exit_id, account_id, outcome_id, leg, exit_reason, quantity,
                    price, net_per_currency, r_multiple, closed_at, provenance, fill_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s)
                   ON CONFLICT {conflict} DO NOTHING""",
                (
                    _uuid(), account_id, outcome_id, exit_row["leg"],
                    exit_row.get("exit_reason"), exit_row["quantity"],
                    exit_row["price"], _json(exit_row.get("net_per_currency") or []),
                    exit_row.get("r_multiple"), exit_row.get("closed_at"),
                    _json(exit_row.get("provenance") or {}), fill_id,
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

        if position.get("reconcile"):
            totals = conn.execute(
                """SELECT COALESCE(SUM(quantity) FILTER (WHERE role = 'entry'), 0) AS entry_qty,
                          COALESCE(SUM(quantity) FILTER (WHERE role = 'exit'), 0) AS exit_qty,
                          MIN(occurred_at) FILTER (WHERE role = 'entry') AS opened_at,
                          MAX(occurred_at) FILTER (WHERE role = 'exit') AS closed_at
                   FROM outcome_fills WHERE outcome_id = %s""", (outcome_id,),
            ).fetchone()
            chronology = conn.execute(
                """SELECT MIN(balance) AS minimum_balance FROM (
                       SELECT SUM(CASE WHEN role = 'entry' THEN quantity ELSE -quantity END)
                           OVER (ORDER BY occurred_at, CASE WHEN role = 'entry' THEN 0 ELSE 1 END,
                                 fill_id) AS balance
                       FROM outcome_fills WHERE outcome_id = %s
                   ) timeline""", (outcome_id,),
            ).fetchone()
            stored_fill_rows = conn.execute(
                """SELECT venue_trade_id AS trade_id, venue_order_id AS order_id,
                          symbol, side, role, quantity, price, occurred_at
                   FROM outcome_fills WHERE outcome_id = %s""",
                (outcome_id,),
            ).fetchall()
            stored_fill_facts = [
                {
                    "trade_id": row["trade_id"],
                    "order_id": row["order_id"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "role": row["role"],
                    "quantity": str(row["quantity"]),
                    "price": str(row["price"]),
                    "occurred_at": row["occurred_at"].isoformat(),
                }
                for row in stored_fill_rows
            ]
            proof = verified_closure(
                fill_history=position.get("fill_history"),
                planned_quantity=position.get("planned_quantity"),
                entry_quantity=totals["entry_qty"],
                exit_quantity=totals["exit_qty"],
                opened_at=totals["opened_at"],
                closed_at=totals["closed_at"],
                minimum_balance=chronology["minimum_balance"],
                entry_order_final=position.get("entry_order_final"),
                expected_identity=position.get("closure_identity") or {},
                fill_facts=position.get("closure_fill_facts") or [],
                stored_fill_facts=stored_fill_facts,
            ) if evidence_current else None
            existing_closure = conn.execute(
                """SELECT closed_at, provenance FROM outcome_positions
                   WHERE outcome_id = %s FOR UPDATE""", (outcome_id,),
            ).fetchone()
            provenance = dict(position.get("provenance") or {})
            provenance["history_complete"] = position.get("history_complete") is True
            provenance["closure_verdict"] = "verified" if proof is not None else "unverified"
            provenance["closure_verdict_at"] = datetime.now(UTC).isoformat()
            if proof is not None:
                provenance.pop("recorded_closed_at", None)
                provenance["closure_proof_version"] = proof["version"]
                provenance["closure_proof"] = proof
            else:
                previous_provenance = dict(existing_closure["provenance"] or {})
                recorded_closed_at = (
                    existing_closure["closed_at"]
                    or previous_provenance.get("recorded_closed_at")
                )
                provenance.pop("closure_proof_version", None)
                provenance.pop("closure_proof", None)
                if recorded_closed_at is not None:
                    provenance["recorded_closed_at"] = (
                        recorded_closed_at.isoformat()
                        if hasattr(recorded_closed_at, "isoformat") else recorded_closed_at
                    )
            conn.execute(
                """UPDATE outcome_positions SET filled_quantity = %s, opened_at = %s,
                   closed_at = %s, provenance = %s::jsonb WHERE outcome_id = %s""",
                (totals["entry_qty"], totals["opened_at"],
                 proof["closed_at"] if proof is not None else None,
                 _json(provenance), outcome_id),
            )

    return {
        "positions_new": positions_new,
        "fills_new": fills_new,
        "fills_duplicate": fills_duplicate,
        "exits_new": exits_new,
        "funding_new": funding_new,
    }


def upsert_coverage(row: dict[str, Any]) -> None:
    """Insert or refresh one coverage row keyed by (account, window)."""
    with _connection(write=True) as conn:
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
    with _connection() as conn:
        row = conn.execute(
            "SELECT cursor_value FROM outcome_ledger_state WHERE state_key = %s",
            (key,),
        ).fetchone()
    return row["cursor_value"] if row is not None else None


def set_cursor(value: str, key: str = LEDGER_CURSOR_KEY) -> None:
    """Persist the cursor so a restart catches up from where it left off."""
    with _connection(write=True) as conn:
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
    with _connection(write=True) as conn:
        conn.execute(
            "DELETE FROM outcome_ledger_state WHERE state_key = %s", (key,),
        )


def schedule_reconciliation(cycle_id: str) -> None:
    """Keep a durable round-robin queue even before a position has fills."""
    with _connection(write=True) as conn:
        conn.execute(
            """INSERT INTO outcome_ledger_state (state_key, cursor_value, updated_at)
               VALUES (%s, %s, %s) ON CONFLICT (state_key) DO NOTHING""",
            (f"reconcile:{cycle_id}", cycle_id, datetime.now(UTC)),
        )


def reconciliation_batch(*, batch_size: int) -> list[dict[str, Any]]:
    """Rotate the least recently attempted cycles, including closed positions.

    The poll's session advisory lock owns the work through venue reads;
    this short transaction only rotates the bounded round-robin batch.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    with _connection(write=True) as conn:
        rows = conn.execute(
            """WITH claimed AS (
                   SELECT s.state_key
                   FROM outcome_ledger_state s
                   JOIN futures_cycles c ON c.cycle_id = s.cursor_value
                   WHERE s.state_key = 'reconcile:' || c.cycle_id
                   ORDER BY s.updated_at, s.state_key
                   LIMIT %s
                   FOR UPDATE OF s SKIP LOCKED
               ),
               rotated AS (
                   UPDATE outcome_ledger_state st SET updated_at = now()
                   WHERE st.state_key IN (SELECT state_key FROM claimed)
               )
               SELECT c.cycle_id, c.symbol, c.four_hour_open, c.one_hour_open,
                      c.strategy_version, c.status, c.phase, c.payload, c.created_at,
                      c.updated_at, c.finished_at
               FROM claimed cl
               JOIN futures_cycles c ON 'reconcile:' || c.cycle_id = cl.state_key
               ORDER BY c.updated_at, c.cycle_id""", (batch_size,),
        ).fetchall()
    return [dict(row) for row in rows]


def schedule_existing_positions(*, batch_size: int) -> None:
    """Enqueue previously recorded positions without altering their history."""
    with _connection(write=True) as conn:
        conn.execute(
            """INSERT INTO outcome_ledger_state (state_key, cursor_value, updated_at)
               SELECT 'reconcile:' || p.cycle_id, p.cycle_id, now()
               FROM outcome_positions p JOIN futures_cycles c ON c.cycle_id = p.cycle_id
               WHERE NOT EXISTS (SELECT 1 FROM outcome_ledger_state s
                                 WHERE s.state_key = 'reconcile:' || p.cycle_id)
               GROUP BY p.cycle_id ORDER BY p.cycle_id LIMIT %s
               ON CONFLICT (state_key) DO NOTHING""", (batch_size,),
        )


def record_funding_observations(account_id: str, venue_key: str, account_type: str,
                                symbol: str, events: list[dict]) -> None:
    """Store identified income without assigning it to a position."""
    if any(not isinstance(value, str) or not value.strip() or value != value.strip() for value in (
        account_id, venue_key, account_type, symbol,
    )):
        raise ValueError("funding observation requires complete account and venue identity")
    if any(not isinstance(event, dict) or not isinstance(event.get("id"), str)
           or not event["id"].strip() or event["id"] != event["id"].strip() for event in events):
        raise ValueError("funding observation requires a venue income ID")
    with _connection(write=True) as conn:
        for event in events:
            inserted = conn.execute(
                """INSERT INTO outcome_funding_observations
                   (account_id, venue_key, account_type, symbol, venue_income_id,
                    funding_at, amount, currency, venue_semantics)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, venue_key, account_type, symbol, venue_income_id)
                   DO NOTHING
                   RETURNING venue_income_id""",
                (account_id, venue_key, account_type, symbol, event["id"],
                 event["funding_at"], event["amount"], event["currency"],
                 event["venue_semantics"]),
            ).fetchone()
            if inserted is None:
                same = conn.execute(
                    """SELECT 1 FROM outcome_funding_observations
                       WHERE account_id = %s AND venue_key = %s AND account_type = %s
                         AND symbol = %s AND venue_income_id = %s AND funding_at = %s
                         AND amount = %s AND currency = %s AND venue_semantics = %s""",
                    (account_id, venue_key, account_type, symbol, event["id"],
                     event["funding_at"], event["amount"], event["currency"],
                     event["venue_semantics"]),
                ).fetchone()
                if same is None:
                    raise ValueError("conflicting venue funding identity")


def revoke_cycle_closure(cycle_id: str, reason: str) -> None:
    """Revoke stale closure proof while retaining the recorded timestamp."""
    with _connection(write=True) as conn:
        conn.execute(
            """UPDATE outcome_positions
               SET provenance = (provenance - 'closure_proof_version' - 'closure_proof')
                   || CASE WHEN closed_at IS NULL
                           THEN '{}'::jsonb
                           ELSE jsonb_build_object('recorded_closed_at', closed_at::text)
                      END
                   || jsonb_build_object('closure_verdict', 'unverified',
                                         'closure_verdict_at', %s::text,
                                         'closure_verdict_reason', %s::text),
                   closed_at = NULL
               WHERE cycle_id = %s""",
            (datetime.now(UTC).isoformat(), reason, cycle_id),
        )


def stored_cycle_fills(account_id: str, cycle_id: str) -> list[dict]:
    with _connection() as conn:
        rows = conn.execute(
            """SELECT f.venue_trade_id, f.symbol, f.realized_pnl, f.realized_pnl_currency
               FROM outcome_fills f JOIN outcome_positions p ON p.outcome_id = f.outcome_id
               WHERE p.account_id = %s AND p.cycle_id = %s""", (account_id, cycle_id),
        ).fetchall()
    return [dict(row) for row in rows]


def terminal_cycles_after(
    cursor_updated_at: str | None,
    cursor_cycle_id: str | None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """The next bounded batch of terminal cycles in stable (updated_at, id) order.

    The cursor is the last processed (updated_at, cycle_id) pair. A restart
    resumes after it, and previously attempted cycles can be retried when a
    poll does not reach its cursor update.
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
    with _connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def find_journal_tasks(symbol: str, venue_order_id: str | None = None, *,
                       intent_id: str | None = None) -> list[dict[str, Any]]:
    """Read-only attribution evidence from the durable entry journal.

    Matches the entry-intent rows of one symbol; with ``venue_order_id`` given,
    only tasks whose ``placed_ids`` contain that venue order id are returned.
    ``intent_id`` restricts the result to the exact durable journal task.
    The journal is never modified here.
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
    if intent_id is not None:
        query += " AND task_id = %s"
        params.append(intent_id)
    with _connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def find_bracket_tasks(symbol: str, entry_order_id: str) -> list[dict[str, Any]]:
    """Return all bracket evidence rows; callers must reject ambiguity."""
    with _connection() as conn:
        rows = conn.execute(
            """SELECT task_id, symbol, entry_order_id, close_side, legs, placed_ids,
                      status, venue_key, account_identity_exchange,
                      account_identity_profile, account_identity_type,
                      account_identity_hash
               FROM bracket_recovery_tasks
               WHERE symbol = %s AND entry_order_id = %s AND task_id LIKE 'bracket:%%'""",
            (symbol, entry_order_id),
        ).fetchall()
    return [dict(row) for row in rows]


def find_bracket_task(symbol: str, entry_order_id: str) -> dict[str, Any] | None:
    """Read one bracket task only when its evidence key is unique."""
    rows = find_bracket_tasks(symbol, entry_order_id)
    return rows[0] if len(rows) == 1 else None
