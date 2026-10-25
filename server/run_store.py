"""Persistence for analysis runs and the agent event stream they emit.

One concern: the durable record of what the agents did on a run — the analyst
reports, the debate turns, the tool calls, the final decision — so the UI can
replay any past run instead of only the one currently streaming. Events are
append-only here; the in-memory condition variable that wakes the SSE stream
stays in ``server/runs.py``, because a database cannot be waited on.

Like ``job_store``, this module deals in plain dicts so neither side imports
the other's dataclasses.
"""

import json
from datetime import datetime, timezone

from server import db

ACTIVE_STATUSES = ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def persist_run(fields: dict) -> None:
    """Upsert one run's row. Events are written separately, as they happen."""
    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO control_runs (run_id, ticker, trade_date, asset_type, mode, status,
                              signal, is_review, error, report_paths, analysts,
                              created_at, finished_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = excluded.status,
                signal = excluded.signal,
                is_review = excluded.is_review,
                error = excluded.error,
                report_paths = excluded.report_paths,
                analysts = excluded.analysts,
                finished_at = excluded.finished_at
            """,
            (
                fields["run_id"],
                fields["ticker"],
                fields["trade_date"],
                fields["asset_type"],
                fields["mode"],
                fields["status"],
                fields.get("signal"),
                bool(fields.get("is_review")),
                _json(fields["error"]) if fields.get("error") else None,
                _json(fields["report_paths"]) if fields.get("report_paths") else None,
                _json(fields.get("analysts") or []),
                fields["created_at"],
                fields.get("finished_at"),
            ),
        )


def append_event(run_id: str, event: dict) -> None:
    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO control_run_events (run_id, seq, ts, type, role, stage,
                                    agent, replay, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (run_id, seq) DO NOTHING
            """,
            (
                run_id,
                event["seq"],
                event["ts"],
                event["type"],
                event.get("role"),
                event.get("stage"),
                event.get("agent"),
                bool(event.get("replay")),
                _json(event.get("payload") or {}),
            ),
        )


def _row_to_run(row) -> dict:
    return {
        "run_id": row["run_id"],
        "ticker": row["ticker"],
        "trade_date": row["trade_date"],
        "asset_type": row["asset_type"],
        "mode": row["mode"],
        "status": row["status"],
        "signal": row["signal"],
        "is_review": bool(row["is_review"]),
        "error": row["error"],
        "report_paths": row["report_paths"],
        "analysts": row["analysts"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def load_run(run_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM control_runs WHERE run_id = %s", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None


def load_events(run_id: str, after_seq: int = -1) -> list[dict]:
    """The persisted agent event stream, oldest first, from ``after_seq``."""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM control_run_events WHERE run_id = %s AND seq > %s ORDER BY seq",
            (run_id, after_seq),
        ).fetchall()
    return [
        {
            "seq": row["seq"],
            "ts": row["ts"],
            "type": row["type"],
            "role": row["role"],
            "stage": row["stage"],
            "agent": row["agent"],
            "replay": bool(row["replay"]),
            "payload": row["payload"],
        }
        for row in rows
    ]


def list_runs(status: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """(run summaries newest-first, total matching) — summaries carry an
    event count instead of the event stream."""
    where = "" if status is None else "WHERE r.status = %s"
    params: tuple = () if status is None else (status,)
    with db.read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS total FROM control_runs r {where}", params
        ).fetchone()["total"]
        rows = conn.execute(
            f"""
            SELECT r.*, (SELECT COUNT(*) FROM control_run_events e WHERE e.run_id = r.run_id) AS events_count
            FROM control_runs r {where}
            ORDER BY r.created_at DESC, r.run_id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = _row_to_run(row)
        item["events_count"] = row["events_count"]
        items.append(item)
    return items, total


def reconcile_orphans() -> list[str]:
    """Close out runs a dead process left active and persist their final event."""
    now = _now()
    error = {
        "code": "interrupted",
        "message": "The server restarted before this analysis run completed.",
    }
    with db.session() as conn:
        rows = conn.execute(
            "SELECT run_id FROM control_runs WHERE status IN (%s, %s) ORDER BY created_at",
            ACTIVE_STATUSES,
        ).fetchall()
        if not rows:
            return []
        for row in rows:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM control_run_events WHERE run_id = %s",
                (row["run_id"],),
            ).fetchone()["next"]
            conn.execute(
                """
                INSERT INTO control_run_events (run_id, seq, ts, type, role, stage,
                                        agent, replay, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, seq) DO NOTHING
                """,
                (
                    row["run_id"], seq, now, "run_interrupted",
                    "system", "system", None, False,
                    _json({"error": error}),
                ),
            )
        conn.execute(
            "UPDATE control_runs SET status = 'interrupted', error = %s::jsonb, finished_at = %s "
            "WHERE status IN (%s, %s)",
            (_json(error), now, *ACTIVE_STATUSES),
        )
    return [row["run_id"] for row in rows]
