"""Runs: POST/GET /api/runs and the GET /api/runs/{id}/events SSE stream (§3).

Analysis only — never execution: the endpoint streams the graph exactly like
``cli/run.py`` does and cannot produce an order plan. One run at a time (a
process-wide ``set_config`` at graph build makes parallel graphs fight over
config — trading_graph.py:65), enforced with a registry lock shared with the
future daily-job endpoint. ``mode: "mock"`` replays a saved
``full_states_log_<date>.json`` when one exists, else emits an inline
synthetic fixture, so the UI runs with no LLM key and no network.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server import audit as audit_log
from server.contract import ApiError, json_body, utc_now_iso
from tradingagents.agents.rating import is_review, parse_rating
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbols import safe_ticker_component

router = APIRouter(prefix="/api", tags=["runs"])

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
QUEUED_STATUSES = {"queued", "running"}

# Pause between mock events so the UI shows progress; tests set this to 0.
MOCK_EVENT_DELAY = 0.4

_KEEP_ALIVE_SECONDS = 15.0


@dataclass
class RunState:
    run_id: str
    ticker: str
    trade_date: str
    asset_type: str
    mode: str
    checkpoint: bool | None
    created_at: str
    analysts: list[str] = field(default_factory=list)
    status: str = "queued"
    finished_at: str | None = None
    signal: str | None = None
    is_review: bool = False
    error: dict | None = None
    report_paths: dict | None = None
    # Events are appended under ``cond``; the SSE stream waits on it.
    cond: threading.Condition = field(default_factory=threading.Condition, repr=False, compare=False)
    events: list[dict] = field(default_factory=list)
    seq: int = 0
    seen_message_ids: set = field(default_factory=set, repr=False, compare=False)


_runs: dict[str, RunState] = {}
_registry_lock = threading.Lock()


def _active_run() -> RunState | None:
    for run in _runs.values():
        if run.status in QUEUED_STATUSES:
            return run
    return None


def active_run_id() -> str | None:
    with _registry_lock:
        active = _active_run()
        return active.run_id if active is not None else None


def _analysts_for(asset_type: str) -> tuple[str, ...]:
    # Crypto drops the fundamentals analyst, exactly like the CLI and the
    # daily runner do (daily.py:139-152, cli/prompts.py:88-96).
    if asset_type == "crypto":
        return ("market", "social", "news")
    return ("market", "social", "news", "fundamentals")


def _new_run_id(ticker: str) -> str:
    base = f"r_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{safe_ticker_component(ticker).lower()}"
    run_id = base
    suffix = 1
    while run_id in _runs:
        suffix += 1
        run_id = f"{base}_{suffix}"
    return run_id


def _run_payload(run: RunState) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "mode": run.mode,
        "ticker": run.ticker,
        "trade_date": run.trade_date,
        "asset_type": run.asset_type,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
        "signal": run.signal,
        "is_review": run.is_review,
        "error": run.error,
        "report_paths": run.report_paths,
        "analysts": run.analysts,
        "events_url": f"/api/runs/{run.run_id}/events",
    }


def _valid_trade_date(value: str) -> bool:
    """Canonical YYYY-MM-DD, no later than today (mirrors trading_graph.py:29-40)."""
    try:
        canonical = datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    return canonical and value <= today


def _emit(run: RunState, event_type: str, *, stage: str, role: str | None = None,
          agent: str | None = None, payload: dict) -> None:
    """Append one SSE event; safe to call while already holding ``run.cond``."""
    with run.cond:
        run.seq += 1
        run.events.append({
            "seq": run.seq,
            "ts": utc_now_iso(),
            "type": event_type,
            "role": role or stage,
            "stage": stage,
            "agent": agent,
            "replay": run.mode == "mock",
            "payload": payload,
        })
        run.cond.notify_all()


# --- mock replay -----------------------------------------------------------

_ANALYST_AGENTS = (
    ("market_report", "Market Analyst"),
    ("sentiment_report", "Sentiment Analyst"),
    ("news_report", "News Analyst"),
    ("fundamentals_report", "Fundamentals Analyst"),
)

_RISK_DEBATERS = (
    ("aggressive", "aggressive_history", "Aggressive Analyst"),
    ("conservative", "conservative_history", "Conservative Analyst"),
    ("neutral", "neutral_history", "Neutral Analyst"),
)


def _steps_from_state(state: dict) -> list[tuple[str, str, str | None, dict]]:
    """Map a saved final state (trading_graph.py:346-386) into pipeline-ordered
    events: analyst → debate → trader → risk → pm → decision."""
    steps: list[tuple[str, str, str | None, dict]] = []
    for report_key, agent in _ANALYST_AGENTS:
        content = str(state.get(report_key) or "").strip()
        if not content:
            continue
        steps.append(("analyst_status", "analyst", agent, {"agent": agent, "status": "in_progress"}))
        steps.append(("message", "analyst", agent, {"msg_type": "Agent", "content": content}))
        steps.append(("analyst_status", "analyst", agent, {"agent": agent, "status": "completed"}))

    debate = state.get("investment_debate_state") or {}
    for field_name, agent in (("bull_history", "Bull Researcher"), ("bear_history", "Bear Researcher")):
        content = str(debate.get(field_name) or "").strip()
        if content:
            steps.append(("debate_update", "researcher", agent, {"field": field_name, "content": content}))
    judge = str(debate.get("judge_decision") or "").strip()
    if judge:
        steps.append(("research_manager", "researcher", "Research Manager", {"content": judge}))
    plan = str(state.get("investment_plan") or "").strip()
    if plan:
        steps.append(("research_manager", "researcher", "Research Manager", {"content": plan}))

    trader = str(state.get("trader_investment_decision") or "").strip()
    if trader:
        steps.append(("trader_update", "trader", "Trader", {"content": trader}))

    risk = state.get("risk_debate_state") or {}
    for field_name, history_key, agent in _RISK_DEBATERS:
        content = str(risk.get(history_key) or "").strip()
        if content:
            steps.append(("risk_update", "risk", agent, {"field": field_name, "content": content}))
    risk_judge = str(risk.get("judge_decision") or "").strip()
    if risk_judge:
        steps.append(("pm_decision", "pm", "Portfolio Manager", {"content": risk_judge}))

    decision = str(state.get("final_trade_decision") or "").strip()
    if decision:
        steps.append(("message", "pm", "Portfolio Manager", {"msg_type": "Agent", "content": decision}))
    return steps


def _synthetic_state(ticker: str, trade_date: str) -> dict:
    """Inline fixture (contract §3.2): a compact Hold so the UI can always demo."""
    return {
        "company_of_interest": ticker,
        "trade_date": trade_date,
        "market_report": f"[mock] Market snapshot for {ticker} on {trade_date}: range-bound with mild accumulation.",
        "sentiment_report": f"[mock] Social sentiment for {ticker}: neutral-to-positive, no spike in chatter.",
        "news_report": f"[mock] News flow for {ticker}: no fresh catalysts in the window.",
        "fundamentals_report": "",
        "investment_debate_state": {
            "bull_history": "[mock] Bull researcher: momentum intact, dips are being bought.",
            "bear_history": "[mock] Bear researcher: volume fading, upside looks limited.",
            "history": "",
            "current_response": "",
            "judge_decision": "[mock] Research manager: evidence balanced, no directional edge.",
        },
        "investment_plan": "[mock] Research plan: no action this session.",
        "trader_investment_decision": "[mock] Trader: stay flat, wait for a cleaner setup.",
        "risk_debate_state": {
            "aggressive_history": "[mock] Aggressive: a small starter position is acceptable.",
            "conservative_history": "[mock] Conservative: preserve capital, no entry.",
            "neutral_history": "[mock] Neutral: no edge either way.",
            "history": "",
            "judge_decision": "[mock] Portfolio manager: keep the current allocation.",
        },
        "final_trade_decision": "[mock] Final Decision:\nRating: Hold",
    }


def _find_state_log(config: dict, ticker: str, trade_date: str, source_run: str | None) -> Path | None:
    logs_dir = Path(config.get("results_dir", "")) / safe_ticker_component(ticker) / "TradingAgentsStrategy_logs"
    if not logs_dir.is_dir():
        return None
    exact = logs_dir / f"full_states_log_{trade_date}.json"
    if exact.is_file():
        return exact
    if source_run:
        source = _runs.get(source_run)
        if source is not None:
            hinted = logs_dir / f"full_states_log_{source.trade_date}.json"
            if hinted.is_file():
                return hinted
    # Date-named files, so lexical order is chronological; replay the latest.
    candidates = sorted(logs_dir.glob("full_states_log_*.json"))
    return candidates[-1] if candidates else None


def _read_state_log(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _run_mock(run: RunState, config: dict, source_run: str | None) -> None:
    state_log = _find_state_log(config, run.ticker, run.trade_date, source_run)
    state = _read_state_log(state_log) if state_log is not None else None
    replayed = state is not None
    if not replayed:
        state = _synthetic_state(run.ticker, run.trade_date)
        state_log = None
    report_paths = {"state_log": str(state_log)} if replayed else None
    for event_type, stage, agent, payload in _steps_from_state(state):
        _emit(run, event_type, stage=stage, agent=agent, payload=payload)
        if MOCK_EVENT_DELAY > 0:
            time.sleep(MOCK_EVENT_DELAY)
    _complete(run, parse_rating(str(state.get("final_trade_decision") or "")), report_paths)


def _complete(run: RunState, signal: str, report_paths: dict | None) -> None:
    with run.cond:
        run.signal = signal
        run.is_review = is_review(signal)
        run.report_paths = report_paths
        _emit(run, "run_completed", stage="system", payload={
            "signal": signal, "is_review": run.is_review, "report_paths": report_paths,
        })
        run.status = "completed"
        run.finished_at = utc_now_iso()


# --- live streaming (mirror of cli/run.py:226-323) --------------------------


def _build_graph(config: dict, analysts: tuple[str, ...]):
    # Lazy: the graph import chain pulls LangChain; server startup stays light.
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    return TradingAgentsGraph(analysts, config=config)


def _message_stage(msg_type: str) -> str:
    return "analyst" if msg_type in ("Agent", "Data") else "system"


def _emit_chunk_events(run: RunState, chunk: dict) -> None:
    from cli.display import classify_message_type

    for message in chunk.get("messages") or []:
        msg_id = getattr(message, "id", None)
        if msg_id is not None:
            if msg_id in run.seen_message_ids:
                continue
            run.seen_message_ids.add(msg_id)
        msg_type, content = classify_message_type(message)
        if content and content.strip():
            _emit(run, "message", stage=_message_stage(msg_type), role=msg_type.lower(),
                  payload={"msg_type": msg_type, "content": content})
        for tool_call in getattr(message, "tool_calls", None) or []:
            if isinstance(tool_call, dict):
                name, args = tool_call["name"], tool_call["args"]
            else:
                name, args = tool_call.name, tool_call.args
            _emit(run, "tool_call", stage="system", payload={"tool": name, "args": args})

    for report_key, agent in _ANALYST_AGENTS:
        if chunk.get(report_key):
            _emit(run, "analyst_status", stage="analyst",
                  payload={"agent": agent, "status": "completed"})

    debate = chunk.get("investment_debate_state")
    if debate:
        for field_name in ("bull_history", "bear_history"):
            content = str(debate.get(field_name) or "").strip()
            if content:
                _emit(run, "debate_update", stage="researcher",
                      payload={"field": field_name, "content": content})
        judge = str(debate.get("judge_decision") or "").strip()
        if judge:
            _emit(run, "research_manager", stage="researcher", payload={"content": judge})

    if chunk.get("trader_investment_plan"):
        _emit(run, "trader_update", stage="trader", payload={"content": chunk["trader_investment_plan"]})

    risk = chunk.get("risk_debate_state")
    if risk:
        for field_name, history_key, _agent in _RISK_DEBATERS:
            content = str(risk.get(history_key) or "").strip()
            if content:
                _emit(run, "risk_update", stage="risk", payload={"field": field_name, "content": content})
        judge = str(risk.get("judge_decision") or "").strip()
        if judge:
            _emit(run, "pm_decision", stage="pm", payload={"content": judge})


def _run_live(run: RunState, config: dict) -> None:
    graph = _build_graph(config, tuple(run.analysts))
    final_state: dict = {}
    checkpoint_tid = graph.begin_checkpoint(run.ticker, run.trade_date, run.asset_type)
    try:
        args = graph.propagator.get_graph_args()
        if checkpoint_tid is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_tid
        init_state = graph.create_run_state(run.ticker, run.trade_date, run.asset_type)
        for chunk in graph.graph.stream(graph.checkpoint_input(init_state), **args):
            _emit_chunk_events(run, chunk)
            final_state.update(chunk)
        graph.record_decision(run.ticker, run.trade_date, final_state)
        graph.clear_checkpoint_on_success(run.ticker, run.trade_date, run.asset_type)
    finally:
        graph.end_checkpoint()
    signal = graph.process_signal(final_state.get("final_trade_decision", ""))
    complete_report = graph.save_reports(final_state, run.ticker)
    state_log = (Path(config["results_dir"]) / safe_ticker_component(run.ticker)
                 / "TradingAgentsStrategy_logs" / f"full_states_log_{run.trade_date}.json")
    report_paths = {
        "complete_report": str(complete_report),
        "state_log": str(state_log) if state_log.is_file() else None,
    }
    _complete(run, signal, report_paths)


def _execute(run: RunState, config: dict, source_run: str | None) -> None:
    with run.cond:
        run.status = "running"
        _emit(run, "run_started", stage="system", payload={"analysts": run.analysts})
    try:
        if run.mode == "mock":
            _run_mock(run, config, source_run)
        else:
            _run_live(run, config)
    except Exception as exc:  # a failed run must still reach the terminal SSE event
        error = {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
        with run.cond:
            run.error = error
            run.finished_at = utc_now_iso()
            _emit(run, "run_failed", stage="system", payload={"error": error})
            run.status = "failed"
        audit_log.record("run_failed", run_id=run.run_id, error=error["message"])


# --- endpoints --------------------------------------------------------------


@router.post("/runs", status_code=201)
async def create_run(request: Request):
    body = await json_body(request)
    ticker = body.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ApiError(400, "validation_error", '"ticker" must be a non-empty string')
    ticker = ticker.strip()
    trade_date = body.get("trade_date")
    if not isinstance(trade_date, str) or not _valid_trade_date(trade_date):
        raise ApiError(400, "validation_error",
                       f'"trade_date" must be a YYYY-MM-DD date not in the future, got {trade_date!r}')
    asset_type = body.get("asset_type")
    if asset_type not in ("stock", "crypto"):
        raise ApiError(400, "validation_error", '"asset_type" must be "stock" or "crypto"')
    mode = body.get("mode", "live")
    if mode not in ("live", "mock"):
        raise ApiError(400, "validation_error", '"mode" must be "live" or "mock"')
    checkpoint = body.get("checkpoint")
    if checkpoint is not None and not isinstance(checkpoint, bool):
        raise ApiError(400, "validation_error", '"checkpoint" must be a boolean')
    source_run = body.get("source_run")
    if source_run is not None and (not isinstance(source_run, str) or mode != "mock"):
        raise ApiError(400, "validation_error", '"source_run" is only valid together with mode "mock"')

    config = get_config()  # snapshot: later settings changes do not touch this run
    if checkpoint is not None:
        config = {**config, "checkpoint_enabled": checkpoint}

    with _registry_lock:
        active = _active_run()
        if active is not None:
            raise ApiError(409, "conflict", "another run is already active", {"run_id": active.run_id})
        from server import daily as daily_jobs  # lazy: daily imports this module

        busy_job = daily_jobs._active_job()  # the lock is shared with POST /api/daily (§3, §5)
        if busy_job is not None:
            raise ApiError(409, "conflict", "a daily job is already active", {"job_id": busy_job.job_id})
        run = RunState(
            run_id=_new_run_id(ticker), ticker=ticker, trade_date=trade_date,
            asset_type=asset_type, mode=mode, checkpoint=checkpoint,
            created_at=utc_now_iso(), analysts=list(_analysts_for(asset_type)),
        )
        _runs[run.run_id] = run
        payload = _run_payload(run)  # snapshot: the worker may flip to "running" at once

    audit_log.record("run_created", run_id=run.run_id, ticker=ticker, mode=mode)
    threading.Thread(target=_execute, args=(run, config, source_run),
                     name=f"server-run-{run.run_id}", daemon=True).start()
    return payload


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    run = _runs.get(run_id)
    if run is None:
        raise ApiError(404, "not_found", f"run {run_id} not found")
    with run.cond:
        return _run_payload(run)


@router.get("/runs/{run_id}/events")
def stream_run_events(run_id: str, request: Request):
    run = _runs.get(run_id)
    if run is None:
        raise ApiError(404, "not_found", f"run {run_id} not found")
    cursor = -1
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
        except ValueError as exc:
            raise ApiError(400, "validation_error", "Last-Event-ID must be an integer") from exc
    return StreamingResponse(
        _event_stream(run, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _event_stream(run: RunState, cursor: int):
    """Replay buffered events from ``cursor + 1``, then follow live until the
    run reaches a terminal state and its buffer is drained."""
    while True:
        with run.cond:
            pending = [event for event in run.events if event["seq"] > cursor]
            terminal = run.status in TERMINAL_STATUSES
            if not pending and not terminal:
                run.cond.wait(timeout=_KEEP_ALIVE_SECONDS)
                pending = [event for event in run.events if event["seq"] > cursor]
                terminal = run.status in TERMINAL_STATUSES
                if not pending and not terminal:
                    pending = None  # idle: send a keep-alive comment, keep waiting
        if pending is None:
            yield ": keep-alive\n\n"
        elif pending:
            for event in pending:
                cursor = event["seq"]
                data = json.dumps(event, ensure_ascii=False)
                yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
        elif terminal:
            return
