"""Daily jobs: POST/GET /api/daily + GET /api/daily/{job_id} + cancel (§5).

The worker runs the per-coin loop on the engine's own seams instead of calling
``run_daily`` (contract §5 rev 3): items are enqueued the moment a coin
finishes, ``mode: "mock"`` reuses the runs.py replay machinery instead of
building a graph, and a job clicked from the UI NEVER executes — the engine's
auto-confirm policy belongs to the unattended cron surface, while every UI
plan resolves through the approval gate (§4). One job at a time, sharing
runs.py's registry lock: ``set_config`` is process-wide (trading_graph.py:65),
so a run and a job — or two jobs — would fight over config.

``auto: true`` (rev 4) is the one-click market scan: instead of a manual
watchlist, the worker screens a universe (crypto top-volume via ccxt public
data + US large caps via the Yahoo screener), ranks candidates by a technical
score, and runs the LLM pipeline only on the top ``top_n``. The screen is the
cheap stage; its results ride on the job payload as ``screening``. Stock
candidates are advisory: analysis runs, but the ccxt bridge cannot plan for
them, so their result records the reason and no plan.

Rev 7 — a job is a database row, not a dict that dies with the process. Every
state change is written to ``control.db`` (``job_store``), so the UI can list
past runs and read what each agent produced long after the worker thread is
gone; a job left ``running`` by a crash is reconciled to ``interrupted`` at
the next startup (``reconcile``) instead of blocking the worker forever. The
in-memory ``_jobs`` dict is now only the live working set: the worker's own
handle, and the fast path for polling. A job the operator cancels is marked
terminal immediately — the worker notices between coins and stops, but a coin
already inside an LLM call runs to completion (Python cannot safely kill a
thread mid-flight), so cancel frees the worker lock rather than the network.
"""

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request

from server import (
    approvals as approvals_mod,
    audit as audit_log,
    job_store,
    runs as runs_mod,
    watchlist as watchlist_mod,
)
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.agents.rating import parse_rating
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import crypto_base

router = APIRouter(prefix="/api", tags=["daily"])

ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

_PROPAGATE_ATTEMPTS = 3
# Seconds before the first propagate retry, then linear per attempt — mirrors
# the engine's unattended runner (execution/daily.py:62).
PROPAGATE_RETRY_WAIT = 20.0
_PROGRESS_HEARTBEAT_SECONDS = 60.0

# Auto-scan tuning: how many universe entries to pull per market (freqtrade's
# VolumePairlist pattern: rank by volume, screen the top-N), and how many
# screened candidates graduate into the expensive LLM pipeline.
AUTO_UNIVERSE_PER_ASSET = 40
AUTO_DEFAULT_TOP_N = 5
AUTO_MAX_TOP_N = 20

# Stub-venue fixtures for mock jobs: fixed price/cash so the whole plan path
# (sync_portfolio + plan_order) stays offline (contract §5 step 2). The balance
# is quote-only on purpose: any base-currency position would push a full Buy
# plan past the 25% per-asset cap and the demo would enqueue nothing.
MOCK_PRICE = 83078.0
MOCK_CASH = 9500.0

# Mock auto-scan must not touch ccxt/yfinance: a fixed candidate list keeps the
# demo deterministic and offline, mirroring runs.py's synthetic fallback. Fields
# mirror screening.Candidate.as_dict().
MOCK_AUTO_CANDIDATES: tuple[dict, ...] = (
    {"ticker": "BTC-USD", "asset_type": "crypto", "score": 5.2,
     "last_close": MOCK_PRICE, "momentum_pct": 6.1, "trend_pct": 3.2,
     "volume_ratio": 2.4, "rsi14": 58.0, "atr_pct": 3.4, "dist_52w_pct": -2.1,
     "breakout": True, "avg_dollar_volume": 8.2e9,
     "reasons": ["+6.1% trong 20 phiên", "giá trên SMA20", "trên cả SMA50 (xu hướng tăng kép)",
                 "RVOL x2.4 (khối lượng đột biến)", "sát đỉnh 52 tuần (chỉ ~2%)",
                 "phá đỉnh 20 phiên kèm khối lượng"]},
    {"ticker": "ETH-USD", "asset_type": "crypto", "score": 1.1,
     "last_close": 3120.5, "momentum_pct": 2.2, "trend_pct": 1.1,
     "volume_ratio": 1.2, "rsi14": 52.0, "atr_pct": 3.1, "dist_52w_pct": -18.4,
     "breakout": False, "avg_dollar_volume": 6.1e9, "reasons": []},
    {"ticker": "NVDA", "asset_type": "stock", "score": 4.6,
     "last_close": 187.4, "momentum_pct": 8.4, "trend_pct": 4.5,
     "volume_ratio": 2.1, "rsi14": 63.0, "atr_pct": 3.8, "dist_52w_pct": -4.2,
     "breakout": True, "avg_dollar_volume": 2.4e10,
     "reasons": ["+8.4% trong 20 phiên", "giá trên SMA20", "trên cả SMA50 (xu hướng tăng kép)",
                 "RVOL x2.1 (khối lượng đột biến)", "sát đỉnh 52 tuần (chỉ ~4%)",
                 "phá đỉnh 20 phiên kèm khối lượng"]},
)

logger = logging.getLogger(__name__)


@dataclass
class DailyJobState:
    job_id: str
    mode: str
    tickers: list[str]
    created_at: str
    status: str = "queued"
    finished_at: str | None = None
    last_progress_at: str | None = None
    current_ticker: str | None = None
    results: list[dict] = field(default_factory=list)
    asset_types: dict[str, str] = field(default_factory=dict)
    screening: dict | None = None
    auto_top_n: int | None = None
    # Why the job itself failed (screen or bridge); a per-coin failure lives on
    # that coin's result. Persisted so the UI can explain a failed job.
    error: str | None = None


# Live working set only: the worker's handle and the fast path for polling.
# Durable history lives in ``control.db`` and is read through ``job_store``.
_jobs: dict[str, DailyJobState] = {}


def _persist(job: DailyJobState) -> None:
    """Mirror the job to the database. Raises on failure — a job whose record
    cannot be written is a job the operator cannot see, which is the bug this
    layer exists to fix."""
    job_store.persist(asdict(job))


def _touch_progress(job: DailyJobState, *, current_ticker: str | None = None) -> None:
    """Record durable progress without changing the job's lifecycle status."""
    job.last_progress_at = utc_now_iso()
    job.current_ticker = current_ticker
    _persist(job)


def _run_with_heartbeat(job: DailyJobState, coin: str, bridge, config: dict) -> dict:
    """Run one coin while refreshing the durable heartbeat during slow calls."""
    outcome: dict[str, dict] = {}
    failure: list[BaseException] = []

    def work() -> None:
        try:
            outcome["result"] = _process_coin(job, bridge, coin, config)
        except BaseException as exc:  # re-raise in the worker thread below
            failure.append(exc)

    worker = threading.Thread(target=work, name=f"daily-{job.job_id}-{coin}")
    worker.start()
    while worker.is_alive():
        worker.join(timeout=_PROGRESS_HEARTBEAT_SECONDS)
        if worker.is_alive():
            _touch_progress(job, current_ticker=coin)
    if failure:
        raise failure[0]
    return outcome["result"]


def reconcile() -> list[str]:
    """Close jobs a dead process left active; returns the ids closed.

    Called once at startup. The worker lock is derived from job status, so an
    orphaned ``running`` row would answer 409 to every new job forever.
    """
    closed = job_store.reconcile_orphans()
    for job_id in closed:
        audit_log.record("daily_job_interrupted", job_id=job_id,
                         reason="server restarted while the job was running")
    return closed


def _state_from_row(data: dict) -> DailyJobState:
    return DailyJobState(**data)


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
    # History is in the database, so a restart no longer frees the namespace:
    # check both the live set and the durable one before reusing an id.
    while job_id in _jobs or job_store.load(job_id) is not None:
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
    payload = {
        "generated_at": utc_now_iso(),
        "job_id": job.job_id,
        "status": job.status,
        "mode": job.mode,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "last_progress_at": job.last_progress_at or job.created_at,
        "current_ticker": job.current_ticker,
        "tickers": list(job.tickers),
        "results": [dict(result) for result in job.results],
        "error": job.error,
    }
    if job.auto_top_n is not None:
        payload["asset_types"] = dict(job.asset_types)
        payload["screening"] = job.screening
        payload["auto"] = True
        payload["top_n"] = job.auto_top_n
    return payload


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
    asset_type = job.asset_types.get(coin, "crypto")
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

            analysts = ("market", "social", "news")
            if asset_type == "stock":
                # The stock pipeline adds the fundamentals analyst, exactly like
                # runs.py _analysts_for (server/runs.py:79-86).
                analysts = ("market", "social", "news", "fundamentals")
            graph = TradingAgentsGraph(config=config, selected_analysts=analysts)
            _, signal = graph.propagate(coin, _today_iso(), asset_type=asset_type)
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


def _attach_bracket(job: DailyJobState, plan, coin: str):
    """ATR-derived protective exits on an entry plan (the operator never
    sizes a stop by hand). Live plans read the cached OHLCV series; mock
    plans carry fixed demo distances. A missing ATR leaves the plan
    bracket-less rather than inventing numbers — the audit shows why."""
    if job.mode == "mock":
        price = plan.estimated_price or MOCK_PRICE
        return plan.model_copy(update={
            "stop_loss": round(price * 0.96, 8),
            "take_profit": round(price * 1.08, 8),
        })
    from tradingagents.dataflows.screening import atr_targets

    stop, take = atr_targets(coin, _today_iso(), "crypto")
    if stop is None or take is None:
        return plan
    return plan.model_copy(update={"stop_loss": stop, "take_profit": take})


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
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit,
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
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit,
    }


def _process_coin(job: DailyJobState, bridge, coin: str, config: dict) -> dict:
    """Analyze → plan → enqueue one coin; a coin's failure never stops the loop
    (execution/daily.py:170-173). The plan is never executed here — an
    actionable one goes to the approval queue and waits (§5 invariant 1)."""
    result: dict = {
        "ticker": coin, "signal": None, "reason": None,
        "plan": None, "approval_id": None, "error": None,
        "asset_type": job.asset_types.get(coin, "crypto"),
    }
    if job.mode == "mock":
        result["mock_source"] = "synthetic"
    try:
        signal, mock_source = _analyze(job, coin, config)
        result["signal"] = signal
        if mock_source is not None:
            result["mock_source"] = mock_source
        if result["asset_type"] != "crypto":
            # Advisory-only: the ccxt bridge cannot plan for non-crypto
            # tickers (bridge.py:174-186), so a stock candidate's signal is
            # the deliverable; the approval path stays crypto-executable.
            result["reason"] = (
                f"advisory only — signal from the {result['asset_type']} pipeline; "
                "the venue bridge only executes crypto, so no order plan"
            )
            return result
        if crypto_base(coin) is None:
            # A crypto-universe ticker outside the pipeline's closed
            # executable set (QNT-USD, MOVR-USD): the analysis is real and
            # quotable, but symbol_for cannot map it to a venue pair, so the
            # plan path is advisory here too — same contract as stocks.
            result["reason"] = (
                "advisory only — asset outside the executable crypto set "
                "(exec_symbol_overrides can enable planning); no order plan"
            )
            return result
        portfolio = bridge.sync_portfolio()
        plan = bridge.plan_order(coin, signal, portfolio=portfolio)
        if plan.side == "buy":
            plan = _attach_bracket(job, plan, coin)
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


def _screen_candidates(config: dict, top_n: int) -> dict:
    """The cheap stage of an auto job: build both universes, rank, return the
    payload stored on the job. Vendor failures bubble — the job itself fails
    (a scan that found nothing because the venue is down is not an empty
    result, it is an outage)."""
    from tradingagents.dataflows.screening import (
        crypto_universe,
        screen_universe,
        stock_universe,
    )

    crypto = crypto_universe(config, AUTO_UNIVERSE_PER_ASSET)
    stocks = stock_universe(config, AUTO_UNIVERSE_PER_ASSET)
    candidates = screen_universe(crypto, stocks, _today_iso(), top_n)
    return {
        "universe_count": len(crypto) + len(stocks),
        "universe_crypto": len(crypto),
        "universe_stock": len(stocks),
        "candidates": candidates,
    }


def _run_coin_loop(job: DailyJobState, bridge, config: dict) -> None:
    """One coin at a time, stopping between coins if the operator cancelled.

    A coin already inside an LLM call runs to completion: Python cannot kill a
    thread safely, so cancel frees the worker lock (the status is already
    terminal) rather than the network. Each result is persisted as it lands so
    a crash mid-loop still leaves the coins that finished.
    """
    for coin in job.tickers:
        if job.status == "cancelled":
            break
        _touch_progress(job, current_ticker=coin)
        try:
            job.results.append(_run_with_heartbeat(job, coin, bridge, config))
        finally:
            _touch_progress(job, current_ticker=None)


def _finish(job: DailyJobState, error: str | None = None) -> None:
    """Settle a job's terminal status and write it down.

    A cancelled job keeps the status the cancel endpoint already gave it — the
    worker must not resurrect a job the operator stopped. The durable row is
    written BEFORE the live object flips terminal: the API reads ``_jobs``
    first, so exposing "completed" ahead of the write lets a poll observe a
    terminal job whose restart record still says "running".
    """
    status = job.status if job.status == "cancelled" else (
        "failed" if error is not None else "completed"
    )
    finished_at = job.finished_at or utc_now_iso()
    payload = asdict(job)
    payload.update(
        status=status,
        error=job.error if job.status == "cancelled" else error,
        finished_at=finished_at,
        current_ticker=None,
        last_progress_at=finished_at,
    )
    job_store.persist(payload)
    job.status = status
    if job.status != "cancelled":
        job.error = error
    job.finished_at = finished_at
    job.current_ticker = None
    job.last_progress_at = finished_at
    audit_log.record("daily_job_completed", job_id=job.job_id, status=job.status,
                     mode=job.mode, tickers=list(job.tickers), error=job.error)


def _execute_auto(job: DailyJobState, config: dict) -> None:
    """Screen first, then run the pipeline loop on the top candidates.

    The screening dict is attached to the job before the coin loop so the UI
    polling sees candidates as soon as the (fast) screen finishes. Vendor
    failures in the screen fail the job — a scan that found nothing because
    the venue is down is an outage, not an empty result.
    """
    job.status = "running"
    _touch_progress(job)
    error: str | None = None
    try:
        if job.mode == "mock":
            candidates = [dict(entry) for entry in MOCK_AUTO_CANDIDATES]
            job.screening = {
                "universe_count": len(MOCK_AUTO_CANDIDATES),
                "universe_crypto": 2,
                "universe_stock": 1,
                "candidates": candidates,
                "mock": True,
            }
        else:
            job.screening = _screen_candidates(config, job.auto_top_n or AUTO_DEFAULT_TOP_N)
            candidates = job.screening["candidates"]
        job.tickers = [str(entry["ticker"]) for entry in candidates]
        job.asset_types = {str(entry["ticker"]): str(entry["asset_type"]) for entry in candidates}
        _touch_progress(job)  # the screen is what the UI watches first
        if job.tickers and job.status != "cancelled":
            _run_coin_loop(job, _build_bridge(config, job.mode == "mock"), config)
    except Exception as exc:  # outside the coin loop: the job itself failed
        error = f"{type(exc).__name__}: {exc}"
        audit_log.record("daily_job_failed", job_id=job.job_id, error=error)
    finally:
        _finish(job, error)


def _execute(job: DailyJobState, config: dict) -> None:
    job.status = "running"
    _touch_progress(job)
    error: str | None = None
    try:
        bridge = _build_bridge(config, job.mode == "mock")
        _run_coin_loop(job, bridge, config)
    except Exception as exc:  # outside the coin loop: the job itself failed
        error = f"{type(exc).__name__}: {exc}"
        audit_log.record("daily_job_failed", job_id=job.job_id, error=error)
    finally:
        _finish(job, error)


# --- endpoints ----------------------------------------------------------------


@router.post("/daily", status_code=202)
async def create_daily_job(request: Request):
    body = await json_body(request)
    mode = body.get("mode", "live")
    if mode not in ("live", "mock"):
        raise ApiError(400, "validation_error", '"mode" must be "live" or "mock"')
    auto = body.get("auto", False)
    if not isinstance(auto, bool):
        raise ApiError(400, "validation_error", '"auto" must be a boolean')
    top_n = body.get("top_n", AUTO_DEFAULT_TOP_N)
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= AUTO_MAX_TOP_N:
        raise ApiError(400, "validation_error",
                       f'"top_n" must be an integer between 1 and {AUTO_MAX_TOP_N}')
    if auto and mode == "mock" and body.get("tickers") is not None:
        raise ApiError(400, "validation_error",
                       '"tickers" cannot be combined with "auto": the screen picks the candidates')

    if auto:
        tickers: list[str] = []  # decided by the screen inside the worker
    elif (raw_tickers := body.get("tickers")) is None:
        tickers = watchlist_mod.current_symbols()  # the watchlist the UI manages
    else:
        if not isinstance(raw_tickers, list) or not all(isinstance(t, str) for t in raw_tickers):
            raise ApiError(400, "validation_error", '"tickers" must be a list of strings')
        tickers = _normalize_tickers(raw_tickers)
    if not auto and not tickers:
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
        if auto:
            job.auto_top_n = top_n
        _jobs[job.job_id] = job
        _persist(job)  # durable before the 202 returns: a restart must not lose it
        payload = _job_payload(job)  # snapshot: the worker may flip to "running" at once
        payload["detail_url"] = f"/api/daily/{job.job_id}"

    audit_log.record("daily_job_created", job_id=job.job_id, mode=mode, tickers=tickers,
                     auto=auto, top_n=top_n)
    threading.Thread(target=_execute_auto if auto else _execute,
                     args=(job, config), name=f"server-daily-{job.job_id}",
                     daemon=True).start()
    return payload


def _list_item(data: dict) -> dict:
    """One row of the job history: enough to show what happened, not the payload."""
    return {
        "job_id": data["job_id"],
        "status": data["status"],
        "mode": data["mode"],
        "auto": data["auto_top_n"] is not None,
        "top_n": data["auto_top_n"],
        "created_at": data["created_at"],
        "finished_at": data["finished_at"],
        "tickers": data["tickers"],
        "results_count": data.get("results_count", 0),
        "approval_count": data.get("approval_count", 0),
        "error": data.get("error"),
    }


@router.get("/daily")
def list_daily_jobs(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Job history, newest first — the screen the operator opens to answer
    "is something running, and what happened last time?".

    ``active_job_id`` is the single field the UI needs for its running banner;
    it is read from the live registry, so a job this process is not running can
    never be reported as active.
    """
    if status is not None and status not in ACTIVE_STATUSES | TERMINAL_STATUSES:
        raise ApiError(400, "validation_error",
                       f'"status" must be one of {sorted(ACTIVE_STATUSES | TERMINAL_STATUSES)}')
    items, total = job_store.list_jobs(status, page_size, (page - 1) * page_size)
    return {
        "generated_at": utc_now_iso(),
        "items": [_list_item(item) for item in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "active_job_id": active_job_id(),
    }


@router.get("/daily/{job_id}")
def get_daily_job(job_id: str):
    job = _jobs.get(job_id)  # live first: the worker owns the freshest state
    if job is not None:
        return _job_payload(job)
    stored = job_store.load(job_id)
    if stored is None:
        raise ApiError(404, "not_found", f"daily job {job_id} not found")
    return _job_payload(_state_from_row(stored))


@router.post("/daily/{job_id}/cancel", status_code=202)
def cancel_daily_job(job_id: str):
    """Stop a running job: mark it terminal now so the worker lock is released.

    The worker notices between coins and stops; the coin in flight finishes
    (a thread cannot be killed safely mid-LLM-call), so this returns as soon as
    the job is marked cancelled, not when the thread exits. Cancelling is never
    gated — turning a thing off is the safe direction.
    """
    with runs_mod._registry_lock:
        job = _jobs.get(job_id)
        if job is None:
            stored = job_store.load(job_id)
            if stored is None:
                raise ApiError(404, "not_found", f"daily job {job_id} not found")
            raise ApiError(409, "conflict",
                           f"daily job {job_id} is {stored['status']}, not running",
                           {"status": stored["status"]})
        if job.status not in ACTIVE_STATUSES:
            raise ApiError(409, "conflict",
                           f"daily job {job_id} is {job.status}, not running",
                           {"status": job.status})
        job.status = "cancelled"
        job.finished_at = utc_now_iso()
        job.current_ticker = None
        job.last_progress_at = job.finished_at
        _persist(job)
    audit_log.record("daily_job_cancelled", job_id=job_id, done=len(job.results))
    return _job_payload(job)
