"""PostgreSQL persistence for the experiment registry (migration 0009).

One concern per the approved seam: register a run with its locked manifest,
append per-repeat results idempotently, record the paired comparisons the
locked criteria produced, and read them back. Writes are
``INSERT ... ON CONFLICT DO NOTHING`` against the migration's uniqueness keys,
so re-recording the same run changes nothing. There is no promotion path: the
store holds verdicts as data and exposes no writer for runtime config.

An empty registry answers ``unavailable`` with a reason — never fabricated
statistics.
"""

from __future__ import annotations

import json as _json_module
import uuid
from datetime import UTC, datetime
from typing import Any

from server import db


def _json(value: Any) -> str:
    return _json_module.dumps(value, default=str)


def _uuid() -> str:
    return str(uuid.uuid4())


def _ms_to_ts(value: Any):
    """Epoch-ms int -> aware UTC datetime for a timestamptz column; None passes."""
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, UTC)


def register_run(manifest: dict[str, Any], *, artifact_dir: str) -> str:
    """Insert the run row with its locked manifest; idempotent by run_id.

    Returns the run_id. A re-registration of the same run_id with a DIFFERENT
    manifest digest is refused — criteria are locked, not revised.
    """
    digest = manifest.get("manifest_digest")
    if not digest:
        raise ValueError("register_run needs the locked manifest_digest")
    criteria = manifest.get("criteria")
    if not isinstance(criteria, dict):
        raise ValueError("register_run needs the locked criteria dict")
    with db.session() as conn:
        existing = conn.execute(
            "SELECT manifest_digest FROM experiment_runs WHERE run_id = %s",
            (manifest["run_id"],),
        ).fetchone()
        if existing is not None:
            if existing["manifest_digest"] != digest:
                raise ValueError(
                    f"run {manifest['run_id']} is already registered under a different "
                    "manifest digest; criteria are locked, not revised"
                )
            return manifest["run_id"]
        window = manifest.get("window") or ""
        window_start = window_end = None
        if ":" in str(window):
            from tradingagents.experiments.walkforward import holdout_start_ms_from_iso
            start_text, _, end_text = str(window).partition(":")
            window_start = _ms_to_ts(holdout_start_ms_from_iso(start_text))
            window_end = _ms_to_ts(holdout_start_ms_from_iso(end_text))
        conn.execute(
            """INSERT INTO experiment_runs
               (run_id, kind, hypothesis, manifest, manifest_digest, criteria,
                seed, repeat_count, variants, window_start, window_end,
                holdout_start, horizon_days, embargo_days, data_source,
                git_sha, config_hash, artifact_dir)
               VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id) DO NOTHING""",
            (
                manifest["run_id"], manifest.get("kind", "walkforward"),
                manifest["hypothesis"], _json(manifest), digest, _json(criteria),
                int(manifest["seed"]), int(manifest["repeat"]),
                _json(manifest["variants"]), window_start, window_end,
                _ms_to_ts(holdout_start_ms_from_iso(criteria["holdout_start"]))
                if criteria.get("holdout_start") else None,
                int(criteria.get("horizon_days") or 0),
                int(manifest.get("embargo_days") or 0),
                str(manifest.get("data") or ""), manifest.get("git_sha"),
                manifest.get("config_hash"), artifact_dir,
            ),
        )
    return manifest["run_id"]


def record_results(run_id: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Append per-repeat rows; idempotent per (run, variant, repeat_index).

    A row whose metric_value is None stays None (unknown funding, unproducible
    net figure) — the column is nullable on purpose; missing is not zero.

    ``metric_name`` is required: the column is NOT NULL and every registered
    value must say which metric it is. The producer is
    ``experiments.runner._run_repeats``, which takes it from the LOCKED
    criteria; a row without it is refused here by name instead of raising a
    bare KeyError deep in the INSERT.
    """
    new = duplicate = 0
    with db.session() as conn:
        for row in rows:
            missing = [key for key in ("metric_name", "variant", "repeat", "seed",
                                       "manifest_digest") if not row.get(key) and row.get(key) != 0]
            if missing:
                raise ValueError(
                    f"experiment result row is missing required key(s): {missing}"
                )
            metric_value = row.get("metric_value")
            inserted = conn.execute(
                """INSERT INTO experiment_results
                   (result_id, run_id, variant, repeat_index, seed, manifest_digest,
                    metric_name, metric_value, llm_tokens, llm_calls, llm_latency_ms, report)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (run_id, variant, repeat_index) DO NOTHING""",
                (
                    _uuid(), run_id, row["variant"], int(row["repeat"]), int(row["seed"]),
                    row["manifest_digest"], row["metric_name"],
                    None if metric_value is None else float(metric_value),
                    int(row.get("llm_tokens") or 0), int(row.get("llm_calls") or 0),
                    float(row.get("llm_latency_ms") or 0.0), _json(row.get("report") or {}),
                ),
            ).rowcount
            if inserted:
                new += 1
            else:
                duplicate += 1
    return {"new": new, "duplicates": duplicate}


def record_comparison(
    run_id: str, comparison: dict[str, Any]
) -> dict[str, int]:
    """One paired comparison row; idempotent per (run, challenger, control, metric)."""
    with db.session() as conn:
        inserted = conn.execute(
            """INSERT INTO experiment_comparisons
               (comparison_id, run_id, challenger, control, metric_name, pairs,
                mean_difference, interval_low, interval_high, confidence,
                verdict, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, challenger, control, metric_name) DO NOTHING""",
            (
                _uuid(), run_id, comparison["challenger"], comparison["control"],
                comparison["metric"], int(comparison.get("pairs") or 0),
                comparison.get("mean_difference"),
                comparison.get("interval_low"), comparison.get("interval_high"),
                comparison.get("confidence"), comparison["verdict"],
                comparison.get("reason") or "",
            ),
        ).rowcount
    return {"new": int(inserted or 0), "duplicates": 0 if inserted else 1}


def list_runs(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Registered runs, newest first, with their locked criteria digests."""
    with db.read() as conn:
        rows = conn.execute(
            """SELECT run_id, kind, hypothesis, manifest_digest, seed, repeat_count,
                      variants, window_start, window_end, holdout_start,
                      horizon_days, embargo_days, data_source, git_sha,
                      config_hash, artifact_dir, created_at
               FROM experiment_runs
               ORDER BY created_at DESC, run_id
               LIMIT %s OFFSET %s""",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM experiment_runs").fetchone()["n"]
    return {"status": "ok", "total": total, "runs": [dict(row) for row in rows]}


def run_results(run_id: str) -> dict[str, Any]:
    """One run's registered row plus its per-repeat rows and comparisons.

    Unknown run_id answers ``unavailable`` with the reason — an empty result
    is never presented as a run with zero repeats.
    """
    with db.read() as conn:
        run = conn.execute(
            """SELECT run_id, kind, hypothesis, manifest, manifest_digest, criteria,
                      seed, repeat_count, variants, window_start, window_end,
                      holdout_start, horizon_days, embargo_days, data_source,
                      git_sha, config_hash, artifact_dir, created_at
               FROM experiment_runs WHERE run_id = %s""",
            (run_id,),
        ).fetchone()
        if run is None:
            return {
                "status": "unavailable",
                "reason": f"no experiment registered under run_id {run_id!r}",
            }
        results = conn.execute(
            """SELECT variant, repeat_index, seed, metric_name, metric_value,
                      llm_tokens, llm_calls, llm_latency_ms, report, recorded_at
               FROM experiment_results WHERE run_id = %s
               ORDER BY variant, repeat_index""",
            (run_id,),
        ).fetchall()
        comparisons = conn.execute(
            """SELECT challenger, control, metric_name, pairs, mean_difference,
                      interval_low, interval_high, confidence, verdict, reason,
                      created_at
               FROM experiment_comparisons WHERE run_id = %s
               ORDER BY challenger""",
            (run_id,),
        ).fetchall()
    payload = dict(run)
    payload["status"] = "ok"
    payload["results"] = [dict(row) for row in results]
    payload["comparisons"] = [dict(row) for row in comparisons]
    return payload


def summary(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """The registry-level read: runs list + a per-run verdict roll-up."""
    listing = list_runs(limit=limit, offset=offset)
    verdicts: dict[str, dict[str, int]] = {}
    if listing["runs"]:
        with db.read() as conn:
            for run in listing["runs"]:
                rows = conn.execute(
                    """SELECT verdict, count(*) AS n FROM experiment_comparisons
                       WHERE run_id = %s GROUP BY verdict""",
                    (run["run_id"],),
                ).fetchall()
                verdicts[run["run_id"]] = {row["verdict"]: row["n"] for row in rows}
    return {
        "status": "ok" if listing["total"] else "unavailable",
        "reason": None if listing["total"] else "the experiment registry is empty",
        "total": listing["total"],
        "runs": listing["runs"],
        "comparison_verdicts": verdicts,
    }


__all__ = [
    "list_runs",
    "record_comparison",
    "record_results",
    "register_run",
    "run_results",
    "summary",
]
