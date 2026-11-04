"""Persistence for daily jobs and their per-coin results (PostgreSQL schema).

One concern: the durable record of what a scan job did. ``server/daily.py``
owns the live worker and the API contract; this module only reads and writes
the ``control_jobs``/``control_job_results`` tables, dealing in plain dicts so neither side
imports the other's dataclasses.

The whole job is rewritten on every state change (one row plus at most
``top_n`` result rows, all in a single transaction). A job is small and its
state is read far more often than written, so replace-all keeps memory and
database in lockstep with no drift and no partial write to reason about.
"""

import dataclasses
import json
import re
import secrets
import time
from datetime import UTC, datetime

from server import db

# Statuses a job can be left in by a process that died mid-flight. Anything
# still active when a new process starts was orphaned — see reconcile_orphans.
ACTIVE_STATUSES = ("queued", "running")
_FUTURES_TERMINAL_STATUSES = ("completed", "rejected", "failed", "interrupted")
_IDENTITY_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_STATE_KEYS = (
    "account_identity_exchange", "account_identity_profile",
    "account_identity_type", "account_identity_hash",
)


def _identity_columns(identity):
    """The four account-identity column values, or None when unbound.

    Accepts the FuturesAccountIdentity dataclass, a duck-typed equivalent
    (exchange/profile/account_type/identity_hash attributes), or a 4-tuple.
    The digest is validated before any write; the database CHECK enforces it
    again.
    """
    if identity is None:
        return None
    if isinstance(identity, (tuple, list)):
        if len(identity) != 4:
            raise ValueError(
                "account identity tuple must be (exchange, profile, account_type, identity_hash)"
            )
        exchange, profile, account_type, identity_hash = identity
    else:
        exchange = getattr(identity, "exchange", None)
        profile = getattr(identity, "profile", None)
        account_type = getattr(identity, "account_type", None)
        identity_hash = getattr(identity, "identity_hash", None)
    if any(not isinstance(value, str) or not value
           for value in (exchange, profile, account_type)):
        raise ValueError("account identity requires string exchange, profile and account_type")
    if not isinstance(identity_hash, str) or not _IDENTITY_HASH.fullmatch(identity_hash):
        raise ValueError("account identity hash must be a 64-character lowercase hex digest")
    return exchange, profile, account_type, identity_hash


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _job_row(data: dict) -> tuple:
    return (
        data["job_id"],
        data["mode"],
        data["status"],
        data.get("auto_top_n") is not None,
        data.get("auto_top_n"),
        _json(data.get("tickers") or []),
        _json(data.get("asset_types") or {}),
        _json(data["screening"]) if data.get("screening") is not None else None,
        data.get("error"),
        data["created_at"],
        data.get("finished_at"),
        data.get("last_progress_at") or data["created_at"],
        data.get("current_ticker"),
    )


def _row_to_job(row) -> dict:
    return {
        "job_id": row["job_id"],
        "mode": row["mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "last_progress_at": row["last_progress_at"] or row["created_at"],
        "current_ticker": row["current_ticker"],
        "tickers": row["tickers"],
        "asset_types": row["asset_types"],
        "screening": row["screening"],
        "auto_top_n": row["top_n"],
        "error": row["error"],
        "results": [],
    }


def persist(data: dict) -> None:
    """Write the job row and replace its results, atomically."""
    with db.session() as conn:
        conn.execute(
            """
            INSERT INTO control_jobs (job_id, mode, status, auto, top_n, tickers, asset_types,
                              screening, error, created_at, finished_at,
                              last_progress_at, current_ticker)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
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
        conn.execute("DELETE FROM control_job_results WHERE job_id = %s", (data["job_id"],))
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
                _json(plan) if plan is not None else None,
                _now(),
            ))
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO control_job_results (job_id, ticker, asset_type, signal, reason, error,
                                             approval_id, mock_source, plan, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    rows,
                )


def _results_for(conn, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM control_job_results WHERE job_id = %s ORDER BY id", (job_id,)
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
            "plan": row["plan"],
        }
        if row["mock_source"] is not None:
            result["mock_source"] = row["mock_source"]
        results.append(result)
    return results


def load(job_id: str) -> dict | None:
    """The full job (DailyJobState-shaped dict) or None when unknown."""
    with db.read() as conn:
        row = conn.execute("SELECT * FROM control_jobs WHERE job_id = %s", (job_id,)).fetchone()
        if row is None:
            return None
        data = _row_to_job(row)
        data["results"] = _results_for(conn, job_id)
    return data


def list_jobs(status: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """(job summaries newest-first, total matching) — summaries carry counts,
    not the result rows: the history list needs shape, not payload."""
    where = "" if status is None else "WHERE status = %s"
    params: tuple = () if status is None else (status,)
    with db.read() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS total FROM control_jobs {where}", params
        ).fetchone()["total"]
        rows = conn.execute(
            f"""
            SELECT j.*,
                   (SELECT COUNT(*) FROM control_job_results r WHERE r.job_id = j.job_id) AS results_count,
                   (SELECT COUNT(*) FROM control_job_results r
                     WHERE r.job_id = j.job_id AND r.approval_id IS NOT NULL) AS approval_count
            FROM control_jobs j {where}
            ORDER BY j.created_at DESC, j.job_id DESC
            LIMIT %s OFFSET %s
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
            "SELECT * FROM control_jobs WHERE status IN (%s, %s) ORDER BY created_at DESC LIMIT 1",
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
               VALUES (%s, %s, %s, %s, %s, 'claimed', 'scan', %s::jsonb, %s, %s)
               ON CONFLICT (cycle_id) DO NOTHING""",
            (cycle_id, symbol, four_hour_open, one_hour_open, strategy_version,
             _json(payload), now, now),
        )
        return cursor.rowcount == 1


def update_futures_cycle(cycle_id: str, *, status: str, phase: str,
                         payload: dict | None = None, terminal: bool = False) -> None:
    now = _now()
    with db.session() as conn:
        row = conn.execute(
            "SELECT status, phase, payload FROM futures_cycles WHERE cycle_id = %s FOR UPDATE",
            (cycle_id,),
        ).fetchone()
        if row is None:
            return
        current_status = row["status"]
        if current_status in _FUTURES_TERMINAL_STATUSES:
            return
        existing = row["payload"]
        merged = _merge_payload(existing or {}, payload or {})
        conn.execute(
            """UPDATE futures_cycles SET status = %s, phase = %s, payload = %s::jsonb,
               updated_at = %s, finished_at = CASE WHEN %s THEN %s ELSE finished_at END
               WHERE cycle_id = %s""",
            (status, phase, _json(merged), now, terminal, now, cycle_id),
        )


def _merge_payload(existing: dict, incoming: dict) -> dict:
    """Merge terminal fields into the claim snapshot without losing observability.

    An incoming ``None`` never erases a value already on the row: a rejection
    write that leaves ``signal_reason`` unset must not null out the
    ``tradeable_signal`` the analysis phase recorded. Deliberate non-None
    corrections (e.g. ``execution_status`` in_progress -> not_started) still
    overwrite.
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None and merged.get(key) is not None:
            continue
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_payload(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_futures_cycle(cycle_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM futures_cycles WHERE cycle_id = %s", (cycle_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_futures_cycles(limit: int = 20, symbol: str | None = None) -> list[dict]:
    query = "SELECT * FROM futures_cycles"
    params: list = []
    if symbol:
        query += " WHERE symbol = %s"
        params.append(symbol)
    query += " ORDER BY updated_at DESC LIMIT %s"
    params.append(limit)
    with db.read() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def futures_cycle_summary() -> dict[str, int]:
    """Outcome funnel over every recorded cycle row.

    Buckets mirror the scan pipeline: market screen → candle confirmation →
    agent proposal → executed entry. ``entered`` requires the executed
    confirmation in the payload AND a payload that never negates it — a row
    recording ``executed: false`` can never be counted as an entry, whatever
    terminal shape it carries. A plan that was never sent or filled lands in
    its own bucket instead: awaiting approval, plan risk refusal, no order, or
    execute failure. Legacy rows written before the phase split are mapped at
    read time from the outcome their payload recorded (explicit mapping below);
    history is never rewritten.
    """
    with db.read() as conn:
        rows = conn.execute(
            """SELECT CASE
                     WHEN status = 'completed' AND phase = 'entry'
                          AND payload->>'execution_status' = 'executed'
                          AND COALESCE(payload->>'executed', 'true') <> 'false'
                          THEN 'entered'
                     WHEN status IN ('claimed', 'running') THEN 'in_flight'
                     WHEN payload->>'execution_status' = 'failed' THEN 'execution_failed'
                     WHEN status IN ('interrupted', 'recovery_pending', 'failed') THEN 'interrupted'
                     WHEN status = 'rejected' AND phase = 'market' THEN 'market_rejected'
                     WHEN status = 'rejected' AND phase = 'confirmation' THEN 'confirmation_rejected'
                     WHEN status = 'rejected' AND phase = 'proposal' THEN 'proposal_rejected'
                     WHEN payload->>'execution_status' = 'awaiting_approval' THEN 'awaiting_approval'
                     WHEN payload->>'execution_status' = 'risk_rejected' THEN 'risk_rejected'
                     WHEN payload->>'execution_status' = 'not_executed' THEN 'not_executed'
                     ELSE 'other'
                   END AS bucket,
                   count(*) AS count
               FROM futures_cycles GROUP BY 1"""
        ).fetchall()
    return {row["bucket"]: row["count"] for row in rows}


def futures_cycle_symbol_rollup() -> list[dict]:
    """Per-symbol funnel counts plus the newest cycle's signal and reasons."""
    with db.read() as conn:
        rows = conn.execute(
            """SELECT symbol,
                      count(*) AS total,
                      count(*) FILTER (WHERE status = 'completed' AND phase = 'entry'
                                       AND payload->>'execution_status' = 'executed'
                                       AND COALESCE(payload->>'executed', 'true') <> 'false') AS entered,
                      count(*) FILTER (WHERE payload->>'execution_status' = 'awaiting_approval') AS awaiting_approval,
                      count(*) FILTER (WHERE payload->>'execution_status' = 'risk_rejected') AS risk_rejected,
                      count(*) FILTER (WHERE payload->>'execution_status' = 'not_executed') AS not_executed,
                      count(*) FILTER (WHERE payload->>'execution_status' = 'failed') AS execution_failed,
                      count(*) FILTER (WHERE status = 'rejected' AND phase = 'proposal') AS proposal_rejected,
                      count(*) FILTER (WHERE status = 'rejected' AND phase = 'confirmation') AS confirmation_rejected,
                      count(*) FILTER (WHERE status = 'rejected' AND phase = 'market') AS market_rejected,
                      count(*) FILTER (WHERE status IN ('claimed', 'running')) AS in_flight,
                      count(*) FILTER (WHERE (status IN ('interrupted', 'recovery_pending')
                                              OR (status = 'failed'
                                                  AND COALESCE(payload->>'execution_status', '') <> 'failed'))
                                             AND COALESCE(payload->>'execution_status', '') <> 'failed') AS interrupted,
                      count(*) FILTER (WHERE NOT (
                                           (status = 'completed' AND phase = 'entry'
                                            AND payload->>'execution_status' = 'executed'
                                            AND COALESCE(payload->>'executed', 'true') <> 'false')
                                           OR status IN ('claimed', 'running')
                                           OR payload->>'execution_status'
                                              IN ('failed', 'awaiting_approval', 'risk_rejected', 'not_executed')
                                           OR status IN ('interrupted', 'recovery_pending', 'failed')
                                           OR (status = 'rejected'
                                               AND phase IN ('market', 'confirmation', 'proposal'))
                                         )) AS other,
                      max(updated_at) AS last_updated,
                      (array_agg(status ORDER BY updated_at DESC))[1] AS last_status,
                      (array_agg(phase ORDER BY updated_at DESC))[1] AS last_phase,
                      (array_agg(COALESCE(payload->>'execution_status', '')
                                 ORDER BY updated_at DESC))[1] AS last_execution_status,
                      (array_agg(COALESCE(payload->>'executed', '')
                                 ORDER BY updated_at DESC))[1] AS last_executed,
                      (array_agg(COALESCE(payload->>'execution_reason', payload->>'plan_reason',
                                          payload->>'failure_reason')
                                 ORDER BY updated_at DESC))[1] AS last_execution_reason,
                      (array_agg(payload->>'signal' ORDER BY updated_at DESC))[1] AS last_signal,
                      (array_agg(payload->>'signal_reason' ORDER BY updated_at DESC))[1] AS last_signal_reason,
                      (array_agg(payload->>'market_rejection_reason' ORDER BY updated_at DESC))[1] AS last_market_rejection_reason,
                      (array_agg(payload->>'confirmation_reason' ORDER BY updated_at DESC))[1] AS last_confirmation_reason
               FROM futures_cycles
               GROUP BY symbol
               ORDER BY last_updated DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def reconcile_futures_cycles() -> list[str]:
    now = _now()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT cycle_id FROM futures_cycles "
            "WHERE status IN ('claimed', 'running', 'recovery_pending')"
        ).fetchall()
        conn.execute(
            """UPDATE futures_cycles SET status = CASE WHEN phase = 'protection'
               THEN 'recovery_pending' ELSE 'interrupted' END,
               updated_at = %s, finished_at = %s
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
            VALUES (%s, %s, %s, 'claimed')
            ON CONFLICT (signal_id) DO UPDATE SET
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
               SET status = %s, completed_at = %s, reason = %s
               WHERE signal_id = %s AND status = 'claimed'""",
            (status, now or _now(), reason, signal_id),
        )
        return cursor.rowcount == 1


def create_bracket_recovery(task_id: str, *, symbol: str, entry_order_id: str, close_side: str,
                            quantity: float, legs: list[dict], now: str,
                            venue_key: str | None = None, identity=None) -> bool:
    columns = _identity_columns(identity) or (None, None, None, None)
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO bracket_recovery_tasks
               (task_id, symbol, entry_order_id, close_side, quantity, legs, updated_at, venue_key,
                account_identity_exchange, account_identity_profile,
                account_identity_type, account_identity_hash)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (task_id) DO NOTHING""",
            (task_id, symbol, entry_order_id, close_side, quantity, _json(legs), now, venue_key,
             *columns),
        )
        return cursor.rowcount == 1


def load_bracket_recovery(task_id: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM bracket_recovery_tasks WHERE task_id = %s", (task_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def bind_bracket_recovery_identity(task_id: str, *, identity) -> str:
    """Bind identity to a legacy (previously unbound) bracket task.

    One of "bound" (row accepted the binding), "not_found",
    "already_bound_same" (same four values already stored), or
    "already_bound_other" (a different binding exists; never overwritten).
    """
    columns = _identity_columns(identity)
    if columns is None:
        raise ValueError("account identity is required to bind a bracket recovery task")
    with db.session() as conn:
        cursor = conn.execute(
            """UPDATE bracket_recovery_tasks
               SET account_identity_exchange = %s, account_identity_profile = %s,
                   account_identity_type = %s, account_identity_hash = %s
               WHERE task_id = %s AND account_identity_hash IS NULL""",
            (*columns, task_id),
        )
        if cursor.rowcount == 1:
            return "bound"
        row = conn.execute(
            "SELECT account_identity_exchange, account_identity_profile, "
            "account_identity_type, account_identity_hash "
            "FROM bracket_recovery_tasks WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        if row is None:
            return "not_found"
        stored = (
            row["account_identity_exchange"], row["account_identity_profile"],
            row["account_identity_type"], row["account_identity_hash"],
        )
        return "already_bound_same" if stored == columns else "already_bound_other"


def update_bracket_recovery(task_id: str, *, status: str, placed_ids: list[str], attempts: int,
                            last_error: str | None, now: str, legs: list[dict] | None = None) -> None:
    with db.session() as conn:
        if legs is None:
            conn.execute(
                """UPDATE bracket_recovery_tasks SET status = %s, placed_ids = %s::jsonb,
                   attempts = %s, last_error = %s, updated_at = %s WHERE task_id = %s""",
                (status, _json(placed_ids), attempts, last_error, now, task_id),
            )
        else:
            conn.execute(
                """UPDATE bracket_recovery_tasks SET status = %s, legs = %s::jsonb,
                   placed_ids = %s::jsonb, attempts = %s, last_error = %s, updated_at = %s
                   WHERE task_id = %s""",
                (status, _json(legs), _json(placed_ids), attempts, last_error, now, task_id),
            )


def pending_bracket_recoveries() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM bracket_recovery_tasks WHERE status = 'pending'"
        ).fetchall()
    return [dict(row) for row in rows]


def active_bracket_recoveries() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM bracket_recovery_tasks "
            "WHERE status IN ('pending', 'monitoring', 'submission_uncertain', "
            "'emergency_close_failed', 'emergency_cleanup_pending', "
            "'replacement_cleanup_pending', 'position_cleanup_pending', "
            "'time_stop_submission_uncertain', 'time_stop_unknown_position', "
            "'time_stop_failed')"
        ).fetchall()
    return [dict(row) for row in rows]


def acquire_position_monitor_lease(position_key: str, *, lease_seconds: float = 55.0) -> str | None:
    if lease_seconds <= 0:
        raise ValueError("lease duration must be positive")
    now = time.time()
    owner = secrets.token_hex(16)
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO position_monitor_leases(position_key, owner, lease_until)
               VALUES (%s, %s, %s)
               ON CONFLICT (position_key) DO UPDATE SET
                   owner = excluded.owner,
                   lease_until = excluded.lease_until
               WHERE position_monitor_leases.lease_until <= %s""",
            (position_key, owner, now + lease_seconds, now),
        )
    return owner if cursor.rowcount == 1 else None


def release_position_monitor_lease(position_key: str, owner: str) -> bool:
    with db.session() as conn:
        cursor = conn.execute(
            "DELETE FROM position_monitor_leases WHERE position_key = %s AND owner = %s",
            (position_key, owner),
        )
    return cursor.rowcount == 1


def load_position_state(position_key: str) -> dict | None:
    with db.read() as conn:
        row = conn.execute(
            """SELECT revision, state, account_identity_exchange, account_identity_profile,
                      account_identity_type, account_identity_hash
               FROM position_states WHERE position_key = %s""",
            (position_key,),
        ).fetchone()
    if row is None:
        return None
    return _state_with_identity(row["state"], row["revision"], row)


def _state_with_identity(state: dict, revision: int, row) -> dict:
    """The stored state plus the identity columns when the row is bound.

    Legacy rows (all four columns NULL) carry no identity keys, so callers
    can distinguish an unbound record from a verified one.
    """
    merged = {**state, "revision": revision}
    if row["account_identity_exchange"] is not None:
        for key in _IDENTITY_STATE_KEYS:
            merged[key] = row[key]
    return merged


def find_bracket_recoveries(symbol: str, entry_order_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM bracket_recovery_tasks WHERE symbol = %s AND entry_order_id = %s",
            (symbol, entry_order_id),
        ).fetchall()
    return [dict(row) for row in rows]


def active_position_states() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            """SELECT position_key, revision, state, account_identity_exchange,
                      account_identity_profile, account_identity_type, account_identity_hash
               FROM position_states ORDER BY updated_at"""
        ).fetchall()
    return [
        {
            "position_key": row["position_key"],
            **_state_with_identity(row["state"], row["revision"], row),
        }
        for row in rows
    ]


def save_position_state(position_key: str, expected_revision: int, state: dict,
                        identity=None) -> bool:
    # The identity keys ride on the state dict when it round-trips through the
    # readers; they belong in the columns, never duplicated inside the jsonb.
    payload = {key: value for key, value in state.items() if key not in _IDENTITY_STATE_KEYS}
    if identity is None:
        candidate = tuple(state.get(key) for key in _IDENTITY_STATE_KEYS)
        if all(value is not None for value in candidate):
            identity = candidate
    columns = _identity_columns(identity)
    with db.session() as conn:
        if columns is None:
            cursor = conn.execute(
                """INSERT INTO position_states(position_key, revision, state, updated_at)
                   VALUES (%s, %s, %s::jsonb, %s)
                   ON CONFLICT (position_key) DO UPDATE SET revision = excluded.revision,
                     state = excluded.state, updated_at = excluded.updated_at
                   WHERE position_states.revision = %s""",
                (position_key, expected_revision + 1, _json(payload), _now(), expected_revision),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO position_states(position_key, revision, state, updated_at,
                                               account_identity_exchange, account_identity_profile,
                                               account_identity_type, account_identity_hash)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                   ON CONFLICT (position_key) DO UPDATE SET revision = excluded.revision,
                     state = excluded.state, updated_at = excluded.updated_at,
                     account_identity_exchange = excluded.account_identity_exchange,
                     account_identity_profile = excluded.account_identity_profile,
                     account_identity_type = excluded.account_identity_type,
                     account_identity_hash = excluded.account_identity_hash
                   WHERE position_states.revision = %s""",
                (position_key, expected_revision + 1, _json(payload), _now(),
                 *columns, expected_revision),
            )
        return cursor.rowcount == 1


def delete_position_state(position_key: str, expected_revision: int | None = None) -> bool:
    """Remove a position snapshot only if its optional revision is current."""
    with db.session() as conn:
        if expected_revision is None:
            cursor = conn.execute(
                "DELETE FROM position_states WHERE position_key = %s", (position_key,)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM position_states WHERE position_key = %s AND revision = %s",
                (position_key, expected_revision),
            )
        return cursor.rowcount == 1


def persist_futures_entry_state(position_key: str, *, side: str, entry: float,
                                quantity: float, stop: float, targets: list[float],
                                mark: float, order_id: str | None,
                                identity=None) -> bool:
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
    now = _now()
    return save_position_state(position_key, 0, {
        "snapshot": {
            **dataclasses.asdict(snapshot),
            "targets": list(snapshot.targets),
            "completed_targets": [],
            "initial_stop": stop,
            "current_target": targets[0] if targets else None,
            "phase": "initial",
            "opened_at": now,
            "observed_at": now,
        },
        "orders": [],
        "entry_order_id": order_id,
    }, identity=identity)


def reconcile_orphans() -> list[str]:
    """Close out jobs a dead process left active; returns the ids it closed.

    The worker lock is derived from job status, so a job stuck at ``running``
    in a record that outlives its thread would answer 409 to every new job
    forever, with nothing in the UI able to show or clear it.
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT job_id FROM control_jobs WHERE status IN (%s, %s) ORDER BY created_at",
            ACTIVE_STATUSES,
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE control_jobs SET status = 'interrupted', finished_at = %s "
            "WHERE status IN (%s, %s)",
            (_now(), *ACTIVE_STATUSES),
        )
    return [row["job_id"] for row in rows]
