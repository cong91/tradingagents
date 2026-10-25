"""One-off SQLite -> PostgreSQL importer for the control-panel store.

Run it explicitly; the server never imports legacy state on startup. The
source file is opened read-only and never modified, and every table write is
an idempotent upsert, so an interrupted import can be resumed by re-running.

    python -m server.import_control_sqlite --source server/data/control.db --dry-run
    python -m server.import_control_sqlite --source server/data/control.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from server import db

# (sqlite table, postgres statement, row builder) — ordered so parents land first.
_TABLES = ("jobs", "job_results", "runs", "run_events", "futures_cycles",
           "futures_signal_claims", "bracket_recovery_tasks", "position_states",
           "position_monitor_leases")


def _connect_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite source not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _counts(source: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    for table in _TABLES:
        try:
            counts[table] = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = 0
    return counts


def _import_jobs(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM jobs").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO control_jobs (job_id, mode, status, auto, top_n, tickers,
                                          asset_types, screening, error, created_at,
                                          finished_at, last_progress_at, current_ticker)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s,
                        %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    row["job_id"], row["mode"], row["status"], bool(row["auto"]),
                    row["top_n"], row["tickers"], row["asset_types"], row["screening"],
                    row["error"], row["created_at"], row["finished_at"],
                    row["last_progress_at"], row["current_ticker"],
                ),
            )
    return len(rows)


def _import_job_results(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM job_results").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO control_job_results (id, job_id, ticker, asset_type, signal,
                                                 reason, error, approval_id, mock_source,
                                                 plan, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    row["id"], row["job_id"], row["ticker"], row["asset_type"],
                    row["signal"], row["reason"], row["error"], row["approval_id"],
                    row["mock_source"], row["plan"], row["created_at"],
                ),
            )
    return len(rows)


def _import_runs(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM runs").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO control_runs (run_id, ticker, trade_date, asset_type, mode,
                                          status, signal, is_review, error, report_paths,
                                          analysts, created_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s, %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    row["run_id"], row["ticker"], row["trade_date"], row["asset_type"],
                    row["mode"], row["status"], row["signal"], bool(row["is_review"]),
                    row["error"], row["report_paths"], row["analysts"],
                    row["created_at"], row["finished_at"],
                ),
            )
    return len(rows)


def _import_run_events(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM run_events").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO control_run_events (id, run_id, seq, ts, type, role, stage,
                                                agent, replay, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    row["id"], row["run_id"], row["seq"], row["ts"], row["type"],
                    row["role"], row["stage"], row["agent"], bool(row["replay"]),
                    row["payload"],
                ),
            )
    return len(rows)


def _import_futures_cycles(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM futures_cycles").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO futures_cycles (cycle_id, symbol, four_hour_open, one_hour_open,
                                            strategy_version, status, phase, payload,
                                            created_at, updated_at, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (cycle_id) DO NOTHING
                """,
                (
                    row["cycle_id"], row["symbol"], row["four_hour_open"],
                    row["one_hour_open"], row["strategy_version"], row["status"],
                    row["phase"], row["payload"], row["created_at"],
                    row["updated_at"], row["finished_at"],
                ),
            )
    return len(rows)


def _import_futures_signal_claims(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM futures_signal_claims").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO futures_signal_claims (signal_id, claimed_at, lease_until,
                                                   status, completed_at, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO NOTHING
                """,
                (
                    row["signal_id"], row["claimed_at"], row["lease_until"],
                    row["status"], row["completed_at"], row["reason"],
                ),
            )
    return len(rows)


def _import_bracket_recovery(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM bracket_recovery_tasks").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO bracket_recovery_tasks (task_id, symbol, entry_order_id,
                                                    close_side, quantity, legs, placed_ids,
                                                    status, attempts, last_error,
                                                    updated_at, venue_key)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                """,
                (
                    row["task_id"], row["symbol"], row["entry_order_id"],
                    row["close_side"], row["quantity"], row["legs"],
                    row["placed_ids"], row["status"], row["attempts"],
                    row["last_error"], row["updated_at"], row["venue_key"],
                ),
            )
    return len(rows)


def _import_position_states(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM position_states").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO position_states (position_key, revision, state, updated_at)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (position_key) DO NOTHING
                """,
                (row["position_key"], row["revision"], row["state"], row["updated_at"]),
            )
    return len(rows)


def _import_position_leases(source: sqlite3.Connection) -> int:
    rows = source.execute("SELECT * FROM position_monitor_leases").fetchall()
    with db.session() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO position_monitor_leases (position_key, owner, lease_until)
                VALUES (%s, %s, %s)
                ON CONFLICT (position_key) DO NOTHING
                """,
                (row["position_key"], row["owner"], row["lease_until"]),
            )
    return len(rows)


_IMPORTERS = {
    "jobs": _import_jobs,
    "job_results": _import_job_results,
    "runs": _import_runs,
    "run_events": _import_run_events,
    "futures_cycles": _import_futures_cycles,
    "futures_signal_claims": _import_futures_signal_claims,
    "bracket_recovery_tasks": _import_bracket_recovery,
    "position_states": _import_position_states,
    "position_monitor_leases": _import_position_leases,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import legacy SQLite control state into PostgreSQL")
    parser.add_argument("--source", required=True, type=Path, help="path to control.db (read-only)")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args()

    source = _connect_source(args.source)
    try:
        counts = _counts(source)
        if args.dry_run:
            for table, count in counts.items():
                print(f"plan: {table}: {count} rows")
            return 0
        for table in _TABLES:
            if counts[table] == 0:
                print(f"skip: {table}: no rows")
                continue
            imported = _IMPORTERS[table](source)
            print(f"imported: {table}: {imported} source rows (idempotent)")
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
