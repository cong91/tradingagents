"""Run a candidate through analysis and its authorized execution policy."""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from typing import Any

from server import (
    autoloop_cycle_records,
    autoloop_execution,
    autoloop_futures_context,
    autoloop_futures_execution,
    futures_cycle_telemetry,
    job_store,
    kronos_context,
)
from server.auto_modes import AutoMode
from server.autoloop_telemetry import CycleTelemetry
from server.contract import utc_now_iso
from server.futures_decision_context import build_futures_portfolio, render_futures_decision_context
from tradingagents.agents.rating import RATINGS_5_TIER, parse_rating
from tradingagents.execution import daily as execution_daily, futures_account as futures_accounts
from tradingagents.execution.auto_authorization import (
    AuthorizationSnapshot,
    AutoAuthorizationError,
)
from tradingagents.execution.daily import DailyResult
from tradingagents.forecast.block import ForecastBlock

logger = logging.getLogger(__name__)
_ANALYSTS_CRYPTO = ("market", "social", "news")
_ANALYSTS_STOCK = ("market", "social", "news", "fundamentals")
_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER

def _process_candidate(
    bridge: Any, candidate: dict, config: dict, build_graph: Callable,
    derivatives: Any | None = None, *, deadline: float | None = None,
    cancelled: threading.Event | None = None,
    execution_lock: threading.Lock | None = None,
    telemetry: CycleTelemetry | None = None,
    mode: AutoMode | None = None,
    authorization: AuthorizationSnapshot | None = None,
    on_cycle_checkpoint: Callable[[dict], None] | None = None,
) -> DailyResult:
    """Analyze → plan → the engine's confirm policy for one candidate.

    Stock candidates are advisory: analysis runs, but the ccxt bridge cannot
    plan for them (bridge.py:174-186), so the signal is the deliverable.
    Crypto candidates go through ``DerivativesExecutor`` when the derivatives
    executor is supplied (the cycle arms it when ``exec_derivatives`` is a
    literal True in config), else through the spot bridge. Either way plans
    are NEVER resolved here beyond ``_handle_plan`` — the 3-gate auto-confirm
    policy alone decides execute-vs-wait, and what it does not execute is
    only audited.
    """
    def persist_progress(*args: Any, **kwargs: Any) -> None:
        if autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            futures_cycle_telemetry.persist_progress(*args, **kwargs)

    def record_rejection(*args: Any, **kwargs: Any) -> None:
        if autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            autoloop_cycle_records._record_futures_cycle_rejection(*args, **kwargs)

    ticker = str(candidate["ticker"])
    asset_type = str(candidate.get("asset_type") or "crypto")
    result = DailyResult(ticker=ticker)
    telemetry = telemetry or CycleTelemetry()
    futures_cycle_id: str | None = None
    futures_cycle_payload: dict = {}
    futures_cycle_timestamps: dict[str, str] = {}
    futures_caps: dict[str, float] = {}
    agent_note: str | None = None
    # The executor's risk_guard_decisions list is shared by every candidate in
    # the cycle, so mark where it stands before this candidate can append to
    # it: a row must only ever carry the decisions made FOR this candidate.
    guard_baseline = len(getattr(derivatives, "risk_guard_decisions", None) or [])
    try:
        autoloop_execution.raise_if_mode_disabled(mode, "candidate start", authorization)
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, f"candidate {ticker} start")
        futures_context = None
        futures_account = None
        futures_portfolio = None
        execution_context = ""
        if asset_type == "crypto" and derivatives is not None:
            autoloop_execution.raise_if_mode_disabled(mode, "futures market context", authorization)
            if deadline is not None:
                autoloop_execution.cycle_timeout(deadline, f"candidate {ticker} market context")
            try:
                with telemetry.span("futures_market_context", ticker):
                    futures_context = autoloop_futures_context._closed_futures_context(derivatives, ticker)
            except AutoAuthorizationError as exc:
                raise autoloop_execution.AutoModeDisabled(str(exc)) from exc
            except PermissionError as exc:
                result.reason = f"{type(exc).__name__}: {exc}"
                record_rejection(
                    ticker, market_rejection_reason=result.reason,
                    phase="market", telemetry={"execution_status": "not_started"},
                )
                return result
            except ValueError as exc:
                result.reason = f"market_rejected: {exc}"
                record_rejection(
                    ticker, market_rejection_reason=str(exc), phase="market",
                )
                return result
            if futures_context is None:
                result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
                record_rejection(
                    ticker,
                    confirmation_reason="closed 1h/4h candle pair unavailable",
                    phase="confirmation",
                    telemetry={"requested_ticker": ticker, "unified_symbol": None},
                )
                return result
            with telemetry.span("futures_account", ticker):
                # Size against the margin book the contract settles in
                # (a USDC perpetual draws on USDC, not the USDT totals).
                settle = str(futures_context.get("unified_symbol") or "").rpartition(":")[2] or None
                futures_account = futures_accounts.read_futures_account(derivatives._bridge, settle=settle)
            if not futures_account.positions_scope_complete:
                raise ValueError("futures position scope is unverified; analysis deferred")
            futures_cycle_id, four_hour_open, one_hour_open = futures_cycle_telemetry.cycle_identity(futures_context)
            futures_cycle_timestamps = {"market_context_finished_at": utc_now_iso()}
            futures_caps = futures_cycle_telemetry.effective_caps(
                derivatives, auto_cap=autoloop_futures_execution.MAX_AUTO_LEVERAGE, config=config,
            )
            futures_cycle_payload = {
                "requested_ticker": ticker,
                "unified_symbol": futures_context["unified_symbol"],
                "snapshot": futures_cycle_telemetry.snapshot_payload(futures_context),
                "effective_caps": futures_caps,
                "confirmation_result": None,
                "confirmation_reason": "pending_agent_signal",
                "confirmation": {"result": None, "reason": "pending_agent_signal"},
                "execution_status": "in_progress",
                "phase_timestamps": dict(futures_cycle_timestamps),
            }
            autoloop_execution.raise_if_execution_closed(deadline, cancelled, "futures claim")
            if not futures_cycle_telemetry.claim_cycle(
                futures_cycle_id,
                symbol=futures_context["unified_symbol"],
                four_hour_open=four_hour_open,
                one_hour_open=one_hour_open,
                payload=futures_cycle_payload,
            ):
                result.reason = "duplicate futures signal skipped"
                return result
            if on_cycle_checkpoint is not None:
                on_cycle_checkpoint({
                    "cycle_id": futures_cycle_id,
                    "symbol": futures_context["unified_symbol"],
                    "four_hour_open": four_hour_open,
                    "one_hour_open": one_hour_open,
                    "payload": dict(futures_cycle_payload),
                })
            persist_progress(
                futures_cycle_id, phase="market", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["market_context_finished_at"],
            )
            if on_cycle_checkpoint is not None:
                on_cycle_checkpoint({
                    "cycle_id": futures_cycle_id,
                    "symbol": futures_context["unified_symbol"],
                    "four_hour_open": four_hour_open,
                    "one_hour_open": one_hour_open,
                    "payload": dict(futures_cycle_payload),
                })
            # The open-heat cap reads Position.stop_loss; the venue book does
            # not carry stops, so the persisted entry state supplies them.
            futures_portfolio = build_futures_portfolio(
                futures_account, position_stops=job_store.futures_position_stops(),
            )
            with telemetry.span("protective_bracket", ticker):
                # Both directions: the agents need the engine's reference levels
                # before they decide, and the entry path must reuse these reads
                # rather than hitting the vendor a second time.
                brackets = {
                    "long": autoloop_futures_context._bracket_prices(ticker, direction="long"),
                    "short": autoloop_futures_context._bracket_prices(ticker, direction="short"),
                }
            with telemetry.span("futures_decision_context", ticker):
                execution_context = render_futures_decision_context(
                    futures_context,
                    futures_account,
                    ticker,
                    config,
                    brackets,
                    max_leverage=futures_caps["effective_leverage"],
                )
            futures_cycle_timestamps["decision_context_finished_at"] = utc_now_iso()
            futures_cycle_payload["phase_timestamps"] = dict(futures_cycle_timestamps)
            futures_cycle_payload["futures_account"] = {
                "equity": futures_account.equity,
                "available_balance": futures_account.available_balance,
                "equity_currency": futures_account.equity_currency,
                "available_balance_currency": futures_account.available_balance_currency,
                "position_count": len(futures_account.positions),
            }
            persist_progress(
                futures_cycle_id, phase="context", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["decision_context_finished_at"],
            )
            # Active Kronos context: the pinned model's compact forecast goes
            # into the SAME execution_context string the Trader and the
            # Portfolio Manager read, before either of them decides. The mode,
            # the deadline and the cancellation event are checked on BOTH sides
            # of the inference, because a 20-200 s model call is exactly the
            # window in which an operator can close the mode or the cycle can
            # run out of budget; the inference itself is bounded by the
            # remaining cycle time (the seam turns the deadline into the
            # sampler's chunk deadline). No execution lock is taken or held
            # here: this runs on the candidate's own thread, so bracket
            # monitoring and position protection are untouched.
            autoloop_execution.raise_if_mode_disabled(mode, "kronos forecast", authorization)
            autoloop_execution.raise_if_execution_closed(deadline, cancelled, f"candidate {ticker} kronos forecast")
            futures_cycle_payload["forecast_started"] = True
            futures_cycle_timestamps["forecast_started_at"] = utc_now_iso()
            futures_cycle_payload["phase_timestamps"] = dict(futures_cycle_timestamps)
            if on_cycle_checkpoint is not None:
                on_cycle_checkpoint({
                    "cycle_id": futures_cycle_id,
                    "symbol": futures_context["unified_symbol"],
                    "four_hour_open": four_hour_open,
                    "one_hour_open": one_hour_open,
                    "payload": dict(futures_cycle_payload),
                })
            with telemetry.span("kronos_active_context", ticker):
                kronos_block = autoloop_futures_context._kronos_block_for(
                    derivatives, ticker, futures_context, config,
                    cycle_id=futures_cycle_id or f"{ticker}:{four_hour_open}",
                    deadline=deadline, cancelled=cancelled,
                )
            autoloop_execution.raise_if_mode_disabled(mode, "kronos forecast", authorization)
            autoloop_execution.raise_if_execution_closed(deadline, cancelled, f"candidate {ticker} kronos forecast")
            if kronos_block is None:
                kronos_block = ForecastBlock(
                    symbol=str(futures_context.get("unified_symbol") or ticker),
                    status="unavailable",
                    reason="the Kronos forecast path returned no block",
                )
            kronos_policy = kronos_context.policy_for(kronos_block)
            futures_cycle_payload["kronos_forecast"] = kronos_block.as_json()
            futures_cycle_payload["kronos_policy"] = kronos_policy
            futures_cycle_timestamps["kronos_finished_at"] = utc_now_iso()
            futures_cycle_payload["phase_timestamps"] = dict(futures_cycle_timestamps)
            if on_cycle_checkpoint is not None:
                on_cycle_checkpoint({
                    "cycle_id": futures_cycle_id,
                    "symbol": futures_context["unified_symbol"],
                    "four_hour_open": four_hour_open,
                    "one_hour_open": one_hour_open,
                    "payload": dict(futures_cycle_payload),
                })
            if futures_cycle_id is not None and autoloop_execution.cycle_writes_allowed(deadline, cancelled):
                persist_progress(
                    futures_cycle_id, phase="context", payload=futures_cycle_payload,
                    timestamp=futures_cycle_timestamps["kronos_finished_at"],
                )
            if kronos_policy == "defer":
                # The failure policy is real: defer stops this candidate BEFORE
                # the graph and any LLM call, with the reason persisted on the
                # claimed cycle. This is not a direction veto — only a missing
                # or failed forecast defers here.
                defer_reason = (
                    f"kronos forecast deferred: status={kronos_block.status} "
                    f"reason={kronos_block.reason}" if kronos_block is not None
                    else "kronos forecast deferred: no forecast was produced"
                )
                result.reason = defer_reason
                if futures_cycle_id is not None and autoloop_execution.cycle_writes_allowed(deadline, cancelled):
                    persist_progress(
                        futures_cycle_id,
                        status="rejected",
                        phase="forecast",
                        payload={
                            **futures_cycle_payload,
                            "execution_status": "not_started",
                            "kronos_defer_reason": defer_reason,
                            "executed": False,
                            "phase_timestamps": futures_cycle_timestamps,
                        },
                        terminal=True,
                    )
                return result
            execution_context = kronos_context.append_to_context(execution_context, kronos_block)
        autoloop_execution.raise_if_mode_disabled(mode, "analysis", authorization)
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, "analysis")
        with telemetry.span("graph_build", ticker):
            graph = build_graph(config, _ANALYSTS_CRYPTO if asset_type == "crypto" else _ANALYSTS_STOCK)
        autoloop_execution.raise_if_mode_disabled(mode, "analysis", authorization)
        with telemetry.span("graph_analysis", ticker):
            propagate = getattr(graph, "propagate_with_telemetry", graph.propagate)
            kwargs: dict = {"asset_type": asset_type}
            parameters = inspect.signature(propagate).parameters
            if "telemetry" in parameters:
                kwargs["telemetry"] = telemetry
            if "check_cancelled" in parameters:
                def check_cancelled() -> None:
                    autoloop_execution.raise_if_mode_disabled(mode, "analysis", authorization)
                    autoloop_execution.raise_if_execution_closed(deadline, cancelled, "analysis")

                kwargs["check_cancelled"] = check_cancelled
            if execution_context and _accepts_execution_context(propagate):
                kwargs["execution_context"] = execution_context
            state, signal = propagate(ticker, autoloop_futures_context._today_iso(), **kwargs)
            if deadline is not None:
                autoloop_execution.cycle_timeout(deadline, f"candidate {ticker} analysis")
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, "analysis result")
        result.signal = signal
        autoloop_execution.raise_if_mode_disabled(mode, "planning", authorization)
        pm_decision = state.get("final_trade_decision") if isinstance(state, dict) else None
        agent_note = (str(pm_decision).strip() or None) if pm_decision else None
        if derivatives is not None and asset_type == "crypto":
            decision_data = futures_cycle_telemetry.decision_telemetry(
                state if isinstance(state, dict) else {}, signal, parse_rating,
            )
            confirmation_ok, confirmation_reason = autoloop_futures_context._confirmation_state(
                futures_context, signal,
            )
            decision_data["confirmation_result"] = confirmation_ok
            decision_data["confirmation_reason"] = confirmation_reason
            decision_data["confirmation"] = {
                "result": confirmation_ok,
                "reason": confirmation_reason,
            }
            futures_cycle_payload.update(decision_data)
            # The PM's verbatim decision travels with every later phase write
            # (success, rejection and failure), so a completed entry keeps the
            # rationale the UI renders (futures-cycle-funnel.tsx AgentDecisionNote).
            if agent_note:
                futures_cycle_payload["agent_decision_note"] = futures_cycle_telemetry.redact_payload(
                    agent_note[:6000]
                )
            futures_cycle_payload["signal_reason"] = (
                "tradeable_signal" if signal in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT)
                else "signal_not_tradeable"
            )
            futures_cycle_payload["phase_timestamps"] = {
                **futures_cycle_timestamps,
                "analysis_finished_at": utc_now_iso(),
            }
            futures_cycle_timestamps = dict(futures_cycle_payload["phase_timestamps"])
            persist_progress(
                futures_cycle_id, phase="analysis", payload=futures_cycle_payload,
                timestamp=futures_cycle_timestamps["analysis_finished_at"],
            )
        if asset_type != "crypto":
            result.reason = (
                "advisory only — signal from the stock pipeline; the venue "
                "bridge only executes crypto, so no order plan"
            )
            return result
        autoloop_execution.raise_if_mode_disabled(mode, "planning", authorization)
        if derivatives is not None:
            proposal = (
                state.get("futures_execution_proposal")
                if isinstance(state, dict) else None
            )
            intent = (
                state.get("futures_execution_intent")
                if isinstance(state, dict) else None
            )
            if intent is None:
                intent = "structured_terms_unavailable"
            if intent != "enter":
                result.reason = {
                    "abstain": "agent_abstained",
                    "manage_existing": "position_management_only",
                    "structured_terms_unavailable": "structured_terms_unavailable",
                }.get(str(intent), f"invalid_futures_intent: {intent}")
                record_rejection(
                    ticker,
                    unified_symbol=futures_context.get("unified_symbol"),
                    signal=signal,
                    signal_reason=result.reason,
                    phase="proposal",
                    snapshot=futures_context,
                    agent_note=agent_note,
                    cycle_id=futures_cycle_id,
                    telemetry={
                        **decision_data,
                        "effective_caps": futures_caps,
                        "proposal_validation": {"valid": False, "reason": result.reason},
                        "execution_status": "not_started",
                    },
                    phase_timestamps=futures_cycle_timestamps,
                )
                return result
            with telemetry.span("futures_planning", ticker):
                autoloop_futures_execution._process_futures_candidate(
                    derivatives, ticker, signal, config, result,
                    context=futures_context, proposal=proposal,
                    portfolio=futures_portfolio, brackets=brackets, intent=intent,
                    deadline=deadline, cancelled=cancelled,
                    execution_lock=execution_lock, telemetry=telemetry,
                    agent_note=agent_note,
                    cycle_id=futures_cycle_id,
                    decision_data=decision_data,
                    cycle_payload=futures_cycle_payload,
                    phase_timestamps=futures_cycle_timestamps,
                    guard_baseline=guard_baseline,
                    mode=mode,
                    authorization=authorization,
                )
            return result
        if deadline is not None:
            autoloop_execution.cycle_timeout(deadline, f"candidate {ticker} planning")
        with telemetry.span("portfolio_sync", ticker):
            portfolio = bridge.sync_portfolio()
        autoloop_execution.raise_if_mode_disabled(mode, "planning", authorization)
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, "planning")
        with telemetry.span("planning", ticker):
            plan = bridge.plan_order(ticker, signal, portfolio=portfolio)
        autoloop_execution.raise_if_mode_disabled(mode, "planning", authorization)
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, "planning")
        if plan.side == "buy":
            with telemetry.span("protective_bracket", ticker):
                plan = _attach_bracket(plan, ticker)
        result.plan = plan
        with telemetry.span("execution_policy", ticker):
            execution_daily._handle_plan(
                bridge,
                plan,
                portfolio,
                result,
                execute_guard=lambda execute: autoloop_execution.execute_with_deadline(
                    execute, deadline, cancelled, execution_lock, mode, authorization
                ),
            )
    except (autoloop_execution.AutoModeDisabled, AutoAuthorizationError) as exc:
        if futures_cycle_id is not None and autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            persist_progress(
                futures_cycle_id,
                status="rejected",
                phase="execution" if result.plan is not None else "analysis",
                payload={
                    **futures_cycle_payload,
                    "execution_status": "not_executed",
                    "execution_reason": str(exc),
                    "executed": False,
                    "phase_timestamps": futures_cycle_timestamps,
                },
                terminal=True,
            )
        if isinstance(exc, AutoAuthorizationError):
            raise autoloop_execution.AutoModeDisabled(str(exc)) from exc
        raise
    except TimeoutError:
        raise
    except Exception as exc:  # one candidate's failure never stops the cycle
        result.reason = f"{type(exc).__name__}: {exc}"
        if futures_cycle_id is not None:
            current_phase = (
                "context" if not futures_cycle_timestamps.get("decision_context_finished_at")
                else "analysis" if "decision_data" not in locals()
                else "execution"
            )
            phase_finished = utc_now_iso()
            risk_decisions = autoloop_futures_execution._candidate_guard_decisions(derivatives, guard_baseline)
            payload = {
                **futures_cycle_payload,
                "failure_reason": result.reason,
                "execution_status": "failed" if current_phase == "execution" else "not_started",
                "risk_guard_decisions": risk_decisions,
                "risk_guard": risk_decisions[-1] if risk_decisions else {
                    "allowed": None, "halted": None, "reason": "not_called",
                },
                "order_id": result.order_id,
                "phase_timestamps": {
                    **futures_cycle_timestamps,
                    f"{current_phase}_failed_at": phase_finished,
                },
            }
            if agent_note and "agent_decision_note" not in payload:
                payload["agent_decision_note"] = futures_cycle_telemetry.redact_payload(agent_note[:6000])
            persist_progress(
                futures_cycle_id,
                status="failed",
                phase=current_phase,
                payload=payload,
                terminal=True,
            )
    return result


def _accepts_execution_context(propagate: Callable) -> bool:
    """Whether this graph's propagate() accepts the futures decision context.

    Signature inspection instead of sniffing the TypeError message: an
    unrelated TypeError that merely mentions the kwarg name must not
    silently drop the account/market context the agents decide from.
    """
    try:
        return "execution_context" in inspect.signature(propagate).parameters
    except (TypeError, ValueError):
        return False


def _attach_bracket(plan: Any, ticker: str, direction: str = "long") -> Any:
    """Attach the ATR-derived bracket to an already-sized plan."""
    stop, take = autoloop_futures_context._bracket_prices(ticker, direction)
    if stop is None or take is None:
        return plan
    return plan.model_copy(update={"stop_loss": stop, "take_profit": take})
