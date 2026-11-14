"""Operating brief: GET /api/brief (plan §5.1, UX plan §7).

One read-only aggregate the dashboard asks its questions of: what needs
handling, what the reconciled net result is, what the open risk is, and what
the bot has been doing. It is a *composition* of stores that already exist —
the outcome ledger, the cycle funnel, the health/limits config and the same
portfolio display read ``/api/portfolio`` uses — never a second venue read of
its own (see ``portfolio`` below) and never a writer.

Honesty rules carried from the plans (each one has a test in
``tests/test_operating_brief.py``):

- money and counts are ``None`` when unknown, never a fabricated ``0``;
- ledger totals stay per-currency; nothing is FX-converted;
- coverage rows are per-cycle windows and are never summed into one figure;
- "no signal" (a valid market/confirmation/proposal rejection, risk refusal,
  awaiting approval or no order) is a different bucket from "pipeline error"
  (failed/interrupted/timeout) — the brief reports both, separately;
- exposure is only claimed when the position notional, its currency and the
  account equity line up; otherwise it is ``None`` with the reason attached;
- open-heat is never claimed: risk-at-stop per open position is not part of the
  position snapshot;
- protective state is only claimed for *executed cycles the runtime recorded*,
  and only over a stated window; venue positions carry no durable account
  binding to their entry order (docs/ui-api-contract.md §6), so per-position
  protection is reported as unverified rather than guessed;
- a section whose read fails degrades locally (``status: unavailable`` plus the
  reason) and never blanks the rest of the brief.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from server import (
    brief_active_forecast,
    brief_activity,
    db,
    experiment_store,
    futures_cycle_store,
    portfolio as portfolio_module,
    portfolio_snapshot,
)
from server.brief_results import _results_section
from server.contract import utc_now_iso
from server.settings import effective_exec_mode
from tradingagents.dataflows.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["operating-brief"])

STALE_AFTER = 30

# Risk limits the brief may SHOW but never writes (plan §7.2). Read-only
# display of the operator's own configured caps; no PUT path exists here.
_LIMIT_KEYS = {
    "total_exposure_pct": "risk_max_total_exposure_pct",
    "derivatives_exposure_soft_pct": "risk_max_derivatives_exposure_pct",
    "derivatives_exposure_hard_pct": "risk_max_derivatives_exposure_hard_pct",
    "open_heat_pct": "risk_max_open_heat_pct",
}

_DISCLAIMER = (
    "Read-only operations brief. Unreconciled figures are labelled estimated; "
    "missing data is reported as missing, never as zero. Research framework — "
    "not financial advice."
)

# Terminal cycle statuses in which the runtime itself failed rather than
# deciding not to trade. Everything else non-entering is "no signal".
_PIPELINE_ERROR_STATUSES = ("failed", "interrupted", "recovery_pending")


def _today_start_iso() -> str:
    """Midnight UTC of the operator's current day, as the project counts days.

    ``datetime.now(UTC).date()`` is the same convention ``risk._halted_today``
    and ``portfolio._get_portfolio`` use, so "today" means one thing here too.
    """
    return f"{datetime.now(UTC).date().isoformat()}T00:00:00Z"


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    """A section body that failed or has nothing to claim — never a zero."""
    return {"status": "unavailable", "reason": reason, **extra}


# ---------------------------------------------------------------------------
# status


def _status_section(portfolio_body: dict[str, Any] | None) -> dict[str, Any]:
    """Automation modes, venue, halt and pending approvals in one block.

    Spot and Futures are reported separately (never collapsed into one "Auto"
    flag) so the dashboard can state exactly which mode is armed, and the venue
    profile rides along because a demo account is not a production account.
    """
    config = get_config()
    try:
        from server.autoloop import gates

        mode_gates = gates()
    except Exception as exc:  # a broken mode read must not blank the brief
        logger.warning("brief: automation gates unreadable: %r", exc)
        mode_gates = {"auto_spot": None, "auto_futures": None, "any_auto_enabled": None}

    halted: bool | None = None
    halt_reason: str | None = None
    pending: int | None = None
    server_status: str | None = None
    audit_readable: bool | None = None
    try:
        from server.health import get_health

        health = get_health()
        server_status = health.get("status")
        halted = health.get("halted_today")
        pending = health.get("pending_approvals")
        audit_readable = health.get("audit_log_readable")
    except Exception as exc:
        logger.warning("brief: health unreadable: %r", exc)
    try:
        from server import audit as audit_log

        _, halt_reason = audit_log.halt_state()
    except Exception:  # the reason is decoration; halted_today is the signal
        halt_reason = None

    futures = (portfolio_body or {}).get("futures")
    futures = futures if isinstance(futures, dict) else None
    return {
        "exec_mode": effective_exec_mode(),
        "auto_spot": mode_gates.get("auto_spot"),
        "auto_futures": mode_gates.get("auto_futures"),
        "any_auto_enabled": mode_gates.get("any_auto_enabled"),
        "venue_profile": (futures or {}).get("venue_profile"),
        "account_scope": (futures or {}).get("account_scope"),
        "halted_today": halted,
        "halt_reason": halt_reason,
        "pending_approvals": pending,
        "server_status": server_status,
        "audit_log_readable": audit_readable,
        "quote_currency": config.get("exec_quote_currency"),
    }


# ---------------------------------------------------------------------------
# risk


def _futures_position_count(futures: dict[str, Any] | None) -> int | None:
    """Open futures positions, or ``None`` when the read is not proven complete.

    A partial or unavailable position read is not a flat account
    (docs/ui-api-contract.md §6), so the count stays ``None`` rather than
    reporting the rows that happened to arrive as the whole book.
    """
    if not isinstance(futures, dict) or futures.get("status") != "ok":
        return None
    positions = futures.get("positions")
    read = futures.get("positions_read")
    if isinstance(positions, list) and isinstance(read, dict) and read.get("status") == "complete":
        return len(positions)
    return None


def _exposure(futures: dict[str, Any] | None) -> dict[str, Any]:
    """Derivatives exposure as % of equity — claimed only when it is provable.

    The account equity and every open notional must be in the same currency and
    every notional must be present. Anything else (mixed currencies, a missing
    notional, no equity) is ``None`` with the reason: the ledger's no-FX rule
    applies to display too.
    """
    if not isinstance(futures, dict) or futures.get("status") != "ok":
        return {"value_pct": None, "currency": None, "reason": "no verified futures account read"}
    read = futures.get("positions_read")
    if not isinstance(read, dict) or read.get("status") != "complete":
        return {"value_pct": None, "currency": None,
                "reason": "the position read is not proven complete"}
    positions = futures.get("positions")
    if not isinstance(positions, list):
        return {"value_pct": None, "currency": None, "reason": "no position list available"}
    if not positions:
        return {"value_pct": 0.0, "currency": futures.get("equity_currency"), "reason": None}

    currencies: set[str | None] = set()
    total = 0.0
    for position in positions:
        notional = position.get("notional")
        if notional is None:
            return {"value_pct": None, "currency": None,
                    "reason": "a futures position reports no notional"}
        total += abs(float(notional))
        currencies.add(position.get("notional_currency"))
    equity = futures.get("equity")
    equity_currency = futures.get("equity_currency")
    if equity is None or equity_currency is None:
        return {"value_pct": None, "currency": None, "reason": "the account equity is unavailable"}
    if len(currencies) != 1 or None in currencies:
        return {"value_pct": None, "currency": None,
                "reason": "positions report no single notional currency"}
    notional_currency = next(iter(currencies))
    if notional_currency != equity_currency:
        return {"value_pct": None, "currency": None,
                "reason": f"notional currency {notional_currency} differs from equity "
                          f"currency {equity_currency}; no FX conversion is performed"}
    if float(equity) <= 0:
        return {"value_pct": None, "currency": equity_currency, "reason": "the account equity is not positive"}
    return {"value_pct": (total / float(equity)) * 100, "currency": equity_currency, "reason": None}


def _limits() -> dict[str, Any]:
    """The operator's configured caps, read-only (never written from here)."""
    config = get_config()
    values: dict[str, Any] = {}
    for label, key in _LIMIT_KEYS.items():
        raw = config.get(key)
        try:
            values[label] = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            values[label] = None
    values["note"] = "Configured risk limits, displayed read-only. Change them in Cấu hình."
    return values


def _risk_section(portfolio_body: dict[str, Any] | None) -> dict[str, Any]:
    body = portfolio_body or {}
    futures = body.get("futures")
    futures = futures if isinstance(futures, dict) else None
    spot_positions = body.get("positions")
    spot_count: int | None = None
    if isinstance(spot_positions, list) and body.get("positions_source") in ("venue", "audit"):
        spot_count = len(spot_positions)
    futures_count = _futures_position_count(futures)
    read = (futures or {}).get("positions_read")
    protection = _protection_section()
    return {
        "open_positions": {"spot": spot_count, "futures": futures_count},
        "positions_read": read if isinstance(read, dict) else None,
        "exposure": _exposure(futures),
        "heat": {
            "value_pct": None,
            "reason": "risk-at-stop per open position is not part of the position "
                      "snapshot, so open heat is not claimed",
        },
        "limits": _limits(),
        "protection": protection,
        "futures_status": (futures or {}).get("status"),
        "futures_error": (futures or {}).get("error"),
        "account_scope": (futures or {}).get("account_scope"),
    }


def _protection_section() -> dict[str, Any]:
    """Protective state over the executed cycles the runtime recorded today.

    Bracket placement is recorded per cycle (``protection_state``) — real
    evidence. Binding a *venue* position to its entry order is not durable
    (docs/ui-api-contract.md §6), so per-position protection is reported as
    unverified instead of inferred.
    """
    try:
        counts = _protection_counts()
    except Exception as exc:
        logger.warning("brief: protection read failed: %r", exc)
        return {
            "scope": "today_executed_cycles", "configured": None, "missing": None,
            "unknown": None, "open_positions_verified": False,
            "reason": f"the protection read failed: {type(exc).__name__}",
        }
    return {
        **counts,
        "scope": "today_executed_cycles",
        "open_positions_verified": False,
        "reason": (
            "Counts cover executed cycles recorded today. An open venue position carries "
            "no durable binding to its entry order, so position-level protection is not "
            "claimed — check the cycle in Bot activity."
        ),
    }


def _protection_counts() -> dict[str, Any]:
    with db.read() as conn:
        rows = conn.execute(
            """SELECT COALESCE(payload->>'protection_state', 'unknown') AS protection_state,
                      count(*) AS n
               FROM futures_cycles
               WHERE status = 'completed' AND phase = 'entry'
                 AND payload->>'execution_status' = 'executed'
                 AND COALESCE(payload->>'executed', 'true') <> 'false'
                 AND updated_at >= %s
               GROUP BY 1""",
            (_today_start_iso(),),
        ).fetchall()
    counts = {"configured": 0, "missing": 0, "unknown": 0}
    for row in rows:
        key = row["protection_state"] if row["protection_state"] in counts else "unknown"
        counts[key] += int(row["n"])
    return counts


def _experiments_section() -> dict[str, Any]:
    """Out-of-sample experiment registry (stage 2), compact for the brief."""
    try:
        summary = experiment_store.summary(limit=5)
    except Exception as exc:
        logger.warning("brief: experiment registry unreadable: %r", exc)
        return _unavailable(f"the experiment registry could not be read: {type(exc).__name__}")
    if summary.get("status") != "ok":
        return _unavailable(summary.get("reason") or "the experiment registry is empty")
    runs = summary.get("runs") or []
    latest = runs[0] if runs else None
    return {
        "status": "ok",
        "total": summary.get("total"),
        "latest": ({
            "run_id": latest.get("run_id"),
            "hypothesis": latest.get("hypothesis"),
            "created_at": latest.get("created_at"),
        } if isinstance(latest, dict) else None),
        "verdicts": (summary.get("comparison_verdicts") or {}).get(
            (latest or {}).get("run_id"), {}
        ) if isinstance(latest, dict) else {},
        "note": "Simulated, paired comparisons. Never a winner claim and never a promotion.",
    }


# ---------------------------------------------------------------------------
# needs attention


def _needs_attention(
    status: dict[str, Any],
    results: dict[str, Any],
    risk: dict[str, Any],
    activity: dict[str, Any],
    portfolio_body: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Prioritised, deduplicated action list. Server-side, so vi/en agree.

    Priority 1 = act now, 2 = watch closely, 3 = informational. One entry per
    ``kind`` (lowest priority number wins, counts merge), because a repeated
    banner is how an operator learns to ignore banners.
    """
    items: list[dict[str, Any]] = []

    def add(kind: str, priority: int, message_code: str, link: str,
            detail: str | None = None, **params: Any) -> None:
        items.append({
            "id": kind,
            "kind": kind,
            "priority": priority,
            "message_code": message_code,
            "message_params": params,
            "detail": detail,
            "link": link,
        })

    if status.get("server_status") == "degraded":
        broken = []
        if status.get("audit_log_readable") is False:
            broken.append("audit log unreadable")
        add("server_degraded", 1, "brief.attention.serverDegraded", "/audit",
            detail="; ".join(broken) or None)

    if status.get("halted_today") is True:
        add("risk_halted", 1, "brief.attention.riskHalted", "/audit",
            detail=status.get("halt_reason"))

    protection = risk.get("protection") or {}
    missing = protection.get("missing")
    if isinstance(missing, int) and missing > 0:
        add("protection_unconfirmed", 1, "brief.attention.protectionMissing", "/cycles",
            count=missing)

    pending = status.get("pending_approvals")
    if isinstance(pending, int) and pending > 0:
        add("approvals_pending", 2, "brief.attention.approvalsPending", "/approvals",
            count=pending)

    coverage = results.get("ledger", {}).get("coverage")
    if isinstance(coverage, list):
        for row in coverage:
            if row.get("status") == "error":
                add("coverage_error", 1, "brief.attention.coverageError", "/audit",
                    detail="; ".join(row.get("warnings") or []) or None)
            elif row.get("status") == "partial":
                add("coverage_partial", 2, "brief.attention.coveragePartial", "/audit",
                    detail="; ".join(row.get("warnings") or []) or None)

    snapshot = (portfolio_body or {}).get("snapshot")
    if isinstance(snapshot, dict) and (
        snapshot.get("cache") == "stale" or snapshot.get("dirty") or snapshot.get("pending")
    ):
        add("stale_snapshot", 2, "brief.attention.staleSnapshot", "/",)

    pipeline_errors = activity.get("pipeline_error_cycles")
    if isinstance(pipeline_errors, int) and pipeline_errors > 0:
        add("pipeline_errors", 3, "brief.attention.pipelineErrors", "/cycles",
            count=pipeline_errors)

    # An empty ledger is not an action item: the results block already states it
    # ("unavailable" plus the reason). Repeating it here would teach the operator
    # to skim this list, which is the one thing it must not become.

    # Dedup by kind, keeping the most urgent priority, then stable order.
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        existing = merged.get(item["kind"])
        if existing is None or item["priority"] < existing["priority"]:
            merged[item["kind"]] = item
    return sorted(merged.values(), key=lambda item: (item["priority"], item["kind"]))


# ---------------------------------------------------------------------------
# assembly


def _read_portfolio() -> dict[str, Any] | None:
    """The same display read ``/api/portfolio`` performs, reused verbatim.

    The brief is the dashboard's single read of the account, so the dashboard
    does not poll ``/api/portfolio`` as well — one read replaces one read
    instead of adding a second venue call. The body is passed through
    unchanged, which keeps ``generated_at``, the snapshot metadata, the
    warnings and ``positions_read`` intact for the cards that consume it.

    A venue failure degrades this section only; the brief still answers.
    """
    try:
        return portfolio_snapshot.read_portfolio(
            get_config(),
            build_bridge=portfolio_module._build_bridge,
            get_config=get_config,
            load_portfolio=portfolio_module._get_portfolio,
            stale_after=STALE_AFTER,
        )
    except Exception as exc:
        logger.warning("brief: portfolio read failed: %r", exc)
        return None


def build_brief() -> dict[str, Any]:
    """Assemble the read-only brief. No writes, no venue calls beyond portfolio."""
    portfolio_body = _read_portfolio()
    status = _status_section(portfolio_body)
    results = _results_section(portfolio_body)
    risk = _risk_section(portfolio_body)
    activity = brief_activity._activity_section(_today_start_iso())
    return {
        "generated_at": utc_now_iso(),
        "stale_after": STALE_AFTER,
        "disclaimer": _DISCLAIMER,
        "status": status,
        # Raw checkpoint of the deliverable's own funnel: read-only, and the
        # same numbers the Bot activity screen shows.
        "funnel": _funnel_section(),
        "results": results,
        "risk": risk,
        "activity": activity,
        # The active forecast is a separate top-level block for the latest cycle.
        "active_forecast": brief_active_forecast._active_forecast_section(),
        "experiments": _experiments_section(),
        # The portfolio body rides along verbatim so the dashboard can render
        # its cards from one poll without losing account identity or freshness.
        "portfolio": portfolio_body,
        "needs_attention": _needs_attention(status, results, risk, activity, portfolio_body),
        "read_only": True,
    }


def _funnel_section() -> dict[str, Any]:
    try:
        summary = futures_cycle_store.futures_cycle_summary()
        return {"status": "ok", "buckets": summary, "total": sum(summary.values())}
    except Exception as exc:
        logger.warning("brief: funnel unreadable: %r", exc)
        return _unavailable(f"the cycle funnel could not be read: {type(exc).__name__}")


@router.get("/brief")
def get_brief():
    """The operating brief: one read-only aggregate for the dashboard."""
    return build_brief()
