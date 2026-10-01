"""Daily jobs: POST /api/daily + GET /api/daily/{job_id} (docs/ui-api-contract.md §5).

The worker runs the per-coin loop on the engine's own seams instead of calling
``run_daily`` (contract §5 rev 3): items are enqueued the moment a coin
finishes, ``mode: "mock"`` reuses the runs.py replay machinery instead of
building a graph, and a job clicked from the UI NEVER executes — the engine's
auto-confirm policy belongs to the unattended cron surface, while every UI
plan resolves through the approval gate (§4). One job at a time, sharing
runs.py's registry lock: ``set_config`` is process-wide (trading_graph.py:65),
so a run and a job — or two jobs — would fight over config.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from server import (
    approvals as approvals_mod,
    audit as audit_log,
    runs as runs_mod,
    watchlist as watchlist_mod,
)
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.agents.rating import parse_rating
from tradingagents.dataflows.config import get_config

router = APIRouter(prefix="/api", tags=["daily"])

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed"}

_PROPAGATE_ATTEMPTS = 3
# Seconds before the first propagate retry, then linear per attempt — mirrors
# the engine's unattended runner (execution/daily.py:62).
PROPAGATE_RETRY_WAIT = 20.0

# Stub-venue fixtures for mock jobs: fixed price/cash so the whole plan path
# (sync_portfolio + plan_order) stays offline (contract §5 step 2). The balance
# is quote-only on purpose: any base-currency position would push a full Buy
# plan past the 25% per-asset cap and the demo would enqueue nothing.
MOCK_PRICE = 83078.0
MOCK_CASH = 9500.0

logger = logging.getLogger(__name__)


@dataclass
class DailyJobState:
    job_id: str
    mode: str
    tickers: list[str]
    created_at: str
    status: str = "queued"
    finished_at: str | None = None
    results: list[dict] = field(default_factory=list)


_jobs: dict[str, DailyJobState] = {}


def _active_job() -> DailyJobState | None:
    """The job currently occupying the worker; callers hold the registry lock."""
    return next((job for job in _jobs.values() if job.status in ACTIVE_STATUSES), None)


def active_job_id() -> str | None:
    with runs_mod._registry_lock:
        job = _active_job()
        return job.job_id if job is not None else None


def _new_job_id() -> str:
    base = f"d_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    job_id = base
    suffix = 1
    while job_id in _jobs:
        suffix += 1
        job_id = f"{base}_{suffix}"
    return job_id


def _normalize_tickers(raw: list[str]) -> list[str]:
    """Strip, drop empties and dedupe keeping order (execution/daily.py:95-111)."""
    coins: list[str] = []
    for entry in raw:
        coin = str(entry).strip()
        if coin and coin not in coins:
            coins.append(coin)
    return coins


def _job_payload(job: DailyJobState) -> dict:
    return {
        "generated_at": utc_now_iso(),
        "job_id": job.job_id,
        "status": job.status,
        "mode": job.mode,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "tickers": list(job.tickers),
        "results": [dict(result) for result in job.results],
    }


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --- worker -----------------------------------------------------------------


class _StubVenue:
    """Fixture venue for mock jobs, injected through the bridge's factory seam
    (bridge.py:126-129): every answer is a constant, so sync_portfolio and
    plan_order never touch ccxt or the network. The numbers are demo data."""

    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": MOCK_PRICE, "close": MOCK_PRICE}

    def load_markets(self) -> dict:
        # No market minimums: plan_order falls back to the sizing rules floor,
        # keeping the minimum single-sourced.
        return {}

    def fetch_balance(self) -> dict:
        return {"total": {"USDT": MOCK_CASH}, "free": {"USDT": MOCK_CASH}}


def _build_bridge(config: dict, mock: bool):
    """The venue for the job's plan path; tests monkeypatch this seam like
    approvals._build_bridge. A mock job pins the bridge dry so its plan lines
    and ``mode_at_plan`` always record "dry" — mock plans are demo data and can
    never execute (contract §5, invariant 2)."""
    from tradingagents.execution.bridge import ExchangeBridge

    if mock:
        return ExchangeBridge(config={**config, "exec_live": False}, exchange_factory=_StubVenue)
    return ExchangeBridge(config=config)


def _analyze(job: DailyJobState, coin: str, config: dict) -> tuple[str, str | None]:
    """One coin's analysis. Mock replays the runs.py machinery (no graph, no
    LLM, no vendor); live mirrors the engine's runner (execution/daily.py:
    147-164) — a fresh graph per attempt, because a failed propagate can leave
    LangGraph checkpoint state mid-pipeline. Returns (signal, mock_source)."""
    if job.mode == "mock":
        state_log = runs_mod._find_state_log(config, coin, _today_iso(), None)
        state = runs_mod._read_state_log(state_log) if state_log is not None else None
        if state is None:
            state = runs_mod._synthetic_state(coin, _today_iso())
            return parse_rating(str(state.get("final_trade_decision") or "")), "synthetic"
        return parse_rating(str(state.get("final_trade_decision") or "")), f"replay:{state_log}"

    signal: str | None = None
    for attempt in range(_PROPAGATE_ATTEMPTS):
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph = TradingAgentsGraph(config=config, selected_analysts=("market", "social", "news"))
            _, signal = graph.propagate(coin, _today_iso(), asset_type="crypto")
            return signal, None
        except Exception as exc:
            if attempt == _PROPAGATE_ATTEMPTS - 1:
                raise
            wait = PROPAGATE_RETRY_WAIT * (attempt + 1)
            logger.warning(
                "daily job %s: %s propagate failed (attempt %d, %s: %s); retrying in %.0fs",
                job.job_id, coin, attempt + 1, type(exc).__name__, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError("propagate exhausted its retries without a signal")  # pragma: no cover


def _plan_item(job: DailyJobState, plan) -> dict:
    """The enqueue payload (contract §4): everything the approval gate needs."""
    return {
        "daily_job_id": job.job_id,
        "run_id": None,
        "ticker": plan.ticker,
        "ccxt_symbol": plan.ccxt_symbol,
        "signal": plan.signal,
        "side": plan.side,
        "quantity": plan.quantity,
        "price_est": plan.estimated_price,
        "cost": plan.cost,
        "market": plan.market,
        "leverage": plan.leverage,
        "mode_at_plan": plan.mode,
        "plan_reason": plan.reason,
        "mock": job.mode == "mock",
    }


def _plan_detail(plan) -> dict:
    return {
        "side": plan.side,
        "quantity": plan.quantity,
        "price_est": plan.estimated_price,
        "cost": plan.cost,
        "ccxt_symbol": plan.ccxt_symbol,
        "market": plan.market,
        "leverage": plan.leverage,
        "mode_at_plan": plan.mode,
        "plan_reason": plan.reason,
    }


def _process_coin(job: DailyJobState, bridge, coin: str, config: dict) -> dict:
    """Analyze → plan → enqueue one coin; a coin's failure never stops the loop
    (execution/daily.py:170-173). The plan is never executed here — an
    actionable one goes to the approval queue and waits (§5 invariant 1)."""
    result: dict = {
        "ticker": coin, "signal": None, "reason": None,
        "plan": None, "approval_id": None, "error": None,
    }
    if job.mode == "mock":
        result["mock_source"] = "synthetic"
    try:
        signal, mock_source = _analyze(job, coin, config)
        result["signal"] = signal
        if mock_source is not None:
            result["mock_source"] = mock_source
        portfolio = bridge.sync_portfolio()
        plan = bridge.plan_order(coin, signal, portfolio=portfolio)
        if plan.side is None:
            result["reason"] = plan.reason  # Hold/REVIEW, sizing refusal or risk rejection
            return result
        item = approvals_mod.enqueue(_plan_item(job, plan))
        bridge.audit_plan(plan, action="awaiting_approval",
                          reason="UI daily job: plan awaits approval")
        result["reason"] = "awaiting approval"
        result["plan"] = _plan_detail(plan)
        result["approval_id"] = item["id"]
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
    return result


def _execute(job: DailyJobState, config: dict) -> None:
    job.status = "running"
    try:
        bridge = _build_bridge(config, job.mode == "mock")
        for coin in job.tickers:
            job.results.append(_process_coin(job, bridge, coin, config))
        job.status = "completed"
    except Exception as exc:  # outside the coin loop: the job itself failed
        job.status = "failed"
        audit_log.record("daily_job_failed", job_id=job.job_id,
                         error=f"{type(exc).__name__}: {exc}")
    finally:
        job.finished_at = utc_now_iso()
        audit_log.record("daily_job_completed", job_id=job.job_id, status=job.status,
                         mode=job.mode, tickers=list(job.tickers))


# --- endpoints ----------------------------------------------------------------


@router.post("/daily", status_code=202)
async def create_daily_job(request: Request):
    body = await json_body(request)
    mode = body.get("mode", "live")
    if mode not in ("live", "mock"):
        raise ApiError(400, "validation_error", '"mode" must be "live" or "mock"')
    raw_tickers = body.get("tickers")
    if raw_tickers is None:
        raw_tickers = watchlist_mod.current_symbols()  # the watchlist the UI manages
    elif not isinstance(raw_tickers, list) or not all(isinstance(t, str) for t in raw_tickers):
        raise ApiError(400, "validation_error", '"tickers" must be a list of strings')
    tickers = _normalize_tickers(raw_tickers)
    if not tickers:
        raise ApiError(400, "validation_error",
                       "no tickers to run: the list is empty after normalization")

    config = get_config()  # snapshot: later settings changes do not touch this job
    with runs_mod._registry_lock:  # shared with POST /api/runs (contract §3, §5)
        active_run = runs_mod._active_run()
        if active_run is not None:
            raise ApiError(409, "conflict", "another run is already active",
                           {"run_id": active_run.run_id})
        busy = _active_job()
        if busy is not None:
            raise ApiError(409, "conflict", "another daily job is already active",
                           {"job_id": busy.job_id})
        job = DailyJobState(job_id=_new_job_id(), mode=mode, tickers=tickers,
                            created_at=utc_now_iso())
        _jobs[job.job_id] = job
        payload = _job_payload(job)  # snapshot: the worker may flip to "running" at once
        payload["detail_url"] = f"/api/daily/{job.job_id}"

    audit_log.record("daily_job_created", job_id=job.job_id, mode=mode, tickers=tickers)
    threading.Thread(target=_execute, args=(job, config),
                     name=f"server-daily-{job.job_id}", daemon=True).start()
    return payload


@router.get("/daily/{job_id}")
def get_daily_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        # Job state is in-memory: a server restart loses it (contract §11.2).
        raise ApiError(404, "not_found", f"daily job {job_id} not found")
    return _job_payload(job)
