"""Validate and execute a Futures entry proposal."""

from __future__ import annotations

import math
import threading
from datetime import UTC, datetime
from typing import Any

from server import (
    autoloop_cycle_records,
    autoloop_execution,
    autoloop_futures_context,
    futures_cycle_telemetry,
)
from server.auto_modes import AutoMode
from server.autoloop_telemetry import CycleTelemetry
from server.contract import utc_now_iso
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.execution import daily as execution_daily
from tradingagents.execution.auto_authorization import (
    AuthorizationSnapshot,
    AutoAuthorizationError,
)
from tradingagents.execution.daily import DailyResult
from tradingagents.execution.futures_signals import FuturesSignalEnvelope

_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER

MAX_AUTO_LEVERAGE = 3.0


def _candidate_guard_decisions(derivatives: Any, baseline: int) -> list[dict]:
    """Only the RiskGuard decisions appended FOR this candidate.

    ``DerivativesExecutor.risk_guard_decisions`` accumulates across the whole
    cycle (derivatives.py builds one executor per cycle), so copying the list
    verbatim would let one candidate's row claim the guard verdicts of its
    predecessors — including an ``allowed=True`` for a candidate the guard
    never even evaluated.
    """
    decisions = getattr(derivatives, "risk_guard_decisions", None) or []
    return list(decisions[baseline:])


def _proposal_terms(
    proposal: Any, *, mark: float, direction: str, config: dict,
    reference_mark: float | None = None,
) -> tuple[dict | None, str | None]:
    """Validate the agents' futures terms; refuse instead of substituting.

    The decision agents own these numbers. The cycle only checks that the
    proposal exists, is internally consistent with the execution price, and
    stays inside the system ceilings -- a proposal outside them is refused,
    never silently clamped, because a clamped size would no longer be the
    decision anyone made.

    ``reference_mark`` is the mark the agents were shown. It is a second
    accepted anchor for ``entry_price``: analysis takes minutes on a moving
    pair, so a live mark can drift past the 1e-3 tolerance from the number the
    agent was told to be consistent with. The bracket and the plan still use
    the live ``mark``.
    """
    if not isinstance(proposal, dict) or not proposal:
        return None, "agent_proposal_missing: no futures entry without agent execution terms"
    margin_mode = str(proposal.get("margin_mode") or "").strip().lower()
    rationale = str(proposal.get("rationale") or "").strip()
    if not rationale:
        return None, "agent_proposal_rejected: rationale is required"
    if margin_mode not in ("isolated", "cross"):
        return None, f"agent_proposal_rejected: margin_mode {margin_mode!r} is not isolated/cross"
    try:
        leverage_raw = proposal["leverage"]
        leverage = float(leverage_raw)
        risk_fraction = float(proposal["risk_fraction"])
        margin_fraction = float(proposal["margin_fraction"])
    except (KeyError, TypeError, ValueError):
        return None, "agent_proposal_rejected: leverage, risk_fraction and margin_fraction are required"
    if not isinstance(leverage_raw, int) or isinstance(leverage_raw, bool):
        return None, "agent_proposal_rejected: leverage must be an integer"
    if not all(math.isfinite(value) for value in (leverage, risk_fraction, margin_fraction)):
        return None, "agent_proposal_rejected: numeric terms must be finite"
    if leverage < 1 or leverage > MAX_AUTO_LEVERAGE:
        return None, (
            f"agent_proposal_rejected: leverage {leverage:g}x is outside "
            f"1-{MAX_AUTO_LEVERAGE:g}x"
        )
    risk_cap = float(config.get("risk_derivatives_max_risk_fraction", 0.02))
    margin_cap = float(config.get("risk_derivatives_max_margin_fraction", 0.5))
    if not 0 < risk_fraction <= risk_cap:
        return None, (
            f"agent_proposal_rejected: risk_fraction {risk_fraction:g} is outside "
            f"0-{risk_cap:g}"
        )
    if not 0 < margin_fraction <= margin_cap:
        return None, (
            f"agent_proposal_rejected: margin_fraction {margin_fraction:g} is outside "
            f"0-{margin_cap:g}"
        )
    entry = proposal.get("entry_price")
    if entry is not None:
        try:
            entry_value = float(entry)
        except (TypeError, ValueError):
            return None, "agent_proposal_rejected: entry_price is not a number"
        if not math.isfinite(entry_value) or entry_value <= 0:
            return None, "agent_proposal_rejected: entry_price must be positive and finite"

        def on_mark(anchor: float | None) -> bool:
            if anchor is None or not math.isfinite(anchor) or anchor <= 0:
                return False
            return abs(entry_value - anchor) <= max(anchor * 0.001, 1e-12)

        if not on_mark(mark) and not on_mark(reference_mark):
            return None, "agent_proposal_rejected: non-market entry_price is unsupported by autoloop"
    stop = proposal.get("stop_loss")
    take = proposal.get("take_profit")
    if (stop is not None) != (take is not None):
        return None, "agent_proposal_rejected: stop_loss and take_profit must be proposed together"
    if stop is not None:
        try:
            stop_value, take_value = float(stop), float(take)
        except (TypeError, ValueError):
            return None, "agent_proposal_rejected: stop_loss/take_profit are not numbers"
        valid = (
            stop_value < mark < take_value if direction == "long"
            else stop_value > mark > take_value
        )
        if not valid:
            return None, (
                f"agent_proposal_rejected: {direction} bracket {stop_value}/{take_value} "
                f"does not protect an entry at {mark}"
            )
    else:
        stop_value = take_value = None
    return {
        "leverage": leverage,
        "risk_fraction": risk_fraction,
        "margin_fraction": margin_fraction,
        "margin_mode": margin_mode,
        "stop_loss": stop_value,
        "take_profit": take_value,
    }, None


def _process_futures_candidate(
    derivatives: Any, ticker: str, signal: str, config: dict, result: DailyResult,
    *, context: dict | None = None, proposal: Any = None, portfolio: Any = None,
    brackets: dict | None = None, intent: str | None = None,
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
    execution_lock: threading.Lock | None = None,
    telemetry: CycleTelemetry | None = None,
    agent_note: str | None = None,
    authorization: AuthorizationSnapshot | None = None,
    cycle_id: str | None = None,
    decision_data: dict | None = None,
    cycle_payload: dict | None = None,
    phase_timestamps: dict | None = None,
    guard_baseline: int | None = None,
    mode: AutoMode | None = None,
) -> None:
    """Run futures candidates through a prevalidated closed candle pair.

    The entry terms come from the Portfolio Manager's FuturesExecutionProposal;
    without one the candidate is rejected and durably recorded. The engine's
    ATR bracket is the fallback only for the protective levels, never for the
    margin mode, leverage, or budgets.
    """
    if not autoloop_execution.execution_allowed(deadline, cancelled):
        result.reason = "cycle closed before futures candidate start"
        return
    telemetry = telemetry or CycleTelemetry()
    now = datetime.now(UTC)

    def persist_progress(cycle: str, /, **kwargs: Any) -> None:
        if autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            futures_cycle_telemetry.persist_progress(cycle, **kwargs)

    def record_rejection(*args: Any, **kwargs: Any) -> None:
        if autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            autoloop_cycle_records._record_futures_cycle_rejection(*args, **kwargs)

    # Guard decisions appended before this function belongs to earlier
    # candidates (or none); planning and execution for THIS candidate append
    # from here on, so the row's risk_guard fields slice from this mark.
    if guard_baseline is None:
        guard_baseline = len(getattr(derivatives, "risk_guard_decisions", None) or [])
    context = context or autoloop_futures_context._closed_futures_context(derivatives, ticker)
    if context is None:
        result.reason = "confirmation_missing: closed 1h/4h candle pair unavailable"
        record_rejection(
            ticker, confirmation_reason="closed 1h/4h candle pair unavailable",
            phase="confirmation", signal=signal, cycle_id=cycle_id,
            telemetry=decision_data, agent_note=agent_note,
        )
        return
    cycle_payload = dict(cycle_payload or {})
    decision_data = dict(decision_data or {})
    phase_timestamps = dict(phase_timestamps or {})
    if not cycle_payload:
        cycle_id, four_hour_open, one_hour_open = futures_cycle_telemetry.cycle_identity(context)
        cycle_payload = {
            "requested_ticker": ticker,
            "unified_symbol": context["unified_symbol"],
            "snapshot": futures_cycle_telemetry.snapshot_payload(context),
            "execution_status": "in_progress",
        }
        if not autoloop_execution.cycle_writes_allowed(deadline, cancelled):
            result.reason = "cycle closed before the candidate could claim its signal"
            return
        if not futures_cycle_telemetry.claim_cycle(
            cycle_id,
            symbol=context["unified_symbol"],
            four_hour_open=four_hour_open,
            one_hour_open=one_hour_open,
            payload=cycle_payload,
        ):
            result.reason = "duplicate futures signal skipped"
            return
    candle_1h = context["one_hour"]
    candle_4h = context["four_hour"]
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
            unified_symbol=context.get("unified_symbol"),
            signal=signal,
            signal_reason=result.reason,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": False, "reason": result.reason},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    direction = autoloop_futures_context._direction(signal)
    confirmation_ok, confirmation_reason = autoloop_futures_context._confirmation_state(context, signal)
    if not confirmation_ok:
        result.reason = "confirmation_missing"
        record_rejection(
            ticker,
            unified_symbol=context.get("unified_symbol"),
            confirmation_reason=confirmation_reason,
            phase="confirmation",
            snapshot=context,
            signal=signal,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": False, "reason": confirmation_reason},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    symbol = context["unified_symbol"]
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        result.reason = f"signal_not_tradeable: {signal}"
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "execution_status": "not_started",
                "proposal_validation": {"valid": True, "reason": "not_required_for_non_tradeable_signal"},
            },
            phase_timestamps=phase_timestamps,
        )
        return
    with telemetry.span("futures_mark_price", ticker):
        mark = autoloop_futures_context._unified_mark_price(derivatives._bridge, symbol)
    if mark is None:
        result.reason = "derivatives entry requires a price"
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data,
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": {"valid": False, "reason": result.reason},
                "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    terms, proposal_error = _proposal_terms(
        proposal, mark=mark, reference_mark=context.get("mark_price"),
        direction=direction, config=config,
    )
    if terms is None:
        result.reason = proposal_error
        record_rejection(
            ticker,
            unified_symbol=symbol,
            signal=signal,
            signal_reason=proposal_error,
            phase="proposal",
            snapshot=context,
            agent_note=agent_note,
            cycle_id=cycle_id,
            telemetry={
                **decision_data,
                "agent_proposal": futures_cycle_telemetry.redact_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": {"valid": False, "reason": proposal_error},
                "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    proposal_validation = {"valid": True, "reason": None}
    stop, take = terms["stop_loss"], terms["take_profit"]
    if stop is None or take is None:
        with telemetry.span("protective_bracket", ticker):
            long_bracket, short_bracket = autoloop_futures_context._direction_brackets(ticker, brackets)
            stop, take = long_bracket if direction == "long" else short_bracket
    if stop is None or take is None:
        result.reason = "derivatives entry requires price and protective bracket"
        proposal_validation = {"valid": False, "reason": result.reason}
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data, "agent_proposal": futures_cycle_telemetry.redact_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": proposal_validation, "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    if not (stop < mark < take if direction == "long" else stop > mark > take):
        result.reason = "invalid protective bracket"
        proposal_validation = {"valid": False, "reason": result.reason}
        record_rejection(
            ticker, unified_symbol=symbol, signal=signal,
            signal_reason=result.reason, phase="proposal", snapshot=context,
            cycle_id=cycle_id, agent_note=agent_note,
            telemetry={
                **decision_data, "agent_proposal": futures_cycle_telemetry.redact_proposal(proposal),
                "effective_caps": cycle_payload.get("effective_caps", {}),
                "proposal_validation": proposal_validation, "execution_status": "not_started",
            },
            phase_timestamps=phase_timestamps,
        )
        return
    expires_at = now + (
        datetime.fromtimestamp(candle_4h.close_time / 1000, UTC)
        - datetime.fromtimestamp(candle_4h.open_time / 1000, UTC)
    )
    cycle_payload.update({
        **decision_data,
        "requested_ticker": ticker,
        "unified_symbol": symbol,
        "signal": signal,
        "signal_reason": "tradeable_signal",
        "confirmation_result": confirmation_ok,
        "confirmation_reason": confirmation_reason,
        "snapshot": futures_cycle_telemetry.snapshot_payload(context),
        "agent_proposal": futures_cycle_telemetry.redact_proposal(proposal),
        "proposal_validation": proposal_validation,
        "mark_price": mark,
        # HEAD carried the PM's verbatim decision onto the terminal success row;
        # staging it here keeps it on the proposal, planning, execution AND
        # failure writes that all build from cycle_payload.
        "agent_decision_note": (
            futures_cycle_telemetry.redact_payload(agent_note[:6000]) if agent_note else None
        ),
        "plan": {
            "stop_loss": stop,
            "take_profit": take,
            "leverage": terms["leverage"],
            "risk_fraction": terms["risk_fraction"],
            "margin_fraction": terms["margin_fraction"],
            "margin_mode": terms["margin_mode"],
        },
    })
    phase_timestamps["proposal_validated_at"] = utc_now_iso()
    persist_progress(
        cycle_id, phase="proposal", payload={**cycle_payload, "phase_timestamps": phase_timestamps},
        timestamp=phase_timestamps["proposal_validated_at"],
    )
    FuturesSignalEnvelope.create(
        symbol=symbol,
        candle_open=datetime.fromtimestamp(candle_1h.open_time / 1000, UTC),
        candle_close=datetime.fromtimestamp(candle_1h.close_time / 1000, UTC),
        direction=direction, entry=mark, stop=stop, targets=(take,),
        created_at=now, expires_at=expires_at,
        confirmation_candle_open=datetime.fromtimestamp(candle_4h.open_time / 1000, UTC),
        strategy="autoloop", strategy_version="1",
    )
    try:
        autoloop_execution.raise_if_mode_disabled(mode, "futures planning", authorization)
        if deadline is not None:
            autoloop_execution.cycle_timeout(deadline, f"candidate {ticker} futures planning")
        phase_timestamps["planning_started_at"] = utc_now_iso()
        persist_progress(
            cycle_id, phase="planning",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps},
            timestamp=phase_timestamps["planning_started_at"],
        )
        with telemetry.span("portfolio_sync", ticker):
            book = portfolio if portfolio is not None else derivatives._bridge.sync_portfolio()
        autoloop_execution.raise_if_mode_disabled(mode, "futures planning", authorization)
        autoloop_execution.raise_if_execution_closed(deadline, cancelled, "futures planning")
        with telemetry.span("planning", ticker):
            plan = derivatives.plan_order(
                ticker, signal, portfolio=book, price=mark,
                leverage=terms["leverage"], stop_loss=stop, take_profit=take,
                risk_fraction=terms["risk_fraction"],
                margin_fraction=terms["margin_fraction"],
                margin_mode=terms["margin_mode"],
                futures_execution_intent=intent,
                # Soft-cap override evidence: measured mechanically by the
                # risk guard from the closed candles and funding read above.
                confirmation_ok=confirmation_ok,
                funding_rate=context.get("funding_rate"),
            )
        result.plan = plan
        phase_timestamps["planning_finished_at"] = utc_now_iso()
        cycle_payload["plan"] = {
            **cycle_payload.get("plan", {}),
            "side": getattr(plan, "side", None),
            "quantity": getattr(plan, "quantity", None),
            "estimated_price": getattr(plan, "estimated_price", None),
            "cost": getattr(plan, "cost", None),
            "leverage": getattr(plan, "leverage", terms["leverage"]),
            "margin_mode": getattr(plan, "margin_mode", terms["margin_mode"]),
            "stop_loss": getattr(plan, "stop_loss", stop),
            "take_profit": getattr(plan, "take_profit", take),
            "reason": getattr(plan, "reason", None),
        }
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        cycle_payload["risk_guard"] = risk_decisions[-1] if risk_decisions else {
            "allowed": None, "halted": None, "reason": "not_called",
        }
        persist_progress(
            cycle_id, phase="planning",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps},
            timestamp=phase_timestamps["planning_finished_at"],
        )
        autoloop_execution.raise_if_mode_disabled(mode, "futures execution", authorization)
        if deadline is not None:
            autoloop_execution.cycle_timeout(deadline, f"candidate {ticker} execution")
        phase_timestamps["execution_started_at"] = utc_now_iso()
        persist_progress(
            cycle_id, phase="execution",
            payload={**cycle_payload, "phase_timestamps": phase_timestamps,
                     "execution_status": "in_progress"},
            timestamp=phase_timestamps["execution_started_at"],
        )
        with telemetry.span("execution_policy", ticker):
            execution_daily._handle_plan(
                derivatives,
                plan,
                book,
                result,
                execute_guard=lambda execute: autoloop_execution.execute_with_deadline(
                    execute, deadline, cancelled, execution_lock, mode, authorization
                ),
            )
        # Terminal outcome vocabulary (audit truth): only a venue-confirmed
        # order is "executed"; a refused/pending/no-order plan keeps its own
        # execution_status so the funnel never counts it as an entry.
        # risk_rejected keys off the guard's actual decision, never a reason
        # substring; awaiting_approval keys off the executor's own literal
        # "awaiting approval" reasons (daily.py), a vocabulary no guard
        # refusal shares, so an entry can never be counted from it either.
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        last_decision = risk_decisions[-1] if risk_decisions else None
        if result.executed:
            execution_status = "executed"
        elif result.reason and "approval" in result.reason:
            execution_status = "awaiting_approval"
        elif (
            plan.side is None
            and last_decision is not None
            and last_decision.get("allowed") is False
        ):
            execution_status = "risk_rejected"
        else:
            execution_status = "not_executed"
        protection_state = "configured" if plan.stop_loss is not None and plan.take_profit is not None else "missing"
        phase_timestamps["execution_finished_at"] = utc_now_iso()
        cycle_payload.update({
            "execution_status": execution_status,
            "executed": result.executed,
            "order_id": result.order_id,
            "plan_reason": plan.reason,
            "protection_state": protection_state,
            "risk_guard_decisions": risk_decisions,
            "risk_guard": last_decision or {"allowed": None, "halted": None, "reason": "not_called"},
            "phase_timestamps": phase_timestamps,
        })
        if result.reason:
            cycle_payload["execution_reason"] = result.reason
        terminal_phase = "entry" if execution_status == "executed" else "execution"
        terminal_status = "completed" if execution_status == "executed" else "rejected"
        persist_progress(
            cycle_id, status=terminal_status, phase=terminal_phase,
            payload=futures_cycle_telemetry.redact_payload(cycle_payload),
            terminal=True,
        )
    except (autoloop_execution.AutoModeDisabled, AutoAuthorizationError):
        raise
    except Exception as exc:
        # This terminal write must carry the complete audit trail: the row is
        # locked once it lands terminal (job_store._FUTURES_TERMINAL_STATUSES),
        # so the outer handler in _process_candidate cannot add risk_guard
        # afterwards.
        risk_decisions = _candidate_guard_decisions(derivatives, guard_baseline)
        # A guard refusal (plan-stage halt or execute-stage refusal) is a
        # deliberate "no", not a malfunction: it keeps its own terminal shape
        # (rejected / risk_rejected) so the funnel never mixes refusals into
        # execution failures.
        if isinstance(exc, PermissionError) and str(exc).startswith("risk guard"):
            persist_progress(
                cycle_id,
                status="rejected",
                phase="execution" if phase_timestamps.get("execution_started_at") else "planning",
                payload={
                    **cycle_payload,
                    "signal": signal,
                    "signal_reason": "tradeable_signal",
                    "plan_reason": getattr(result.plan, "reason", None),
                    "execution_status": "risk_rejected",
                    "executed": False,
                    "order_id": result.order_id,
                    "risk_guard_decisions": risk_decisions,
                    "risk_guard": risk_decisions[-1] if risk_decisions else {
                        "allowed": None, "halted": None, "reason": "not_called",
                    },
                    "execution_reason": str(exc),
                    "phase_timestamps": phase_timestamps,
                },
                terminal=True,
            )
            raise
        persist_progress(
            cycle_id,
            status="failed",
            phase="execution",
            payload={
                **cycle_payload,
                "signal": signal,
                "signal_reason": "tradeable_signal",
                "plan_reason": getattr(result.plan, "reason", None),
                "execution_status": "failed",
                "executed": bool(result.executed),
                "order_id": result.order_id,
                "risk_guard_decisions": risk_decisions,
                "risk_guard": risk_decisions[-1] if risk_decisions else {
                    "allowed": None, "halted": None, "reason": "not_called",
                },
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "phase_timestamps": {
                    **phase_timestamps,
                    "execution_failed_at": utc_now_iso(),
                },
            },
            terminal=True,
        )
        raise
