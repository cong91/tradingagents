"""One-off: backfill the real entry timestamp into durable position state.

The time-stop reads ``snapshot.opened_at``; positions opened before that field
existed would otherwise be treated as brand new. This script writes the entry
time recorded in the engine audit log (the ``execute`` line carrying the venue
order id) so the monitor judges the position's real age.

The monitor itself stamps ``now()`` when it finds ``opened_at`` missing, and
that synthetic value is indistinguishable from a real entry time afterwards.
The audit log is therefore the authority: when it holds an earlier timestamp
for the entry order, that timestamp wins. ``--apply`` writes it; without the
flag the script only prints the plan.

Usage (inside the API container, after the new image is built):
    python /tmp/backfill_opened_at.py            # dry run, prints the plan
    python /tmp/backfill_opened_at.py --apply    # writes the state
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from server import job_store
from tradingagents.dataflows.config import get_config

APPLY = "--apply" in sys.argv


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)



def entry_times_from_audit(path: Path) -> dict[str, str]:
    """Map venue order id -> ISO timestamp of its 'execute' audit line."""
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("phase") != "execute" or not event.get("order_id"):
            continue
        order_id = str(event["order_id"]).split(",")[0].strip()
        if order_id and event.get("timestamp"):
            found.setdefault(order_id, str(event["timestamp"]))
    return found


def main() -> int:
    audit_path = Path(str(get_config().get("exec_log_path", ""))).expanduser()
    entries = entry_times_from_audit(audit_path)
    print(f"audit: {audit_path} ({len(entries)} entry timestamps)")

    for state in job_store.active_position_states():
        key = state["position_key"]
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        order_id = str(state.get("entry_order_id") or key.rsplit(":", 1)[-1])
        recorded = _parse(snapshot.get("opened_at"))
        audited = _parse(entries.get(order_id))
        if audited is None:
            print(f"{key}: no audit entry for order {order_id} — skip (monitor keeps its stamp)")
            continue
        if recorded is not None and recorded <= audited:
            print(f"{key}: opened_at={snapshot['opened_at']} is already the earliest known — skip")
            continue
        # The audit log records the real entry; a later stamp is the monitor's
        # synthetic "found it missing, wrote now()" value.
        print(
            f"{key}: opened_at {snapshot.get('opened_at')} -> {entries[order_id]} "
            f"(order {order_id})"
        )
        if not APPLY:
            continue
        updated = {**state, "snapshot": {**snapshot, "opened_at": entries[order_id]}}
        updated.pop("revision", None)
        if job_store.save_position_state(key, state["revision"], updated):
            print(f"{key}: saved")
        else:
            print(f"{key}: revision conflict — rerun")
    if not APPLY:
        print("\ndry run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
