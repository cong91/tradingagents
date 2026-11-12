"""PostgreSQL persistence and outcome classification for futures cycles."""

import dataclasses
import json
from datetime import UTC, datetime

from server import db

_FUTURES_TERMINAL_STATUSES = ("completed", "rejected", "failed", "interrupted")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


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


_FUTURES_CYCLE_BUCKET_SQL = """CASE
    WHEN status = 'completed' AND phase = 'entry'
         AND payload->>'execution_status' = 'executed'
         AND COALESCE(payload->>'executed', 'true') <> 'false' THEN 'entered'
    WHEN status IN ('claimed', 'running') THEN 'in_flight'
    WHEN payload->>'execution_status' = 'failed' THEN 'execution_failed'
    WHEN status IN ('interrupted', 'recovery_pending', 'failed') THEN 'interrupted'
    WHEN status = 'rejected' AND phase = 'market' THEN 'market_rejected'
    WHEN status = 'rejected' AND phase = 'confirmation' THEN 'confirmation_rejected'
    WHEN status = 'rejected' AND phase = 'proposal' THEN 'proposal_rejected'
    WHEN status = 'rejected' AND phase = 'forecast'
         AND payload->>'kronos_policy' = 'defer' THEN 'forecast_deferred'
    WHEN payload->>'execution_status' = 'awaiting_approval' THEN 'awaiting_approval'
    WHEN payload->>'execution_status' = 'risk_rejected' THEN 'risk_rejected'
    WHEN payload->>'execution_status' = 'not_executed' THEN 'not_executed'
    ELSE 'other'
END"""


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
            f"""SELECT {_FUTURES_CYCLE_BUCKET_SQL} AS bucket, count(*) AS count
                FROM futures_cycles GROUP BY 1"""
        ).fetchall()
    return {row["bucket"]: row["count"] for row in rows}


def futures_cycle_symbol_rollup() -> list[dict]:
    """Per-symbol funnel counts plus the newest cycle's signal and reasons."""
    with db.read() as conn:
        rows = conn.execute(
            f"""WITH classified AS (
                   SELECT *, {_FUTURES_CYCLE_BUCKET_SQL} AS bucket FROM futures_cycles
               )
               SELECT symbol,
                      count(*) AS total,
                      count(*) FILTER (WHERE bucket = 'entered') AS entered,
                      count(*) FILTER (WHERE bucket = 'awaiting_approval') AS awaiting_approval,
                      count(*) FILTER (WHERE bucket = 'risk_rejected') AS risk_rejected,
                      count(*) FILTER (WHERE bucket = 'not_executed') AS not_executed,
                      count(*) FILTER (WHERE bucket = 'execution_failed') AS execution_failed,
                      count(*) FILTER (WHERE bucket = 'proposal_rejected') AS proposal_rejected,
                      count(*) FILTER (WHERE bucket = 'confirmation_rejected') AS confirmation_rejected,
                      count(*) FILTER (WHERE bucket = 'market_rejected') AS market_rejected,
                      count(*) FILTER (WHERE bucket = 'forecast_deferred') AS forecast_deferred,
                      count(*) FILTER (WHERE bucket = 'in_flight') AS in_flight,
                      count(*) FILTER (WHERE bucket = 'interrupted') AS interrupted,
                      count(*) FILTER (WHERE bucket = 'other') AS other,
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
                      (array_agg(payload->>'kronos_policy' ORDER BY updated_at DESC))[1] AS last_kronos_policy,
                      (array_agg(payload->>'kronos_defer_reason' ORDER BY updated_at DESC))[1] AS last_kronos_defer_reason,
                      (array_agg(payload->>'signal' ORDER BY updated_at DESC))[1] AS last_signal,
                      (array_agg(payload->>'signal_reason' ORDER BY updated_at DESC))[1] AS last_signal_reason,
                      (array_agg(payload->>'market_rejection_reason' ORDER BY updated_at DESC))[1] AS last_market_rejection_reason,
                      (array_agg(payload->>'confirmation_reason' ORDER BY updated_at DESC))[1] AS last_confirmation_reason
               FROM classified
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
