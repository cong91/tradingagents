"""Audit: GET /api/audit over the engine's execution log + the server's own trail.

Two concerns, one domain: reading the append-only execution audit JSONL the
RiskGuard replays (``exec_log_path``, default ``~/.tradingagents/execution/
audit.jsonl``), and appending the server's own action trail for every
state-changing API action (``server/data/audit.jsonl``). Reading never fails
open for the UI: an unreadable existing log answers ``503
audit_log_unreadable``, consistent with the execute-time fail-closed read
(risk.py:122-147). A missing log is a fresh install, not an error.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query

from server import paths
from server.contract import ApiError, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.audit import append_event

router = APIRouter(prefix="/api", tags=["audit"])


def record(action: str, **details) -> None:
    """Append one line to the server's own action trail (UTF-8 JSONL)."""
    append_event(paths.SERVER_AUDIT_PATH, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        **details,
    })


def read_events() -> tuple[list[dict], int]:
    """(events, skipped_line_count) from the engine's execution audit log.

    Raises ``ApiError`` 503 when the log exists but cannot be read. Control
    lines (``{"event": "halt"}``) are kept: the halt state is computed from
    them. Each event gains ``_line_no`` for provenance.
    """
    path = Path(str(get_config().get("exec_log_path", ""))).expanduser()
    if not path.exists():
        return [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ApiError(503, "audit_log_unreadable", f"audit log {path} cannot be read: {exc}") from exc
    events: list[dict] = []
    skipped = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1  # same silent-skip policy as risk.py:148-157, but counted
            continue
        if isinstance(event, dict):
            event["_line_no"] = line_no
            events.append(event)
        else:
            skipped += 1
    return events, skipped


def halt_state(events: list[dict] | None = None) -> tuple[bool, str | None]:
    """(halted_today, last halt reason), per risk.py:163-173: a halt line today
    not yet followed by a halt_reset today. Raises 503 when the log is
    unreadable — the approval gate must fail closed with the execute path."""
    if events is None:
        events, _ = read_events()
    from tradingagents.execution.risk import _halted_today

    halted = _halted_today(events, datetime.now(timezone.utc).date())
    reason = None
    for event in events:
        if event.get("event") == "halt":
            reason = event.get("reason")
    return halted, reason if halted else None


def _halt_bookmarks(events: list[dict]) -> tuple[str | None, str | None]:
    last_halt = last_reset = None
    for event in events:
        kind = event.get("event")
        if kind == "halt":
            last_halt = event.get("timestamp")
        elif kind == "halt_reset":
            last_reset = event.get("timestamp")
    return last_halt, last_reset


def _valid_day(value: str | None) -> bool:
    if value is None:
        return True
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


@router.get("/audit")
def get_audit(
    ticker: str | None = Query(None),
    action: str | None = Query(None),
    phase: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    if not _valid_day(from_date) or not _valid_day(to_date):
        raise ApiError(400, "validation_error", '"from"/"to" must be YYYY-MM-DD dates')
    events, skipped = read_events()

    def matches(event: dict) -> bool:
        day = str(event.get("timestamp") or "")[:10]
        if ticker is not None and event.get("ticker") != ticker:
            return False
        if action is not None and event.get("action") != action:
            return False
        if phase is not None and event.get("phase") != phase:
            return False
        if from_date is None and to_date is None:
            return True
        return bool(day) and (from_date is None or day >= from_date) and (to_date is None or day <= to_date)

    matching = [event for event in events if matches(event)]
    matching.reverse()  # newest first
    start = (page - 1) * page_size
    halted, halt_reason = halt_state(events)
    last_halt, last_reset = _halt_bookmarks(events)
    return {
        "generated_at": utc_now_iso(),
        "items": matching[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total_matching": len(matching),
        "total_lines": len(events),
        "halted_today": halted,
        "halt_reason": halt_reason,
        "last_halt": last_halt,
        "last_halt_reset": last_reset,
        "skipped_lines": skipped,
    }
