"""Durable, idempotent persistence for forward Kronos evaluations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from server import db

STATE_KEY = "forecast_evaluation"
TERMINAL_STATUSES = ("completed", "rejected", "failed", "interrupted")

# Normalize fractional seconds like datetime.fromisoformat (truncate, do not
# round) before PostgreSQL compares them with the microsecond cutover.
_RECORDED_AT_SQL = r"""CASE
    WHEN jsonb_typeof(payload #> '{kronos_forecast,recorded_at}') = 'string'
         AND payload #>> '{kronos_forecast,recorded_at}' ~
             '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9]([.][0-9]+)?(Z|[+-](0[0-9]|1[0-5]):[0-5][0-9])$'
         AND substring(payload #>> '{kronos_forecast,recorded_at}' FROM 12 FOR 2) < '24'
    THEN CASE
        WHEN pg_input_is_valid(payload #>> '{kronos_forecast,recorded_at}', 'timestamptz')
        THEN CASE
            WHEN regexp_replace(
                payload #>> '{kronos_forecast,recorded_at}',
                '([.][0-9]{6})[0-9]+(Z|[+-][0-9]{2}:[0-9]{2})$', '\1\2'
            )::timestamptz >= '0001-01-01T00:00:00+00:00'::timestamptz
             AND regexp_replace(
                payload #>> '{kronos_forecast,recorded_at}',
                '([.][0-9]{6})[0-9]+(Z|[+-][0-9]{2}:[0-9]{2})$', '\1\2'
            )::timestamptz < '10000-01-01T00:00:00+00:00'::timestamptz
            THEN regexp_replace(
                payload #>> '{kronos_forecast,recorded_at}',
                '([.][0-9]{6})[0-9]+(Z|[+-][0-9]{2}:[0-9]{2})$', '\1\2'
            )::timestamptz
        END
    END
END"""


class CursorLostError(RuntimeError):
    """The poll no longer owns the current evaluation cutover cursor."""


def _cursor_parts(cursor: str) -> tuple[str, str, str]:
    parts = cursor.split("|", 2)
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise ValueError("forecast evaluation cursor is malformed")
    return parts[0], parts[1], parts[2]


def cursor_cutover(cursor: str) -> str:
    return _cursor_parts(cursor)[0]


def scan_cursor(cursor: str, updated_at: str, cycle_id: str) -> str:
    cutover, _old_updated_at, _old_cycle_id = _cursor_parts(cursor)
    return f"{cutover}|{updated_at}|{cycle_id}"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _next_attempt(row: dict[str, Any]) -> datetime:
    value = row.get("next_attempt_at")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    target = row.get("target_as_of")
    if row.get("status") == "pending" and isinstance(target, datetime):
        return target if target.tzinfo else target.replace(tzinfo=UTC)
    if row.get("status") == "unavailable":
        if row.get("target_as_of") is None:
            return datetime.max.replace(tzinfo=UTC)
        return datetime.now(UTC) + timedelta(minutes=1)
    return datetime.now(UTC)


def get_cursor() -> str | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT cursor_value FROM forecast_evaluation_state WHERE state_key = %s",
            (STATE_KEY,),
        ).fetchone()
    return row["cursor_value"] if row else None


def initialize_cursor(*, reset: bool = False) -> bool:
    """Set the no-backfill cutover, optionally resetting it on re-enable."""
    now = datetime.now(UTC)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    cursor = f"{timestamp}|{timestamp}|"
    with db.session() as conn:
        row = conn.execute(
            """INSERT INTO forecast_evaluation_state(state_key, cursor_value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (state_key) DO UPDATE SET
                   cursor_value = EXCLUDED.cursor_value,
                   updated_at = EXCLUDED.updated_at
               WHERE %s
               RETURNING cursor_value""",
            (STATE_KEY, cursor, now, reset),
        ).fetchone()
    return row is not None


def advance_cursor(previous: str, updated_at: str, cycle_id: str) -> bool:
    """Advance only if no enable/disable reset replaced the previous cursor."""
    value = scan_cursor(previous, updated_at, cycle_id)
    with db.session() as conn:
        result = conn.execute(
            """UPDATE forecast_evaluation_state
               SET cursor_value = %s, updated_at = %s
               WHERE state_key = %s AND cursor_value = %s""",
            (value, datetime.now(UTC), STATE_KEY, previous),
        )
    return result.rowcount == 1


def terminal_forecast_cycles_after(
    cursor: str, *, batch_size: int = 50,
) -> list[dict[str, Any]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    cutover, _cursor_time, _cursor_id = _cursor_parts(cursor)
    with db.read() as conn:
        rows = conn.execute(
            f"""SELECT cycle_id, symbol, status, phase, payload, created_at, updated_at,
                       timeframe AS cycle_timeframe, selected_open AS cycle_selected_open
               FROM futures_cycles c
               WHERE status = ANY(%s)
                 AND payload ? 'kronos_forecast'
                 AND updated_at::timestamptz >= %s::timestamptz
                 AND ({_RECORDED_AT_SQL}) >= %s::timestamptz
                 AND NOT EXISTS (
                     SELECT 1 FROM forecast_evaluations e WHERE e.cycle_id = c.cycle_id
                 )
               ORDER BY updated_at::timestamptz, cycle_id
               LIMIT %s""",
            (list(TERMINAL_STATUSES), cutover, cutover, batch_size),
        ).fetchall()
    return [dict(row) for row in rows]


def pending_evaluations_due(*, limit: int = 50) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    with db.read() as conn:
        rows = conn.execute(
            """SELECT e.cycle_id, e.symbol, e.forecast_snapshot,
                      e.forecast_digest, e.horizon_key, e.venue_scope,
                      e.forecast_as_of, e.target_as_of AS due_at,
                      e.forecast_created_at, e.forecast_recorded_at, e.timeframe, e.profile,
                      e.effective_lookback_bars, e.horizon_bars, e.horizon_hours,
                      e.generation_kind, e.direction, e.direction_ratio, e.band,
                      e.reference_close, e.predicted_close,
                      c.timeframe AS cycle_timeframe, c.selected_open AS cycle_selected_open
               FROM forecast_evaluations e
               LEFT JOIN futures_cycles c ON c.cycle_id = e.cycle_id
               WHERE e.status IN ('pending', 'unavailable')
                 AND e.next_attempt_at <= now()
               ORDER BY e.next_attempt_at, e.cycle_id, e.horizon_key
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_evaluation(row: dict[str, Any], *, expected_cursor: str | None = None) -> None:
    if not row.get("horizon_key"):
        raise ValueError("forecast evaluation requires a stable horizon_key")
    with db.session() as conn:
        cursor_row = conn.execute(
            """SELECT cursor_value FROM forecast_evaluation_state
               WHERE state_key = %s FOR UPDATE""",
            (STATE_KEY,),
        ).fetchone()
        if expected_cursor is not None and (
            cursor_row is None or cursor_row["cursor_value"] != expected_cursor
        ):
            raise CursorLostError("forecast evaluation cutover changed during poll")
        conn.execute(
            """INSERT INTO forecast_evaluations(
                evaluation_id, cycle_id, symbol, forecast_digest, horizon_key,
                forecast_snapshot, venue_scope, status, forecast_as_of, forecast_created_at,
                forecast_recorded_at, target_as_of,
                timeframe, profile, effective_lookback_bars, horizon_bars, horizon_hours,
                generation_kind, direction, direction_ratio, band, reference_close,
                predicted_close, target_close, target_return, absolute_error,
                absolute_error_pct, directional_correct, band_contains_target, reason,
                warnings, provenance, created_at, last_attempted_at,
                retry_count, next_attempt_at, evaluated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (cycle_id, forecast_digest, horizon_key) DO UPDATE SET
                forecast_snapshot = EXCLUDED.forecast_snapshot,
                venue_scope = EXCLUDED.venue_scope,
                status = EXCLUDED.status,
                target_as_of = EXCLUDED.target_as_of,
                target_close = EXCLUDED.target_close,
                target_return = EXCLUDED.target_return,
                absolute_error = EXCLUDED.absolute_error,
                absolute_error_pct = EXCLUDED.absolute_error_pct,
                directional_correct = EXCLUDED.directional_correct,
                band_contains_target = EXCLUDED.band_contains_target,
                reason = EXCLUDED.reason,
                warnings = EXCLUDED.warnings,
                provenance = EXCLUDED.provenance,
                last_attempted_at = EXCLUDED.last_attempted_at,
                retry_count = CASE
                    WHEN EXCLUDED.status = 'unavailable'
                        THEN forecast_evaluations.retry_count + 1
                    ELSE forecast_evaluations.retry_count
                END,
                next_attempt_at = CASE
                    WHEN EXCLUDED.status = 'unavailable'
                        THEN EXCLUDED.last_attempted_at
                             + LEAST(
                                 INTERVAL '1 hour',
                                 INTERVAL '1 minute'
                                 * POWER(2, LEAST(forecast_evaluations.retry_count, 6))
                             )
                    ELSE EXCLUDED.next_attempt_at
                END,
                evaluated_at = EXCLUDED.evaluated_at
            WHERE forecast_evaluations.status IN ('pending', 'unavailable')""",
            (
                row.get("evaluation_id", str(uuid.uuid4())), row["cycle_id"], row["symbol"],
                row["forecast_digest"], row["horizon_key"],
                _json(row.get("forecast_snapshot") or {}), _json(row.get("venue_scope") or {}),
                row["status"], row.get("forecast_as_of"), row.get("forecast_created_at"),
                row.get("forecast_recorded_at"), row.get("target_as_of"),
                row.get("timeframe"), row.get("profile"),
                row.get("effective_lookback_bars"), row.get("horizon_bars"),
                row.get("horizon_hours"), row.get("generation_kind"), row.get("direction"),
                _json(row.get("direction_ratio") or {}), _json(row.get("band") or {}),
                row.get("reference_close"), row.get("predicted_close"), row.get("target_close"),
                row.get("target_return"), row.get("absolute_error"), row.get("absolute_error_pct"),
                row.get("directional_correct"), row.get("band_contains_target"),
                row.get("reason"), _json(row.get("warnings") or []),
                _json(row.get("provenance") or {}), row.get("created_at") or datetime.now(UTC),
                row.get("last_attempted_at") or datetime.now(UTC), row.get("retry_count", 0),
                _next_attempt(row), row.get("evaluated_at"),
            ),
        )


def _list_evaluations(conn: Any, *, limit: int, symbol: str | None) -> list[dict[str, Any]]:
    query = "SELECT * FROM forecast_evaluations"
    params: list[Any] = []
    if symbol:
        query += " WHERE symbol = %s"
        params.append(symbol)
    query += " ORDER BY forecast_as_of DESC NULLS LAST, created_at DESC, evaluation_id LIMIT %s"
    params.append(limit)
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def list_evaluations(
    *, limit: int = 100, symbol: str | None = None,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    with db.read() as conn:
        return _list_evaluations(conn, limit=limit, symbol=symbol)


def summary(*, limit: int = 100, symbol: str | None = None) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    where = " WHERE symbol = %s" if symbol else ""
    params = (symbol,) if symbol else ()
    with db.read() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        totals = conn.execute(
            """SELECT count(*) AS total_count,
                      count(*) FILTER (WHERE status = 'pending') AS pending_count,
                      count(*) FILTER (WHERE status = 'evaluated') AS evaluated_count,
                      count(*) FILTER (WHERE status = 'unavailable') AS unavailable_count,
                      count(*) FILTER (
                          WHERE status = 'unavailable' AND target_as_of IS NULL
                      ) AS nonretryable_unavailable_count,
                      count(*) FILTER (WHERE status = 'invalid') AS invalid_count,
                      count(*) FILTER (
                          WHERE status = 'evaluated' AND directional_correct IS NOT NULL
                      ) AS directional_count,
                      count(*) FILTER (
                          WHERE status = 'evaluated' AND directional_correct IS TRUE
                      ) AS direction_correct_count,
                      count(*) FILTER (
                          WHERE status = 'evaluated' AND band_contains_target IS NOT NULL
                      ) AS band_count,
                      count(*) FILTER (
                          WHERE status = 'evaluated' AND band_contains_target IS TRUE
                      ) AS band_contained_count
               FROM forecast_evaluations""" + where,
            params,
        ).fetchone()
        unproven = conn.execute(
            f"""SELECT count(*) AS unproven_count
                FROM futures_cycles
                WHERE status = ANY(%s)
                  AND payload ? 'kronos_forecast'
                  AND updated_at::timestamptz >= (
                      SELECT split_part(cursor_value, '|', 1)::timestamptz
                      FROM forecast_evaluation_state WHERE state_key = %s
                  )
                  AND ({_RECORDED_AT_SQL}) IS NULL""" + (" AND symbol = %s" if symbol else ""),
            (list(TERMINAL_STATUSES), STATE_KEY, *params),
        ).fetchone()
        rows = _list_evaluations(conn, limit=limit, symbol=symbol)
    total_count = int(totals["total_count"])
    counts = {
        "pending": int(totals["pending_count"]),
        "evaluated": int(totals["evaluated_count"]),
        "unavailable": int(totals["unavailable_count"]),
        "invalid": int(totals["invalid_count"]),
    }
    directional_count = int(totals["directional_count"])
    band_count = int(totals["band_count"])
    unproven_count = int(unproven["unproven_count"])
    nonretryable_count = int(totals["nonretryable_unavailable_count"])
    coverage_warnings = [
        "forecast timestamps are self-reported and do not independently prove pre-close persistence",
    ]
    if unproven_count:
        coverage_warnings.append(
            "some terminal forecast snapshots have an unproven recording time and are excluded"
        )
    if total_count == 0:
        coverage_warnings.append("no forecast evaluation rows are available")
    if counts["pending"]:
        coverage_warnings.append("some forecast horizons have not closed")
    if counts["unavailable"] > nonretryable_count:
        coverage_warnings.append("some target market reads are unavailable and will retry")
    if nonretryable_count:
        coverage_warnings.append("some unavailable forecast snapshots have no target and cannot be retried")
    if counts["invalid"]:
        coverage_warnings.append("invalid or retrospective evidence is excluded from metrics")
    return {
        "status": "unavailable" if total_count == 0 else (
            "complete" if counts["evaluated"] == total_count and not unproven_count else "partial"
        ),
        "total_count": total_count,
        "sample_count": counts["evaluated"],
        "counts": counts,
        "coverage": {
            "total_count": total_count,
            "evaluated_count": counts["evaluated"],
            "pending_count": counts["pending"],
            "unavailable_count": counts["unavailable"],
            "nonretryable_unavailable_count": nonretryable_count,
            "invalid_count": counts["invalid"],
            "unproven_recording_time_count": unproven_count,
            "metrics_scope": "all_matching_rows",
        },
        "coverage_warnings": coverage_warnings,
        "directional_sample_count": directional_count,
        "band_sample_count": band_count,
        "directional_accuracy": (
            int(totals["direction_correct_count"]) / directional_count
            if directional_count else None
        ),
        "band_containment": (
            int(totals["band_contained_count"]) / band_count if band_count else None
        ),
        "items_page_limit": limit,
        "metrics_scope": "all_matching_rows",
        "items": rows,
    }
