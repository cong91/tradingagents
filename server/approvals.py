"""Approvals: GET /api/approvals + approve/reject (docs/ui-api-contract.md §4).

The queue is new API-layer state, persisted as JSONL at
``server/data/approvals.jsonl`` (one full snapshot line per item, rewritten
atomically on change, so a crash cannot leave a half-written queue; the
restart-recovery story lives in contract §11.1). Approve is the ONLY path that
places a real order: gate ``exec_live`` fail-closed (``412`` when it is not a
literal True), RiskGuard re-check fail-closed inside ``execute_order``, then
the plan resolved to ``executed``/``execute_failed`` — an item claimed and
dropped would be a dead-end state, so approve always finishes the transition.
Plan data is synthetic for mock daily jobs (``mock: true``) and can never
become an order (``409 mock_not_executable`` before the claim, so the item
stays pending and rejectable). Every approve/reject is written to the server's
own audit trail (``server/audit.record``), separate from the engine's
execution log. Only ``POST /api/daily`` plans may enqueue items; the enqueue
hook is exposed here for that integration.
"""

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request

from server import audit as audit_log, paths
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.bridge import PreTradeRejection
from tradingagents.execution.sizing import PlannedOrder

if TYPE_CHECKING:
    from tradingagents.execution.bridge import ExchangeBridge

router = APIRouter(prefix="/api", tags=["approvals"])

EXPIRY_HOURS = 24

_store_lock = threading.Lock()


def _load_items() -> list[dict]:
    path = paths.APPROVALS_PATH
    if not path.exists():
        return []
    items = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return items
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn last line must not take the whole queue down
        if isinstance(item, dict) and item.get("id"):
            items.append(item)
    return items


def _save_items(items: list[dict]) -> None:
    paths.ensure_data_dir()
    tmp_path = paths.APPROVALS_PATH.with_name(paths.APPROVALS_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    tmp_path.replace(paths.APPROVALS_PATH)


def _next_id(items: list[dict]) -> str:
    numbers = []
    for item in items:
        item_id = str(item.get("id", ""))
        if item_id.startswith("ap_") and item_id[3:].isdigit():
            numbers.append(int(item_id[3:]))
    return f"ap_{max(numbers, default=0) + 1:04d}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find(items: list[dict], approval_id: str) -> dict | None:
    return next((item for item in items if item.get("id") == approval_id), None)


def _pending_item(items: list[dict], approval_id: str) -> dict:
    item = _find(items, approval_id)
    if item is None:
        raise ApiError(404, "not_found", f"approval {approval_id} not found")
    if item["status"] != "pending":
        raise ApiError(409, "conflict", f"approval {approval_id} is already {item['status']}",
                       {"status": item["status"]})
    created = _parse_ts(item.get("created_at"))
    if created is not None and datetime.now(timezone.utc) - created > timedelta(hours=EXPIRY_HOURS):
        raise ApiError(410, "expired", f"approval {approval_id} expired after {EXPIRY_HOURS}h")
    return item


def enqueue(plan: dict) -> dict:
    """Add one planned order to the queue (the only producer is POST /api/daily)."""
    with _store_lock:
        items = _load_items()
        item = {
            "id": _next_id(items),
            "created_at": utc_now_iso(),
            "daily_job_id": plan.get("daily_job_id"),
            "run_id": plan.get("run_id"),
            "ticker": plan.get("ticker"),
            "ccxt_symbol": plan.get("ccxt_symbol"),
            "signal": plan.get("signal"),
            "side": plan.get("side"),
            "quantity": plan.get("quantity"),
            "price_est": plan.get("price_est"),
            "cost": plan.get("cost"),
            "market": plan.get("market", "spot"),
            "leverage": plan.get("leverage", 1.0),
            "mode_at_plan": plan.get("mode_at_plan", "dry"),
            "plan_reason": plan.get("plan_reason"),
            "mock": bool(plan.get("mock", False)),
            "status": "pending",
            "resolved_at": None,
            "resolution": None,
            "executed": None,
            "mode": None,
            "order_id": None,
        }
        items.append(item)
        _save_items(items)
    return item


def pending_count() -> int:
    with _store_lock:
        return sum(1 for item in _load_items() if item["status"] == "pending")


def _build_bridge(config: dict) -> "ExchangeBridge":
    """The venue for the approve pipeline; tests monkeypatch this seam to inject
    a fake exchange factory (contract §4; engine seam bridge.py:126-129)."""
    from tradingagents.execution.bridge import ExchangeBridge

    return ExchangeBridge(config=config)


def _rebuildable(item: dict) -> bool:
    """The item carries a plan worth rebuilding: a tradeable side and quantity."""
    side = item.get("side")
    quantity = item.get("quantity")
    if side not in ("buy", "sell"):
        return False
    return isinstance(quantity, (int, float)) and not isinstance(quantity, bool) and quantity > 0


def _rebuild_plan(item: dict) -> PlannedOrder:
    """(a) Rebuild the PlannedOrder from the stored item (contract §4 step 4).

    ``mode`` is informational only — ``execute_order`` computes the real gate
    at execute time (sizing.py:49-53), so the rebuilt plan stays "dry".
    """
    return PlannedOrder(
        ticker=str(item.get("ticker") or ""),
        ccxt_symbol=str(item.get("ccxt_symbol") or ""),
        signal=str(item.get("signal") or ""),
        side=item["side"],
        quantity=float(item["quantity"]),
        estimated_price=item.get("price_est"),
        cost=item.get("cost"),
        needs_review=False,
        reason=None,
        market=item.get("market") or "spot",
        leverage=float(item.get("leverage") or 1.0),
    )


def _resolve(
    approval_id: str,
    *,
    status: str,
    executed: bool | None = None,
    mode: str | None = None,
    order_id: str | None = None,
    resolution: str | None = None,
) -> dict | None:
    """(7) Final item transition under the store lock; reloads fresh state."""
    with _store_lock:
        items = _load_items()
        item = _find(items, approval_id)
        if item is None:  # vanished between claim and resolve: trail still holds the record
            return None
        item["status"] = status
        item["resolved_at"] = utc_now_iso()
        if executed is not None:
            item["executed"] = executed
        if mode is not None:
            item["mode"] = mode
        if order_id is not None:
            item["order_id"] = order_id
        if resolution is not None:
            item["resolution"] = resolution
        _save_items(items)
        return item


def _fail_execute(approval_id: str, reason: str) -> None:
    """Execute refused/failed: item → execute_failed + server trail."""
    _resolve(approval_id, status="execute_failed", resolution=reason)
    audit_log.record("approval_execute_failed", approval_id=approval_id, reason=reason)


def _placed_order_id(message: str) -> str | None:
    """Parse the order id out of the "WAS placed" audit-failure message
    (bridge.py:466-469 embeds ``repr(order_id)``)."""
    match = re.search(r"order '([^']*)' WAS placed", message)
    return match.group(1) if match else None


@router.get("/approvals")
def list_approvals(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
):
    # Read under the store lock: on Windows, os.replace into approvals.jsonl
    # raises PermissionError while another handle holds the file open, so an
    # unsynchronized load racing enqueue/approve/save can 500 intermittently
    # (the UI polls this route every 15s — contract §4).
    with _store_lock:
        items = _load_items()
    matching = [item for item in items if status is None or item["status"] == status]
    return {
        "generated_at": utc_now_iso(),
        "items": matching[offset:offset + limit],
        "pending_count": sum(1 for item in items if item["status"] == "pending"),
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_approval(approval_id: str, request: Request):
    await json_body(request)  # CSRF rule §0: application/json required, body may be {}
    config = get_config()
    if config.get("exec_live") is not True:
        raise ApiError(
            412, "gate_closed",
            "approving requires exec_live=True in config; the FR5 gate is closed — "
            "plans stay pending. Arm the gate (operator action outside this API) and retry.",
        )
    halted, halt_reason = audit_log.halt_state()  # 503 when the audit log is unreadable
    if halted:
        raise ApiError(403, "risk_halted", "risk guard is halted today", {"reason": halt_reason})

    # Claim under the store lock; the refusals before the flip leave the item
    # pending, so it stays resolvable (reject or a later approve).
    with _store_lock:
        items = _load_items()
        item = _pending_item(items, approval_id)  # 404 / 409 not-pending / 410 expired
        if item.get("mock") is True:
            raise ApiError(
                409, "mock_not_executable",
                f"approval {approval_id} was planned by a mock daily job; synthetic plans "
                "cannot become real orders — reject it instead",
                {"mock": True},
            )
        if not _rebuildable(item):
            raise ApiError(
                409, "conflict", f"approval {approval_id} carries no executable plan to rebuild"
            )
        item["status"] = "approved"  # claim: the idempotency fence (contract §4 step 3)
        _save_items(items)

    audit_log.record("approval_granted", approval_id=item["id"], ticker=item["ticker"],
                     side=item["side"], quantity=item["quantity"],
                     daily_job_id=item.get("daily_job_id"))

    try:
        bridge = _build_bridge(config)
    except Exception as exc:  # no usable venue: nothing can be rebuilt into an order
        _fail_execute(approval_id, f"venue not available: {exc}")
        raise ApiError(502, "execute_failed", f"venue not available: {exc}") from exc

    warning: str | None = None
    portfolio = None
    try:
        portfolio = bridge.sync_portfolio()
    except Exception as exc:  # venue unreadable: guard's percent checks fail-open (risk.py:512-520)
        warning = f"portfolio sync failed ({type(exc).__name__}: {exc}); percentage limits skipped fail-open"
        audit_log.record("approval_portfolio_sync_failed", approval_id=item["id"], reason=warning)

    order = _rebuild_plan(item)  # (a) rebuild the plan from the item
    mode = bridge.effective_mode()
    try:
        order_id = bridge.execute_order(order, confirm=True, portfolio=portfolio)  # (b)+(c)
    except PermissionError as exc:
        _fail_execute(approval_id, str(exc))
        raise ApiError(403, "risk_halted", "risk guard refused execution",
                       {"reason": str(exc)}) from exc
    except PreTradeRejection as exc:
        _fail_execute(approval_id, str(exc))
        raise ApiError(502, "execute_failed", str(exc)) from exc
    except RuntimeError as exc:
        # bridge.py:464-469: the order WAS placed and only the audit write failed —
        # it must never look like a failure, or a retry would duplicate it.
        if "WAS placed" not in str(exc):
            _fail_execute(approval_id, str(exc))
            raise ApiError(502, "execute_failed", str(exc)) from exc
        order_id = _placed_order_id(str(exc))
        mode, executed = "live", True
        warning = str(exc)
        _resolve(approval_id, status="executed", executed=True, mode=mode, order_id=order_id)
        audit_log.record("approval_executed", approval_id=approval_id, mode=mode,
                         executed=True, order_id=order_id, warning=warning)
    except Exception as exc:  # engine audits before re-raising (bridge.py:449-451)
        _fail_execute(approval_id, f"{type(exc).__name__}: {exc}")
        raise ApiError(502, "execute_failed", f"{type(exc).__name__}: {exc}") from exc
    else:
        executed = mode == "live"
        _resolve(approval_id, status="executed", executed=executed, mode=mode, order_id=order_id)
        audit_log.record("approval_executed", approval_id=approval_id, mode=mode,
                         executed=executed, order_id=order_id)

    return {
        "id": approval_id,
        "status": "executed",
        "executed": executed,
        "mode": mode,
        "order_id": order_id,
        "risk": {"allowed": True, "halted": False, "reason": None},
        "audit_action": order.side or "no_order",
        "warning": warning,
    }


@router.post("/approvals/{approval_id}/reject")
async def reject_approval(approval_id: str, request: Request):
    body = await json_body(request)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ApiError(400, "validation_error", '"reason" must be a string')

    with _store_lock:
        items = _load_items()
        item = _pending_item(items, approval_id)
        item["status"] = "rejected"
        item["resolution"] = reason
        item["resolved_at"] = utc_now_iso()
        _save_items(items)

    audit_log.record("approval_denied", approval_id=item["id"], ticker=item["ticker"],
                     reason=reason, run_id=item["run_id"])
    return item
