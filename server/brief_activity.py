"""Read-only activity projection for the operating brief."""

from __future__ import annotations

import logging
from typing import Any

from server import db

logger = logging.getLogger("server.operating_brief")

# Cycles the funnel treats as in-flight are not "did not enter": the decision
# has not been made yet. Mirrors futures_cycle_store's claimed/running exclusion.
_IN_FLIGHT_STATUSES = ("claimed", "running")


def _activity_section(start: str) -> dict[str, Any]:
    """Today's funnel: how many cycles, how many reached an order, and why not.

    The cycle path is the derivatives path, so this block says so rather than
    implying it covers Spot.
    """
    try:
        counts = _cycle_counts(start)
        reasons, pipeline_errors = _no_entry_reasons(start)
    except Exception as exc:
        logger.warning("brief: cycle funnel unreadable: %r", exc)
        return {
            "scope": "futures",
            "window": "today_utc",
            "cycles_today": None,
            "analysed_today": None,
            "entries_today": None,
            "no_entry_top_reasons": [],
            "pipeline_error_cycles": None,
            "reason": f"the cycle funnel could not be read: {type(exc).__name__}",
            "note": _NO_ENTRY_NOTE,
        }
    return {
        "scope": "futures",
        "window": "today_utc",
        **counts,
        "no_entry_top_reasons": reasons,
        "pipeline_error_cycles": pipeline_errors,
        "note": _NO_ENTRY_NOTE,
    }


_NO_ENTRY_NOTE = (
    "Cycles that decided not to trade (market/confirmation/proposal rejection, risk "
    "refusal, awaiting approval, no order) are counted separately from cycles whose "
    "pipeline failed; the two are never merged."
)


def _cycle_counts(start: str) -> dict[str, Any]:
    _entered = (
        "status = 'completed' AND phase = 'entry' "
        "AND payload->>'execution_status' = 'executed' "
        "AND COALESCE(payload->>'executed', 'true') <> 'false'"
    )
    with db.read() as conn:
        row = conn.execute(
            f"""SELECT count(*) AS cycles,
                       count(*) FILTER (WHERE (status NOT IN ('rejected') OR phase <> 'market'
                                                 OR {_entered})
                                        AND NOT (status = 'rejected' AND phase = 'forecast'
                                                 AND COALESCE(payload->>'kronos_policy', '') = 'defer')) AS analysed,
                       count(*) FILTER (WHERE {_entered}) AS entries
                FROM futures_cycles WHERE updated_at >= %s""",
            (start,),
        ).fetchone()
    return {
        "cycles_today": int(row["cycles"]),
        "analysed_today": int(row["analysed"]),
        "entries_today": int(row["entries"]),
    }


def _no_entry_reasons(start: str) -> tuple[list[dict[str, Any]], int]:
    """The top reasons a cycle did not enter, plus the pipeline-error count.

    The reason text is passed through verbatim (it is the runtime's own words);
    the ``code`` is derived from the status/phase the runtime recorded, never
    parsed out of the sentence.
    """
    with db.read() as conn:
        rows = conn.execute(
            """SELECT
                   CASE
                     WHEN status IN ('failed', 'interrupted', 'recovery_pending') THEN 'pipeline_error'
                     WHEN payload->>'execution_status' = 'failed' THEN 'pipeline_error'
                     WHEN status = 'rejected' AND phase = 'forecast'
                          AND payload->>'kronos_policy' = 'defer' THEN 'pipeline_error'
                     WHEN status = 'rejected' AND phase = 'market' THEN 'market_rejected'
                     WHEN status = 'rejected' AND phase = 'confirmation' THEN 'confirmation_rejected'
                     WHEN status = 'rejected' AND phase = 'proposal' THEN 'proposal_rejected'
                     WHEN payload->>'execution_status' = 'awaiting_approval' THEN 'awaiting_approval'
                     WHEN payload->>'execution_status' = 'risk_rejected' THEN 'risk_rejected'
                     WHEN payload->>'execution_status' = 'not_executed' THEN 'not_executed'
                     ELSE 'other'
                   END AS code,
                   COALESCE(payload->>'execution_reason', payload->>'plan_reason',
                            payload->>'failure_reason', payload->>'market_rejection_reason',
                            payload->>'confirmation_reason') AS reason,
                   count(*) AS n,
                   (array_agg(cycle_id ORDER BY updated_at DESC))[1] AS sample_cycle_id
               FROM futures_cycles
               WHERE updated_at >= %s
                 AND status <> ALL(%s)
                 AND NOT (status = 'completed' AND phase = 'entry'
                          AND COALESCE(payload->>'execution_status', '') = 'executed'
                          AND COALESCE(payload->>'executed', 'true') <> 'false')
               GROUP BY 1, 2
               ORDER BY n DESC, code ASC""",
            (start, list(_IN_FLIGHT_STATUSES)),
        ).fetchall()

    pipeline_errors = 0
    reasons: list[dict[str, Any]] = []
    for row in rows:
        count = int(row["n"])
        if row["code"] == "pipeline_error":
            pipeline_errors += count
            continue
        reasons.append({
            "code": row["code"],
            "reason": row["reason"],
            "count": count,
            "sample_cycle_id": row["sample_cycle_id"],
        })
    return reasons[:3], pipeline_errors
