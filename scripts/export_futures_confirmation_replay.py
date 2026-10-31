"""Export a deterministic replay fixture from the archived futures telemetry.

The confirmation gate in ``server/autoloop.py::_confirmation_state`` rejects
futures candidates on ``doji_candle`` / ``direction_mismatch``. To evaluate that
rule against alternatives (higher-timeframe dominance + bounded lower-timeframe
pullback) the replay needs the real reject distribution: which cycles the gate
refused, what reason it recorded, and which closed-candle directions survived.

The production store is transient, so this exporter reads an archived snapshot
of the ``futures_cycles`` table and writes:

* the outcome funnel (market → confirmation → proposal → entry buckets, the
  same buckets ``server/job_store.futures_cycle_summary`` classifies), grouped
  by signal, 1h/4h candle direction, doji, bracket R:R and intent;
* the signal × (1h, 4h) direction matrix of every direction-bearing snapshot;
* one replay row per cycle with every field the gate replay needs — never the
  candle OHLC of rejected pairs (the archive predates snapshot-on-reject and no
  local candle store exists), which is exactly the gap the replay documents.

Usage::

    python scripts/export_futures_confirmation_replay.py \
        --source archive/legacy-server-data-20261021/control.db \
        --output tests/fixtures/futures_confirmation_replay.json

Read-only over the archive; no network, no venue calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

# Payload keys the confirmation-reject rows actually carry; anything the gate
# replay would need that is absent from this set is a documented telemetry gap.
_CONFIRM_REJECT_KEYS = {
    "confirmation_reason", "market_rejection_reason", "requested_ticker",
    "signal", "signal_reason", "unified_symbol",
}


def _rr_ratio(plan: dict) -> float | None:
    """Reward:risk of a recorded plan bracket; None when not computable."""
    entry, stop, take = plan.get("estimated_price"), plan.get("stop_loss"), plan.get("take_profit")
    if entry is None or stop is None or take is None:
        return None
    risk, reward = abs(entry - stop), abs(take - entry)
    if risk <= 0:
        return None
    return round(reward / risk, 4)


def export(source: Path) -> dict:
    con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    table_rows: list[tuple] = list(con.execute(
        "SELECT cycle_id, symbol, status, phase, payload, one_hour_open, four_hour_open, created_at"
        " FROM futures_cycles ORDER BY created_at"
    ))
    first_cycle, last_cycle = con.execute(
        "SELECT min(created_at), max(created_at) FROM futures_cycles"
    ).fetchone()
    con.close()

    funnel: Counter = Counter()
    confirm_reasons: Counter = Counter()
    confirm_keys: set = set()
    intent_rejects = 0
    snapshot_matrix: Counter = Counter()
    rrs: list[float] = []
    replay_rows = []
    for cycle_id, symbol, status, phase, payload, one_open, four_open, _created in table_rows:
        data = json.loads(payload) if payload else {}
        snap = data.get("snapshot") or {}
        plan = data.get("plan") or {}
        one_h, four_h = snap.get("one_hour_direction"), snap.get("four_hour_direction")
        rr = _rr_ratio(plan)
        if rr is not None:
            rrs.append(rr)

        # Funnel buckets mirror server/job_store.futures_cycle_summary.
        if status == "completed" and phase == "entry":
            if data.get("execution_status") == "executed":
                funnel["entered"] += 1
            elif data.get("execution_status") == "not_executed":
                funnel["not_executed"] += 1
            elif "execution_status" not in data:
                funnel["completed_without_execution_status"] += 1
            else:
                funnel["completed_other"] += 1
        elif status == "rejected":
            funnel[f"{phase}_rejected"] += 1
        elif status == "failed":
            funnel["execution_failed"] += 1
        else:
            funnel[f"{status}:{phase}"] += 1

        if status == "rejected" and phase == "confirmation":
            confirm_reasons[data.get("confirmation_reason") or "<absent>"] += 1
            confirm_keys.update(data.keys())
            if data.get("intent") or (data.get("proposal_validation") or {}).get("intent"):
                intent_rejects += 1
        if one_h and four_h:
            snapshot_matrix[(data.get("signal"), one_h, four_h)] += 1

        replay_rows.append({
            "cycle_id": cycle_id,
            "symbol": symbol,
            "status": status,
            "phase": phase,
            "confirmation_reason": data.get("confirmation_reason"),
            "signal": data.get("signal"),
            "signal_reason": data.get("signal_reason"),
            "intent": data.get("intent"),
            "one_hour_open": one_open,
            "four_hour_open": four_open,
            "one_hour_direction": one_h,
            "four_hour_direction": four_h,
            "execution_status": data.get("execution_status"),
            "plan_rr": rr,
        })

    return {
        "source": str(source),
        "window": {"first_cycle": first_cycle, "last_cycle": last_cycle},
        "funnel": dict(sorted(funnel.items())),
        "confirmation_reject_reasons": dict(sorted(confirm_reasons.items())),
        "confirmation_reject_payload_keys": sorted(confirm_keys),
        "confirmation_reject_fields_expected": sorted(_CONFIRM_REJECT_KEYS),
        "confirmation_reject_missing_fields": [
            "snapshot.one_hour_direction", "snapshot.four_hour_direction",
            "signal_at_reject", "intent", "candle_ohlcv",
        ],
        "intent_rejects_recorded": intent_rejects,
        # Every archived row whose snapshot carries both closed-candle
        # directions (completed, not_executed, and the single failed row).
        "direction_bearing_snapshot_matrix": [
            {"signal": signal, "one_hour_direction": one_h, "four_hour_direction": four_h, "count": count}
            for (signal, one_h, four_h), count in sorted(snapshot_matrix.items(), key=lambda kv: str(kv[0]))
        ],
        "plan_rr_summary": {
            "plans_with_computable_rr": len(rrs),
            "min_rr": min(rrs) if rrs else None,
            "median_rr": sorted(rrs)[len(rrs) // 2] if rrs else None,
            "max_rr": max(rrs) if rrs else None,
        },
        "rows": replay_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, required=True, help="archived control.db (read-only)")
    parser.add_argument("--output", type=Path, required=True, help="fixture JSON to write")
    args = parser.parse_args()
    report = export(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(report['rows'])} rows)")
    print("funnel:", json.dumps(report["funnel"], indent=2))
    print("confirmation_reject_reasons:", report["confirmation_reject_reasons"])
    print("intent_rejects_recorded:", report["intent_rejects_recorded"])
    print("plan_rr_summary:", report["plan_rr_summary"])


if __name__ == "__main__":
    main()
