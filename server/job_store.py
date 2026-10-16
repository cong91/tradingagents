"""Persistence for daily jobs and their per-coin results (server/db.py schema).

One concern: the durable record of what a scan job did. ``server/daily.py``
owns the live worker and the API contract; this module only reads and writes
the ``jobs``/``job_results`` tables, dealing in plain dicts so neither side
imports the other's dataclasses.

The whole job is rewritten on every state change (one row plus at most
``top_n`` result rows, all in a single transaction). A job is small and its
state is read far more often than written, so replace-all keeps memory and
database in lockstep with no drift and no partial write to reason about.
"""

import dataclasses
import json
import sqlite3
from datetime import datetime, timezone

from server import db

# Statuses a job can be left in by a process that died mid-flight. Anything
# still active when a new process starts was orphaned — see reconcile_orphans.
ACTIVE_STATUSES = ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_row(data: dict) -> tuple:
    return (
        data["job_id"],
        data["mode"],
        data["status"],
        1 if data.get("auto_top_n") is not None else 0,
        data.get("auto_top_n"),
        json.dumps(data.get("tickers") or [], ensure_ascii=False),
        json.dumps(data.get("asset_types") or {}, ensure_ascii=False),
        json.dumps(data["screening"], ensure_ascii=False) if data.get("screening") is not None else None,
        data.get("error"),
        data["created_at"],
        data.get("finished_at"),
        data.get("last_progress_at") or data["created_at"],
        data.get("current_ticker"),
    )


def _row_to_job(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"],
        "mode": row["mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "last_progress_at": row["last_progress_at"] or row["created_at"],
        "current_ticker": row["current_ticker"],
        "tickers": json.loads(row["tickers"]),
        "asset_types": json.loads(row["asset_types"]),
        "screening": json.loads(row["screening"]) if row["screening"] is not None else None,
        "auto_top_n": row["top_n"],
        "error": row["error"],
        "results": [],
    }


def persist(data: dict) -> None:
    """Write the job row and replace its results, atomically."""
    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO jobs (job_id, mode, status, auto, top_n, tickers, asset_types,
                              screening, error, created_at, finished_at,
                              last_progress_at, current_ticker)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                mode = excluded.mode,
                status = excluded.status,
                auto = excluded.auto,
                top_n = excluded.top_n,
                tickers = excluded.tickers,
                asset_types = excluded.asset_types,
                screening = excluded.screening,
                error = excluded.error,
                finished_at = excluded.finished_at,
                last_progress_at = excluded.last_progress_at,
                current_ticker = excluded.current_ticker
            """,
            _job_row(data),
        )
        conn.execute("DELETE FROM job_results WHERE job_id = ?", (data["job_id"],))
        rows = []
        for result in data.get("results") or []:
            plan = result.get("plan")
            rows.append((
                data["job_id"],
                result.get("ticker"),
                result.get("asset_type"),
                result.get("signal"),
                result.get("reason"),
                result.get("error"),
                result.get("approval_id"),
                result.get("mock_source"),
                json.dumps(plan, ensure_ascii=False) if plan is not None else None,
                _now(),
            ))
        conn.executemany(
            """
            INSERT INTO job_results (job_id, ticker, asset_type, signal, reason, error,
                                     approval_id, mock_source, plan, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _results_for(conn: sqlite3.Connection, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM job_results WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    results = []
    for row in rows:
        result = {
            "ticker": row["ticker"],
            "asset_type": row["asset_type"],
            "signal": row["signal"],
            "reason": row["reason"],
            "error": row["error"],
            "approval_id": row["approval_id"],
            "plan": json.loads(row["plan"]) if row["plan"] is not None else None,
        }
        if row["mock_source"] is not None:
            result["mock_source"] = row["mock_source"]
        results.append(result)
    return results


def load(job_id: str) -> dict | None:
    """The full job (DailyJobState-shaped dict) or None when unknown."""
    with db.read() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        data = _row_to_job(row)
        data["results"] = _results_for(conn, job_id)
    return data


def list_jobs(status: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """(job summaries newest-first, total matching) — summaries carry counts,
    not the result rows: the history list needs shape, not payload."""
    where = "" if status is None else "WHERE status = ?"
    params: tuple = () if status is None else (status,)
    with db.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT j.*,
                   (SELECT COUNT(*) FROM job_results r WHERE r.job_id = j.job_id) AS results_count,
                   (SELECT COUNT(*) FROM job_results r
                     WHERE r.job_id = j.job_id AND r.approval_id IS NOT NULL) AS approval_count
            FROM jobs j {where}
            ORDER BY j.created_at DESC, j.job_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = _row_to_job(row)
        item["results_count"] = row["results_count"]
        item["approval_count"] = row["approval_count"]
        del item["results"]
        items.append(item)
    return items, total


def active() -> dict | None:
    """The job a previous process left running, if any (read-only view)."""
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
        return _row_to_job(row) if row is not None else None


def claim_futures_cycle(cycle_id: str, *, symbol: str, four_hour_open: str,
                        one_hour_open: str, strategy_version: str,
                        payload: dict | None = None) -> bool:
    now = _now()
    def normalize(value):
        if dataclasses.is_dataclass(value):
            return {key: normalize(item) for key, item in dataclasses.asdict(value).items()}
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        return value
    payload = normalize(payload or {})
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO futures_cycles
               (cycle_id, symbol, four_hour_open, one_hour_open, strategy_version,
                status, phase, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'claimed', 'scan', ?, ?, ?)
               ON CONFLICT(cycle_id) DO NOTHING""",
            (cycle_id, symbol, four_hour_open, one_hour_open, strategy_version,
             json.dumps(payload or {}), now, now),
        )
        return cursor.rowcount == 1


def update_futures_cycle(cycle_id: str, *, status: str, phase: str,
                         payload: dict | None = None, terminal: bool = False) -> None:
    now = _now()
    with db.session() as conn:
        row = conn.execute(
            "SELECT payload FROM futures_cycles WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()
        existing = json.loads(row["payload"]) if row and row["payload"] else {}
        merged = _merge_payload(existing, payload or {})
        conn.execute(
            """UPDATE futures_cycles SET status = ?, phase = ?, payload = ?,
               updated_at = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END
               WHERE cycle_id = ?""",
            (status, phase, json.dumps(merged, ensure_ascii=False), now, terminal, now, cycle_id),
        )


def _merge_payload(existing: dict, incoming: dict) -> dict:
    """Merge terminal fields into the claim snapshot without losing observability."""
    merged = dict(existing)
    for key, value in incoming.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_payload(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_futures_cycle(cycle_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM futures_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return item


def list_futures_cycles(limit: int = 20) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT cycle_id FROM futures_cycles ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [load_futures_cycle(row["cycle_id"]) for row in rows]


def reconcile_futures_cycles() -> list[str]:
    now = _now()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT cycle_id FROM futures_cycles WHERE status IN ('claimed', 'running', 'recovery_pending')"
        ).fetchall()
        conn.execute(
            """UPDATE futures_cycles SET status = CASE WHEN phase = 'protection'
               THEN 'recovery_pending' ELSE 'interrupted' END,
               updated_at = ?, finished_at = ?
               WHERE status IN ('claimed', 'running')""",
            (now, now),
        )
    return [row["cycle_id"] for row in rows]


def claim_futures_signal(signal_id: str, *, now: str, lease_until: str) -> bool:
    """Claim once; only an unfinished expired lease may be recovered."""
    with db.session() as conn:
        cursor = conn.execute(
            """
            INSERT INTO futures_signal_claims(signal_id, claimed_at, lease_until, status)
            VALUES (?, ?, ?, 'claimed')
            ON CONFLICT(signal_id) DO UPDATE SET
                claimed_at = excluded.claimed_at,
                lease_until = excluded.lease_until,
                status = 'claimed'
            WHERE futures_signal_claims.status = 'claimed'
              AND futures_signal_claims.lease_until <= excluded.claimed_at
            """,
            (signal_id, now, lease_until),
        )
        return cursor.rowcount == 1


def release_futures_signal(signal_id: str, *, status: str = "completed",
                           now: str | None = None, reason: str | None = None) -> bool:
    """Atomically make a signal terminal; terminal rows are immutable."""
    if status == "claimed":
        raise ValueError("terminal signal status cannot be claimed")
    with db.session() as conn:
        cursor = conn.execute(
            """UPDATE futures_signal_claims
               SET status = ?, completed_at = ?, reason = ?
               WHERE signal_id = ? AND status = 'claimed'""",
            (status, now or _now(), reason, signal_id),
        )
        return cursor.rowcount == 1
def create_bracket_recovery(task_id: str, *, symbol: str, entry_order_id: str, close_side: str,
                            quantity: float, legs: list[dict], now: str) -> None:
    with db.session() as conn:
        conn.execute(
            """INSERT INTO bracket_recovery_tasks
               (task_id, symbol, entry_order_id, close_side, quantity, legs, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO NOTHING""",
            (task_id, symbol, entry_order_id, close_side, quantity, json.dumps(legs), now),
        )


def load_bracket_recovery(task_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM bracket_recovery_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return {**dict(row), "legs": json.loads(row["legs"]), "placed_ids": json.loads(row["placed_ids"])}


def update_bracket_recovery(task_id: str, *, status: str, placed_ids: list[str], attempts: int,
                            last_error: str | None, now: str, legs: list[dict] | None = None) -> None:
    with db.session() as conn:
        if legs is None:
            conn.execute(
                """UPDATE bracket_recovery_tasks SET status = ?, placed_ids = ?, attempts = ?,
                   last_error = ?, updated_at = ? WHERE task_id = ?""",
                (status, json.dumps(placed_ids), attempts, last_error, now, task_id),
            )
        else:
            conn.execute(
                """UPDATE bracket_recovery_tasks SET status = ?, legs = ?, placed_ids = ?, attempts = ?,
                   last_error = ?, updated_at = ? WHERE task_id = ?""",
                (status, json.dumps(legs), json.dumps(placed_ids), attempts, last_error, now, task_id),
            )


def pending_bracket_recoveries() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute("SELECT task_id FROM bracket_recovery_tasks WHERE status = 'pending'").fetchall()
    return [load_bracket_recovery(row["task_id"]) for row in rows]


def load_position_state(position_key: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT revision, state FROM position_states WHERE position_key = ?",
            (position_key,),
        ).fetchone()
    if row is None:
        return None
    return {**json.loads(row["state"]), "revision": row["revision"]}


def save_position_state(position_key: str, expected_revision: int, state: dict) -> bool:
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO position_states(position_key, revision, state, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(position_key) DO UPDATE SET revision = excluded.revision,
                 state = excluded.state, updated_at = excluded.updated_at
               WHERE position_states.revision = ?""",
            (position_key, expected_revision + 1, json.dumps(state), _now(), expected_revision),
        )
        return cursor.rowcount == 1


def delete_position_state(position_key: str, expected_revision: int | None = None) -> bool:
    """Remove a position snapshot only if its optional revision is current."""
    with db.session() as conn:
        if expected_revision is None:
            cursor = conn.execute(
                "DELETE FROM position_states WHERE position_key = ?", (position_key,)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM position_states WHERE position_key = ? AND revision = ?",
                (position_key, expected_revision),
            )
        return cursor.rowcount == 1


def persist_futures_entry_state(position_key: str, *, side: str, entry: float,
                                quantity: float, stop: float, targets: list[float],
                                mark: float, order_id: str | None) -> bool:
    """Record a live acknowledged entry and its initial protective state."""
    from tradingagents.execution.position_manager import PositionSnapshot

    snapshot = PositionSnapshot(
        position_key=position_key,
        side=side,
        entry=entry,
        quantity=quantity,
        stop=stop,
        targets=tuple(targets),
        mark=mark,
    )
    return save_position_state(position_key, 0, {
        "snapshot": {
            **dataclasses.asdict(snapshot),
            "targets": list(snapshot.targets),
            "completed_targets": [],
        },
        "orders": [],
        "entry_order_id": order_id,
    })


def reconcile_orphans() -> list[str]:
    """Close out jobs a dead process left active; returns the ids it closed.
    lock is derived from job status, so a job stuck at ``running`` in a record
    that outlives its thread would answer 409 to every new job forever, with
    nothing in the UI able to show or clear it.
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE status IN (?, ?) ORDER BY created_at",
            ACTIVE_STATUSES,
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status IN (?, ?)",
            (_now(), *ACTIVE_STATUSES),
        )
    return [row["job_id"] for row in rows]
