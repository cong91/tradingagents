"""PostgreSQL persistence for shadow observations (plan §6.3).

One concern: durable, idempotent storage of observations and their evaluations,
plus the worker's restart cursor and the read-only aggregate the dashboard asks
for. Writes are ``INSERT ... ON CONFLICT DO NOTHING`` against the migration-0010
uniqueness keys, so re-observing the same closed candle changes no total and a
replayed window writes nothing.

Reads never invent a number: an empty table answers ``unavailable`` with the
reason, and every aggregate counts only rows that recorded a real outcome.
"""

from __future__ import annotations

import json as _json_module
import uuid
from datetime import UTC, datetime
from typing import Any

from server import db

SHADOW_CURSOR_KEY = "shadow_worker"
DEFAULT_BATCH_SIZE = 25

# Terminal futures_cycles statuses the worker scans. Kept equal to
# job_store._FUTURES_TERMINAL_STATUSES (a test pins the two lists together).
TERMINAL_CYCLE_STATUSES = ("completed", "rejected", "failed", "interrupted")


def _json(value: Any) -> str:
    return _json_module.dumps(value, default=str)


def record_forecast(row: dict[str, Any]) -> bool:
    """Insert one observation; True when it was new, False when it already existed."""
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO shadow_forecasts
               (forecast_id, cycle_id, symbol, timeframe, horizon_hours, provider,
                model_revision, source_revision, artifact_hash, data_open_time,
                prediction_created_at, generation_kind, outcome_group, outcome_reason,
                sample_count, per_path_sample_count, distribution_kind,
                calibration_label, calibration_note, direction, samples, paths,
                status, reason, seed, deadline_seconds, duration_ms, params, provenance)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb)
               ON CONFLICT (provider, symbol, data_open_time, horizon_hours, model_revision)
               DO NOTHING""",
            (
                row.get("forecast_id") or str(uuid.uuid4()),
                row.get("cycle_id"), row["symbol"], row["timeframe"],
                row["horizon_hours"], row["provider"], row["model_revision"],
                row.get("source_revision"), row.get("artifact_hash"),
                row["data_open_time"], row["prediction_created_at"],
                row["generation_kind"], row["outcome_group"], row["outcome_reason"],
                row["sample_count"], row["per_path_sample_count"],
                row["distribution_kind"], row["calibration_label"],
                row["calibration_note"], row.get("direction"),
                _json(row.get("samples") or {}), _json(row.get("paths") or []),
                row["status"], row.get("reason"), row["seed"],
                row["deadline_seconds"], row.get("duration_ms"),
                _json(row.get("params") or {}), _json(row.get("provenance") or {}),
            ),
        )
        return cursor.rowcount == 1


def record_evaluation(forecast_id: str, row: dict[str, Any]) -> bool:
    """Insert one evaluation; False when this forecast was already evaluated."""
    with db.session() as conn:
        cursor = conn.execute(
            """INSERT INTO shadow_evaluations
               (evaluation_id, forecast_id, status, reason, realized_direction,
                realized_direction_hit, band_coverage, stop_first_rate, net_r,
                net_r_reason, realized, evaluated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
               ON CONFLICT (forecast_id) DO NOTHING""",
            (
                str(uuid.uuid4()), forecast_id, row["status"], row.get("reason"),
                row.get("realized_direction"), row.get("realized_direction_hit"),
                row.get("band_coverage"), row.get("stop_first_rate"),
                row.get("net_r"), row.get("net_r_reason"),
                _json(row.get("realized") or {}), datetime.now(UTC).isoformat(),
            ),
        )
        return cursor.rowcount == 1


def get_cursor(key: str = SHADOW_CURSOR_KEY) -> str | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT cursor_value FROM shadow_state WHERE state_key = %s", (key,),
        ).fetchone()
    return row["cursor_value"] if row is not None else None


def set_cursor(value: str, key: str = SHADOW_CURSOR_KEY) -> None:
    with db.session() as conn:
        conn.execute(
            """INSERT INTO shadow_state (state_key, cursor_value, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (state_key) DO UPDATE SET
                   cursor_value = EXCLUDED.cursor_value,
                   updated_at = EXCLUDED.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )


def clear_state(key: str) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM shadow_state WHERE state_key = %s", (key,))


def terminal_cycles_after(
    cursor_updated_at: str | None,
    cursor_cycle_id: str | None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """The next bounded batch of terminal cycles in stable (updated_at, id) order.

    The cursor is the last processed pair, so a restart resumes exactly where it
    stopped: nothing reprocessed, nothing dropped by a from-the-top LIMIT.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    query = (
        "SELECT cycle_id, symbol, four_hour_open, one_hour_open, strategy_version, "
        "status, phase, payload, created_at, updated_at, finished_at "
        "FROM futures_cycles WHERE status IN ("
        + ", ".join(f"'{status}'" for status in TERMINAL_CYCLE_STATUSES)
        + ")"
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


def forecasts_awaiting_evaluation(*, batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict[str, Any]]:
    """Observations whose horizon should have realized and that lack an evaluation.

    Bounded and ordered by the data cutoff, so the oldest unresolved forecast is
    looked at first and a long backlog drains in order.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    with db.read() as conn:
        rows = conn.execute(
            """SELECT f.forecast_id, f.cycle_id, f.symbol, f.timeframe,
                      f.horizon_hours, f.provider, f.model_revision,
                      f.data_open_time, f.prediction_created_at, f.direction,
                      f.distribution_kind, f.samples, f.paths, f.status,
                      f.outcome_group, f.provenance
               FROM shadow_forecasts f
               LEFT JOIN shadow_evaluations e ON e.forecast_id = f.forecast_id
               WHERE e.forecast_id IS NULL AND f.status = 'ok'
               ORDER BY f.data_open_time ASC, f.forecast_id ASC
               LIMIT %s""",
            (batch_size,),
        ).fetchall()
    return [dict(row) for row in rows]


def forecast_rows(*, limit: int = 50, symbol: str | None = None,
                  provider: str | None = None) -> list[dict[str, Any]]:
    """The newest observations with their evaluation joined, read-only.

    ``limit`` is validated here rather than left to the SQL: a non-positive value
    reached PostgreSQL as ``LIMIT -1`` and surfaced as a 500 ``internal_error``,
    which tells the caller nothing about their own bad input. This raises the
    same ``ValueError`` the API turns into a 400-class refusal.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    query = (
        "SELECT f.forecast_id, f.cycle_id, f.symbol, f.timeframe, f.horizon_hours, "
        "f.provider, f.model_revision, f.source_revision, f.artifact_hash, "
        "f.data_open_time, f.prediction_created_at, f.generation_kind, "
        "f.outcome_group, f.outcome_reason, f.sample_count, f.distribution_kind, "
        "f.calibration_label, f.calibration_note, f.direction, f.samples, f.status, "
        "f.reason, f.seed, f.duration_ms, f.provenance, "
        "e.status AS evaluation_status, e.reason AS evaluation_reason, "
        "e.realized_direction, e.realized_direction_hit, e.band_coverage, "
        "e.stop_first_rate, e.net_r, e.net_r_reason, e.realized, e.evaluated_at "
        "FROM shadow_forecasts f "
        "LEFT JOIN shadow_evaluations e ON e.forecast_id = f.forecast_id"
    )
    params: list[Any] = []
    conditions = []
    if symbol:
        conditions.append("f.symbol = %s")
        params.append(symbol)
    if provider:
        conditions.append("f.provider = %s")
        params.append(provider)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY f.data_open_time DESC, f.forecast_id DESC LIMIT %s"
    params.append(limit)
    with db.read() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _ratio_rows(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT provider, distribution_kind, status, count(*) AS rows,
                  count(*) FILTER (WHERE direction = 'up') AS up,
                  count(*) FILTER (WHERE direction = 'down') AS down,
                  count(*) FILTER (WHERE direction = 'flat') AS flat
           FROM shadow_forecasts GROUP BY provider, distribution_kind, status
           ORDER BY provider, status"""
    ).fetchall()]


def _evaluation_rows(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT f.provider, e.status, count(*) AS rows,
                  count(*) FILTER (WHERE e.realized_direction_hit) AS direction_hits,
                  count(*) FILTER (WHERE e.realized_direction_hit IS NOT NULL) AS direction_scored,
                  count(*) FILTER (WHERE e.band_coverage) AS band_inside,
                  count(*) FILTER (WHERE e.band_coverage IS NOT NULL) AS band_scored,
                  avg(e.stop_first_rate) AS stop_first_rate,
                  count(*) FILTER (WHERE e.net_r IS NOT NULL) AS net_r_rows
           FROM shadow_forecasts f
           JOIN shadow_evaluations e ON e.forecast_id = f.forecast_id
           GROUP BY f.provider, e.status
           ORDER BY f.provider, e.status"""
    ).fetchall()]


def _provider_rows(provider_rows: list[dict[str, Any]],
                   evaluation_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One block per provider: attempt statuses, verdict counts, evaluation counts.

    Statuses are reported as COUNTS, not rates: with a handful of observations a
    percentage would imply a precision the sample does not have. The direction
    breakdown is a count of verdicts (``up``/``down``/``flat``), which is what a
    deterministic rule and a model both honestly produce.
    """
    blocks: dict[str, dict[str, Any]] = {}
    for row in provider_rows:
        block = blocks.setdefault(str(row["provider"]), {
            "provider": row["provider"],
            "distribution_kind": row["distribution_kind"],
            "attempts": {"rows": 0, "ok": 0, "unavailable": 0, "error": 0, "timeout": 0},
            "verdicts": {"up": 0, "down": 0, "flat": 0},
            "evaluations": {
                "rows": 0, "direction_scored": 0, "direction_hits": 0,
                "band_scored": 0, "band_inside": 0, "net_r_rows": 0,
                "stop_first_rate_mean": None,
            },
        })
        count = int(row["rows"] or 0)
        block["attempts"]["rows"] += count
        if row["status"] in block["attempts"]:
            block["attempts"][row["status"]] += count
        if row["status"] == "ok":
            block["verdicts"]["up"] += int(row["up"] or 0)
            block["verdicts"]["down"] += int(row["down"] or 0)
            block["verdicts"]["flat"] += int(row["flat"] or 0)
    for row in evaluation_rows:
        block = blocks.get(str(row["provider"]))
        if block is None:
            continue
        block["evaluations"]["rows"] += int(row["rows"] or 0)
        block["evaluations"]["direction_scored"] += int(row["direction_scored"] or 0)
        block["evaluations"]["direction_hits"] += int(row["direction_hits"] or 0)
        block["evaluations"]["band_scored"] += int(row["band_scored"] or 0)
        block["evaluations"]["band_inside"] += int(row["band_inside"] or 0)
        block["evaluations"]["net_r_rows"] += int(row["net_r_rows"] or 0)
        if row["stop_first_rate"] is not None:
            block["evaluations"]["stop_first_rate_mean"] = float(row["stop_first_rate"])
    return [blocks[key] for key in sorted(blocks)]


def summary(*, limit: int = 20) -> dict[str, Any]:
    """Read-only aggregate for the brief and the research screen.

    ``status`` is ``unavailable`` while nothing has been observed — an empty
    shadow table is not "0% agreement". Per-provider counts are published as
    counts, not rates, wherever the denominator would be tiny; the ratios that DO
    appear carry the calibration label of the rows they came from.

    ``limit`` is validated up front so a caller passing 0 or a negative number
    gets a clear refusal instead of a PostgreSQL error dressed as a 500.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    with db.read() as conn:
        totals = conn.execute(
            """SELECT count(*) AS rows,
                      count(*) FILTER (WHERE status = 'ok') AS ok,
                      count(*) FILTER (WHERE status = 'unavailable') AS unavailable,
                      count(*) FILTER (WHERE status = 'error') AS errors,
                      count(*) FILTER (WHERE status = 'timeout') AS timeouts,
                      count(*) FILTER (WHERE generation_kind = 'retrospective') AS retrospective,
                      min(data_open_time) AS first_data,
                      max(data_open_time) AS last_data
               FROM shadow_forecasts"""
        ).fetchone()
    rows = int(totals["rows"] or 0)
    if rows == 0:
        return {
            "status": "unavailable",
            "reason": "no shadow observation has been recorded; nothing is claimed",
            "totals": None,
            "providers": [],
            "evaluations": [],
            "recent": [],
        }
    with db.read() as conn:
        provider_rows = _ratio_rows(conn)
        evaluation_rows = _evaluation_rows(conn)
    recent = forecast_rows(limit=limit)
    return {
        "status": "ok",
        "totals": {
            "rows": rows,
            "ok": int(totals["ok"] or 0),
            "unavailable": int(totals["unavailable"] or 0),
            "errors": int(totals["errors"] or 0),
            "timeouts": int(totals["timeouts"] or 0),
            "retrospective": int(totals["retrospective"] or 0),
            "first_data_open_time": totals["first_data"],
            "last_data_open_time": totals["last_data"],
        },
        "providers": _provider_rows(
            [dict(row) for row in provider_rows],
            [dict(row) for row in evaluation_rows],
        ),
        "evaluations": [dict(row) for row in evaluation_rows],
        "recent": recent,
    }
