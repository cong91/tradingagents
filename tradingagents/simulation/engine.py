"""Event-driven futures simulation that reuses the runtime's own decisions.

The engine walks a strictly increasing sequence of CLOSED candles. At each
decision candle close it takes a proposal, sizes it through the runtime's
futures sizing chain (``validate_bracket`` + ``risk_limited_quantity`` plus
the executor's cap comparisons — the exact math ``DerivativesExecutor._size``
runs), asks the real ``RiskGuard.check`` for the verdict, and fills the entry
at the next candle's open through the cost model. While a position is open,
the real ``evaluate_profit_protection`` drives every stop/target phase and
the real ``plan_scale_out`` drives the tranche exits; the time-stop verdict is
the function's own, never re-derived.

Offline by construction: no exchange client, no venue URL, no network path.
The module imports only pure execution modules. ``sizing.compute`` (the spot
mapping) is deliberately NOT used: its shorts require a held position, which
would fork the futures semantics — the futures parity target is the
``DerivativesExecutor._size`` chain, and ``tests/test_futures_simulation.py``
pins this engine's plan against that method's output for identical inputs.

Everything produced here is labeled a simulation and carries the disclaimer:
this is not evidence of live profitability and not financial advice.
"""

from __future__ import annotations

import math
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tradingagents.agents.rating import RATINGS_5_TIER, is_review
from tradingagents.dataflows.market_data import Candle
from tradingagents.execution.audit import append_event
from tradingagents.execution.futures_safety import (
    FuturesPlanError,
    risk_limited_quantity,
    validate_bracket,
)
from tradingagents.execution.profit_protection import evaluate_profit_protection
from tradingagents.execution.risk import RiskDecision, RiskGuard, RiskLimits
from tradingagents.execution.scale_out import ScaleOutPlan, plan_scale_out
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.portfolio import PortfolioContext, Position
from tradingagents.simulation.costs import CostLedger, CostModel, FundingSeries
from tradingagents.simulation.data import candle_from_payload
from tradingagents.simulation.fills import (
    AMBIGUITY_POLICIES,
    MarketRules,
    market_entry_fill,
    match_exit,
    match_scale_out,
)

SIMULATION_LABEL = "simulation"
DISCLAIMER = (
    "Simulation output on replayed closed candles with modeled costs, funding "
    "and fills; it is not evidence of live profitability and not financial advice."
)

ProposalSource = Callable[[Candle], "Proposal | None"]


@dataclass(frozen=True)
class Proposal:
    """One simulated entry proposal with the runtime's plan-time terms."""

    signal: str
    ticker: str
    ccxt_symbol: str
    stop_loss: float
    take_profit: float
    leverage: float = 1.0
    risk_fraction: float = 0.01
    margin_fraction: float = 0.25
    margin_mode: Literal["isolated", "cross"] | None = None
    scale_out_enabled: bool = True
    scale_out_first_r: float = 1.0
    scale_out_first_fraction: float = 0.33
    scale_out_second_r: float = 2.0
    scale_out_second_fraction: float = 0.33


@dataclass(frozen=True)
class SizingCaps:
    """The plan-time hard caps ``DerivativesExecutor._size`` compares against."""

    max_risk_fraction: float = 0.02
    max_margin_fraction: float = 0.5
    min_order_cost: float = 10.0


def futures_plan(
    *,
    proposal: Proposal,
    portfolio: PortfolioContext | None,
    price: float | None,
    caps: SizingCaps | None = None,
) -> PlannedOrder:
    """Size a derivatives plan exactly like the runtime's futures chain.

    The refusal vocabulary, the cap comparisons and the risk-limited quantity
    mirror ``DerivativesExecutor._size`` (which delegates to the pure
    ``futures_safety`` functions); the parity test pins this function against
    that method for identical inputs, so a drift breaks a test, not reality.
    """
    limits = caps if caps is not None else SizingCaps()
    margin_mode = proposal.margin_mode if proposal.margin_mode in ("isolated", "cross") else None

    def no_order(reason: str, *, needs_review: bool = False) -> PlannedOrder:
        return PlannedOrder(
            ticker=proposal.ticker, ccxt_symbol=proposal.ccxt_symbol,
            signal=proposal.signal, market="derivatives",
            leverage=proposal.leverage, margin_mode=margin_mode,
            futures_execution_intent="enter",
            needs_review=needs_review, reason=reason,
        )

    if is_review(proposal.signal):
        return no_order("REVIEW signal: no order without human review", needs_review=True)
    if proposal.signal not in RATINGS_5_TIER:
        return no_order(
            f"unrecognized signal {proposal.signal!r}: no order without human review",
            needs_review=True,
        )
    if proposal.signal == "Hold":
        return no_order("Hold: no trade")
    if price is None or price <= 0:
        return no_order("price unavailable")
    book = portfolio if portfolio is not None else PortfolioContext()
    if book.cash is None:
        return no_order("cash unavailable")
    if proposal.margin_mode is not None and proposal.margin_mode not in ("isolated", "cross"):
        return no_order(
            f"unrecognized margin_mode {proposal.margin_mode!r}: expected 'isolated' or 'cross'"
        )
    signal = proposal.signal
    long = signal in ("Buy", "Overweight")
    side = "buy" if long else "sell"
    if proposal.stop_loss is None or proposal.take_profit is None:
        return no_order("derivatives entry requires explicit stop_loss and take_profit")
    if proposal.risk_fraction > limits.max_risk_fraction:
        return no_order(
            f"proposed risk_fraction {proposal.risk_fraction:g} exceeds the hard "
            f"cap {limits.max_risk_fraction:g}"
        )
    if proposal.margin_fraction > limits.max_margin_fraction:
        return no_order(
            f"proposed margin_fraction {proposal.margin_fraction:g} exceeds the hard "
            f"cap {limits.max_margin_fraction:g}"
        )
    try:
        validate_bracket(
            side=side, entry=price, stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
        )
        equity = float(book.cash)
        quantity = risk_limited_quantity(
            side=side, equity=equity, entry=price, stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit, leverage=proposal.leverage,
            risk_fraction=proposal.risk_fraction,
            margin_fraction=proposal.margin_fraction,
        )
    except (FuturesPlanError, TypeError, ValueError) as exc:
        return no_order(f"derivatives sizing refused: {exc}")
    cost = quantity * price / proposal.leverage
    if cost * proposal.leverage < limits.min_order_cost:
        return no_order("cost below exchange minimum")
    return PlannedOrder(
        ticker=proposal.ticker, ccxt_symbol=proposal.ccxt_symbol, signal=signal,
        side=side, quantity=quantity, estimated_price=price, cost=cost,
        market="derivatives", leverage=proposal.leverage, margin_mode=margin_mode,
        futures_execution_intent="enter",
        stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
    )


@dataclass
class _Position:
    """One open simulated futures position."""

    plan: PlannedOrder
    side: str
    entry: float
    quantity: float
    initial_stop: float
    current_stop: float
    current_target: float | None
    opened_at_ms: int
    risk_per_unit: float
    round_trip_fee_r: float
    scale_out: ScaleOutPlan | None = None
    fired_tranches: int = 0
    remaining: float = 0.0
    funding_received: float = 0.0


@dataclass
class SimulationResult:
    """The report of one offline run: capital path, costs, parity, honesty."""

    label: str = SIMULATION_LABEL
    disclaimer: str = DISCLAIMER
    candles: int = 0
    ambiguity_policy: str = "worst-first"
    ambiguity_count: int = 0
    equity_curve: list[dict[str, float]] = field(default_factory=list)
    max_drawdown: float = 0.0
    net_after_costs: dict[str, Any] = field(default_factory=dict)
    exit_reason_distribution: dict[str, int] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    parity_log: list[dict[str, Any]] = field(default_factory=list)
    costs: dict[str, Any] = field(default_factory=dict)
    sensitivity: list[dict[str, Any]] = field(default_factory=list)
    funding_known: bool = False

    def report(self) -> dict[str, Any]:
        """The JSON-safe report; every number here is a simulated number."""
        return {
            "kind": self.label,
            "disclaimer": self.disclaimer,
            "candles": self.candles,
            "ambiguity_policy": self.ambiguity_policy,
            "ambiguity_count": self.ambiguity_count,
            "equity_curve": self.equity_curve,
            "max_drawdown": self.max_drawdown,
            "net_after_costs": self.net_after_costs,
            "exit_reason_distribution": dict(sorted(self.exit_reason_distribution.items())),
            "fills": self.fills,
            "decisions": self.decisions,
            "parity_log": self.parity_log,
            "costs": self.costs,
            "cost_sensitivity": self.sensitivity,
            "funding_known": self.funding_known,
        }


@dataclass
class _RunState:
    """Mutable simulation state for one run."""

    result: SimulationResult
    ledger: CostLedger
    guard: RiskGuard
    audit_path: Path
    realized_total: float = 0.0
    position: _Position | None = None
    pending_exit_kind: str | None = None


def _book_position(position: _Position, *, mark: float) -> Position:
    """The PortfolioContext view of one open simulated position."""
    return Position(
        ticker=position.plan.ticker,
        quantity=position.quantity,
        market="derivatives",
        market_symbol=position.plan.ccxt_symbol,
        notional=position.quantity * mark,
        notional_currency="USDT",
        stop_loss=position.current_stop,
    )


def _audit_fill(
    path: Path, position: _Position, *, action: str, quantity: float, price: float,
    open_time_ms: int,
) -> None:
    """One simulated fill in the runtime audit shape, at simulated time.

    The guard's FIFO replay reads ``qty``/``price_est`` from confirmed
    buy/sell lines to reconstruct closed trades, so simulated fills enter the
    streak and daily-loss caps exactly like runtime fills would.
    """
    when = datetime.fromtimestamp(open_time_ms / 1000, tz=UTC)
    append_event(str(path), {
        "timestamp": when.isoformat(),
        "ticker": position.plan.ticker,
        "ccxt_symbol": position.plan.ccxt_symbol,
        "signal": position.plan.signal,
        "action": action,
        "qty": quantity,
        "price_est": price,
        "mode": "simulation",
        "confirmed": True,
        "order_id": None,
        "phase": "execute",
        "venue_key": "simulation",
        "market": "derivatives",
        "leverage": position.plan.leverage,
    })


def _realized(side: str, entry: float, exit_price: float, quantity: float) -> float:
    """Price PnL of one simulated exit (fees and funding are separate)."""
    if side == "long":
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity


def _unrealized(position: _Position, mark: float) -> float:
    return _realized(position.side, position.entry, mark, position.remaining)


def _max_drawdown(curve: list[dict[str, float]]) -> float:
    """Deepest peak-to-trough drop of the equity path, as a positive fraction."""
    peak = -math.inf
    drawdown = 0.0
    for point in curve:
        value = point["equity"]
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def _record_exit(
    state: _RunState, position: _Position, *, kind: str, price: float,
    quantity: float, open_time_ms: int, maker: bool, model: CostModel,
    ambiguous: bool = False,
) -> None:
    """Book one simulated exit: PnL, fee, fill record, audit line, size cut."""
    realized = _realized(position.side, position.entry, price, quantity)
    fee = model.fee(quantity * price, maker=maker)
    state.ledger.charge_fee(fee)
    state.realized_total += realized
    state.result.fills.append({
        "kind": kind, "side": position.side, "price": price,
        "quantity": quantity, "candle_open_time": open_time_ms,
        "fee": fee, "realized": realized, "ambiguous": ambiguous,
    })
    state.result.exit_reason_distribution[kind] = (
        state.result.exit_reason_distribution.get(kind, 0) + 1
    )
    _audit_fill(
        state.audit_path, position,
        action="sell" if position.side == "long" else "buy",
        quantity=quantity, price=price, open_time_ms=open_time_ms,
    )
    position.remaining -= quantity
    if position.remaining <= 1e-12:
        position.remaining = 0.0
        state.position = None


def _try_pending_exit(
    state: _RunState, candle: Candle, *, model: CostModel, rules: MarketRules,
) -> None:
    """Fill a time-stop decided on the previous candle, at this candle's open."""
    position = state.position
    if position is None or state.pending_exit_kind is None:
        return
    kind = state.pending_exit_kind
    fill = market_entry_fill(candle, position.side, position.remaining, rules)
    if fill is None:
        return  # rules refused the size this candle; stays pending
    exit_price = model.exit_price(position.side, candle.open)
    state.pending_exit_kind = None
    _record_exit(
        state, position, kind=kind, price=exit_price,
        quantity=fill.quantity, open_time_ms=candle.open_time,
        maker=False, model=model,
    )


def _maybe_open(
    state: _RunState,
    decision_candle: Candle,
    next_candle: Candle | None,
    proposal: Proposal,
    *,
    model: CostModel,
    rules: MarketRules,
    caps: SizingCaps | None,
    equity: float,
) -> None:
    """Plan, risk-check and fill one entry, exactly as the runtime would."""
    book = PortfolioContext(
        cash=equity, currency="USDT",
        positions=[_book_position(state.position, mark=decision_candle.close)]
        if state.position is not None else [],
    )
    plan = futures_plan(
        proposal=proposal, portfolio=book, price=decision_candle.close, caps=caps,
    )
    state.result.parity_log.append({
        "call": "futures_plan",
        "input": {
            "signal": proposal.signal, "price": decision_candle.close,
            "equity": equity, "stop_loss": proposal.stop_loss,
            "take_profit": proposal.take_profit,
            "leverage": proposal.leverage,
            "risk_fraction": proposal.risk_fraction,
            "margin_fraction": proposal.margin_fraction,
        },
        "output": plan.model_dump(mode="json"),
    })
    if plan.side is None:
        state.result.decisions.append({
            "candle_open_time": decision_candle.open_time, "decision": "no_order",
            "reason": plan.reason, "needs_review": plan.needs_review,
        })
        return
    decision: RiskDecision = state.guard.check(plan, book, state.audit_path)
    state.result.parity_log.append({
        "call": "RiskGuard.check",
        "input": {"plan": plan.model_dump(mode="json"), "portfolio_cash": equity},
        "output": decision.model_dump(mode="json"),
    })
    if not decision.allowed:
        state.result.decisions.append({
            "candle_open_time": decision_candle.open_time, "decision": "risk_refused",
            "reason": decision.reason, "halted": decision.halted,
        })
        return
    if next_candle is None:
        state.result.decisions.append({
            "candle_open_time": decision_candle.open_time, "decision": "no_next_candle",
            "reason": "entry decided but no following closed candle exists",
        })
        return
    side = "long" if plan.side == "buy" else "short"
    fill = market_entry_fill(next_candle, side, float(plan.quantity), rules)
    if fill is None:
        state.result.decisions.append({
            "candle_open_time": next_candle.open_time, "decision": "entry_not_matched",
            "reason": "lot/notional rules refused the simulated size",
        })
        return
    entry_price = model.entry_price(plan.side, next_candle.open)
    # A gap between the decision candle and the fill candle can invalidate the
    # planned bracket (a long filling at/below its stop). The plan is
    # abandoned — the same validate_bracket semantics the runtime enforces —
    # instead of opening a position whose stop does not protect it.
    bracket_valid = (
        entry_price > plan.stop_loss if side == "long" else entry_price < plan.stop_loss
    )
    if plan.stop_loss is not None and not bracket_valid:
        state.result.decisions.append({
            "candle_open_time": next_candle.open_time,
            "decision": "entry_bracket_invalidated_by_gap",
            "reason": "entry fill price does not protect against the planned stop; nothing opened",
        })
        return
    fee = model.fee(fill.quantity * entry_price)
    state.ledger.charge_fee(fee)
    state.result.fills.append({
        "kind": "entry", "side": side, "price": entry_price,
        "quantity": fill.quantity,
        "candle_open_time": next_candle.open_time,
        "fee": fee, "realized": None,
    })
    entry = _Position(
        plan=plan, side=side, entry=entry_price, quantity=fill.quantity,
        initial_stop=plan.stop_loss or 0.0, current_stop=plan.stop_loss or 0.0,
        current_target=plan.take_profit, opened_at_ms=next_candle.open_time,
        risk_per_unit=abs(entry_price - plan.stop_loss) if plan.stop_loss else 0.0,
        round_trip_fee_r=0.0, remaining=fill.quantity,
    )
    if entry.risk_per_unit > 0:
        entry.round_trip_fee_r = (
            2 * model.taker_rate * entry_price * fill.quantity
        ) / (entry.risk_per_unit * fill.quantity)
    if proposal.scale_out_enabled and plan.stop_loss:
        entry.scale_out = plan_scale_out(
            side=side, entry=entry_price, initial_stop=plan.stop_loss,
            contracts=fill.quantity,
            first_r=proposal.scale_out_first_r,
            first_fraction=proposal.scale_out_first_fraction,
            second_r=proposal.scale_out_second_r,
            second_fraction=proposal.scale_out_second_fraction,
        )
        state.result.parity_log.append({
            "call": "plan_scale_out",
            "input": {"side": side, "entry": entry_price,
                      "initial_stop": plan.stop_loss, "contracts": fill.quantity},
            "output": [level.price for level in entry.scale_out.levels],
        })
    state.position = entry
    _audit_fill(
        state.audit_path, entry,
        action="buy" if side == "long" else "sell",
        quantity=fill.quantity, price=entry_price,
        open_time_ms=next_candle.open_time,
    )
    state.result.decisions.append({
        "candle_open_time": decision_candle.open_time, "decision": "entry_planned",
        "fill_candle_open_time": next_candle.open_time,
        "quantity": fill.quantity, "entry_price": entry_price,
    })


def _manage_position(
    state: _RunState, candle: Candle, previous_window: tuple[int, int] | None,
    *, model: CostModel, rules: MarketRules, funding: FundingSeries | None,
    max_hold_hours: float | None, ambiguity_policy: str,
) -> None:
    """One candle of exit matching, scale-out and protection for the position.

    The order matters and mirrors the venue: exits match against the levels
    that were RESTING during this candle (set at the previous candle's close),
    and only afterwards does the close update the stop/target for future
    candles. Matching against a stop the candle itself just moved would be
    look-ahead within the candle.
    """
    position = state.position
    if position is None:
        return
    if funding is not None and previous_window is not None:
        start_ms = max(previous_window[0], position.opened_at_ms)
        end_ms = previous_window[1]
        if start_ms < end_ms:
            state.ledger.apply_funding(funding, start_ms, end_ms)
            window_funding = funding.received_between(start_ms, end_ms)
            if window_funding is not None:
                position.funding_received += window_funding
    # 1. Match exits against the levels resting during this candle.
    # Runtime parity: with scale-out enabled the take-profit order is
    # REPLACED by the tranche legs plus a trailed runner (scale_out_apply),
    # so the target level must never close the whole remaining position —
    # only the stop (and the trailed stop) exits it.
    exit_target = None if position.scale_out is not None else position.current_target
    level = match_exit(
        candle, position.side, stop=position.current_stop, target=exit_target,
        quantity=position.remaining, rules=rules,
        ambiguity_policy=ambiguity_policy,
    )
    if level.ambiguous:
        state.result.ambiguity_count += 1
    if level.fill is not None:
        exit_fill = level.fill
        # Resting levels (targets) fill at their price; a stop is a market
        # fill and slips against the trader through the cost model.
        exit_price = (
            exit_fill.price if exit_fill.maker
            else model.exit_price(position.side, exit_fill.price)
        )
        _record_exit(
            state, position, kind=exit_fill.kind, price=exit_price,
            quantity=exit_fill.quantity, open_time_ms=candle.open_time,
            maker=exit_fill.maker, model=model, ambiguous=exit_fill.ambiguous,
        )
    elif position.scale_out is not None:
        # Scale-out tranches only fire when the candle did not stop out.
        for index in range(position.fired_tranches, len(position.scale_out.levels)):
            tranche = position.scale_out.levels[index]
            scale_fill = match_scale_out(
                candle, tranche.price, position.side,
                min(tranche.quantity, position.remaining),
            )
            if scale_fill is None:
                break
            _record_exit(
                state, position, kind="scale_out", price=tranche.price,
                quantity=scale_fill.quantity, open_time_ms=candle.open_time,
                maker=True, model=model,
            )
            position.fired_tranches += 1
            if state.position is None:
                break
    if state.position is None:
        return  # fully exited during this candle; nothing left to protect
    # 2. Protection at the close updates the levels for FUTURE candles.
    held_hours = (candle.close_time - position.opened_at_ms) / 3_600_000
    # The runtime's funding conversion divides by the size the bracket
    # OPENED with (futures_protection._time_stop_funding_inputs), never the
    # shrunken remainder — the funding accrued on the original size.
    risk_amount = position.risk_per_unit * position.quantity
    cumulative_funding_r = (
        position.funding_received / risk_amount if risk_amount > 0 else None
    )
    protection = evaluate_profit_protection(
        side=position.side,
        entry=position.entry,
        initial_stop=position.initial_stop,
        current_stop=position.current_stop,
        current_target=position.current_target,
        mark=candle.close,
        max_hold_hours=max_hold_hours,
        held_hours=held_hours,
        cumulative_funding_r=cumulative_funding_r,
        round_trip_fee_r=position.round_trip_fee_r,
    )
    state.result.parity_log.append({
        "call": "evaluate_profit_protection",
        "input": {
            "side": position.side, "entry": position.entry,
            "initial_stop": position.initial_stop,
            "current_stop": position.current_stop,
            "current_target": position.current_target,
            "mark": candle.close, "held_hours": held_hours,
            "cumulative_funding_r": cumulative_funding_r,
            "round_trip_fee_r": position.round_trip_fee_r,
        },
        "output": {
            "stop": protection.stop, "target": protection.target,
            "phase": protection.phase, "exit_reason": protection.exit_reason,
        },
    })
    position.current_stop = protection.stop
    position.current_target = protection.target
    if protection.exit_reason == "time_stop":
        state.pending_exit_kind = "time_stop"
        state.result.decisions.append({
            "candle_open_time": candle.open_time, "decision": "time_stop_pending",
        })


def run_simulation(
    candles: list[Candle],
    *,
    proposal_source: ProposalSource,
    risk_limits: RiskLimits | None = None,
    costs: CostModel | None = None,
    market_rules: MarketRules | None = None,
    caps: SizingCaps | None = None,
    funding: FundingSeries | None = None,
    ambiguity_policy: str = "worst-first",
    max_hold_hours: float | None = None,
    initial_cash: float = 10000.0,
    quote_currency: str = "USDT",
    audit_log_path: Path | None = None,
    include_sensitivity: bool = True,
) -> SimulationResult:
    """Run one offline futures simulation over closed candles.

    Every decision runs the real runtime function and is logged into
    ``parity_log`` with its inputs, so the parity tests can re-derive each
    verdict by calling the same function directly. ``ambiguity_policy`` must
    be one of the fills module's policies; ``worst-first`` (the default)
    assumes the stop touched first when one candle spans both levels and
    counts every such candle in ``ambiguity_count``. A run without a funding
    series reports funding as unknown, never as zero.
    """
    if ambiguity_policy not in AMBIGUITY_POLICIES:
        raise ValueError(f"unknown ambiguity policy: {ambiguity_policy!r}")
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    if not ordered:
        raise ValueError("simulation needs at least one closed candle")
    for candle in ordered:
        if not candle.is_closed:
            raise ValueError("simulation only consumes closed candles")
    model = costs if costs is not None else CostModel()
    rules = market_rules if market_rules is not None else MarketRules()
    limits = risk_limits if risk_limits is not None else RiskLimits()
    result = SimulationResult(candles=len(ordered), ambiguity_policy=ambiguity_policy)
    result.costs = {
        "taker_rate": model.taker_rate,
        "maker_rate": model.maker_rate,
        "slippage_rate": model.slippage_rate,
    }
    ledger = CostLedger(currency=quote_currency)
    guard = RiskGuard(limits=limits, price_of=lambda ticker: None)
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = (
            audit_log_path if audit_log_path is not None
            else Path(tmp) / "risk_audit.jsonl"
        )
        state = _RunState(result=result, ledger=ledger, guard=guard, audit_path=audit_path)
        for index, candle in enumerate(ordered):
            previous_window = (
                (ordered[index - 1].open_time, candle.open_time) if index > 0 else None
            )
            _try_pending_exit(state, candle, model=model, rules=rules)
            if state.position is None:
                proposal = proposal_source(candle)
                if proposal is not None:
                    equity = initial_cash + state.realized_total - ledger.fee_paid
                    _maybe_open(
                        state, candle,
                        ordered[index + 1] if index + 1 < len(ordered) else None,
                        proposal,
                        model=model, rules=rules, caps=caps, equity=equity,
                    )
            else:
                _manage_position(
                    state, candle, previous_window, model=model, rules=rules,
                    funding=funding, max_hold_hours=max_hold_hours,
                    ambiguity_policy=ambiguity_policy,
                )
            equity_now = (
                initial_cash + state.realized_total - ledger.fee_paid
                + ledger.funding_received
            )
            unrealized = (
                _unrealized(state.position, candle.close)
                if state.position is not None else 0.0
            )
            result.equity_curve.append({
                "candle_open_time": candle.open_time, "equity": equity_now + unrealized,
            })

        # Realize any position still open at the end at the last close, so the
        # net figure never reports an open position as flat equity.
        if state.position is not None:
            exit_price = model.exit_price(state.position.side, ordered[-1].close)
            _record_exit(
                state, state.position, kind="final_close", price=exit_price,
                quantity=state.position.remaining,
                open_time_ms=ordered[-1].open_time, maker=False, model=model,
            )

    final_equity = (
        initial_cash + state.realized_total - ledger.fee_paid + ledger.funding_received
    )
    result.max_drawdown = _max_drawdown(result.equity_curve)
    result.funding_known = ledger.funding_available and ledger.funding_unknown_windows == 0
    result.net_after_costs = {
        "currency": quote_currency,
        "initial_cash": initial_cash,
        "final_equity": final_equity,
        "realized_price_pnl": state.realized_total,
        "fee_paid": ledger.fee_paid,
        "funding_received": ledger.funding_received if ledger.funding_available else None,
        "funding_known": result.funding_known,
        "funding_partial": ledger.funding_available and ledger.funding_unknown_windows > 0,
        "funding_unknown_windows": ledger.funding_unknown_windows,
        "funding_semantics": ledger.settlement_semantics(),
        "note": (
            "funding_received is None when no funding series was supplied; a "
            "partial series (funding_partial) means some windows lay outside "
            "the known span — unknown funding is never reported as zero"
        ),
    }
    if include_sensitivity:
        result.sensitivity = _cost_sensitivity(
            candles=ordered, proposal_source=proposal_source,
            risk_limits=limits, market_rules=rules, caps=caps, funding=funding,
            ambiguity_policy=ambiguity_policy, max_hold_hours=max_hold_hours,
            initial_cash=initial_cash, quote_currency=quote_currency,
            base_costs=model,
        )
    return result


def _cost_sensitivity(
    *, candles: list[Candle], proposal_source: ProposalSource,
    risk_limits: RiskLimits, market_rules: MarketRules, caps: SizingCaps | None,
    funding: FundingSeries | None, ambiguity_policy: str,
    max_hold_hours: float | None, initial_cash: float, quote_currency: str,
    base_costs: CostModel,
) -> list[dict[str, Any]]:
    """Net result under shifted fee and slippage rates (offline re-runs)."""
    variants: list[tuple[str, CostModel]] = [
        ("base", base_costs),
        ("fee+2bp", base_costs.shifted(fee_delta=0.0002)),
        ("fee-2bp", base_costs.shifted(fee_delta=-0.0002)),
        ("slippage+5bp", base_costs.shifted(slippage_delta=0.0005)),
    ]
    rows: list[dict[str, Any]] = []
    for name, variant in variants:
        outcome = run_simulation(
            candles,
            proposal_source=proposal_source,
            risk_limits=risk_limits,
            costs=variant,
            market_rules=market_rules,
            caps=caps,
            funding=funding,
            ambiguity_policy=ambiguity_policy,
            max_hold_hours=max_hold_hours,
            initial_cash=initial_cash,
            quote_currency=quote_currency,
            include_sensitivity=False,
        )
        rows.append({
            "variant": name,
            "final_equity": outcome.net_after_costs["final_equity"],
            "fee_paid": outcome.net_after_costs["fee_paid"],
            "ambiguity_count": outcome.ambiguity_count,
        })
    return rows


# ---------------------------------------------------------------------------
# Recorded-proposal replay: real review functions over recorded cycle data.
# ---------------------------------------------------------------------------

# The recorded review result is read from the export's confirmation_reason
# vocabulary, using the same flip classification futures_replay.summary
# establishes: direction_mismatch / doji_candle are recorded rejections,
# anything else (aligned_direction, bounded_pullback, not_required...) was a
# passed confirmation. A cycle later rejected at the proposal phase keeps its
# passed-confirmation reason — the terminal status/phase cannot decide this.
_RECORDED_REJECTION_REASONS = {"direction_mismatch", "doji_candle"}


def replay_recorded_decisions(
    cycles: list[dict[str, Any]],
    *,
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
) -> dict[str, Any]:
    """Replay recorded cycle proposals through the real confirmation rule.

    ``cycles`` are exported futures_cycles rows: the recorded proposal is the
    cycle's signal, the recorded review result is the row's confirmation
    reason in the recorded vocabulary above. The rule is a real production
    rule (e.g. server.futures_replay.RULES), never a re-implementation. The
    report separates what the recorded data can prove from what it cannot:
    the export holds only the two decision candles, so exits, funding and PnL
    are reported unavailable-with-reason, never zero.
    """
    outcomes: list[dict[str, Any]] = []
    missing_snapshot = 0
    missing_signal = 0
    for row in cycles:
        if not isinstance(row, dict):
            continue
        signal = row.get("signal")
        if not signal:
            missing_signal += 1
            continue
        snapshot = row.get("snapshot")
        symbol = str(row.get("symbol") or "")
        one_hour = candle_from_payload(
            snapshot.get("one_hour") if isinstance(snapshot, dict) else None,
            "1h", symbol,
        )
        four_hour = candle_from_payload(
            snapshot.get("four_hour") if isinstance(snapshot, dict) else None,
            "4h", symbol,
        )
        if one_hour is None or four_hour is None:
            missing_snapshot += 1
            continue

        def direction(candle: Candle) -> str:
            if candle.close > candle.open:
                return "bullish"
            if candle.close < candle.open:
                return "bearish"
            return "doji"

        context = {
            "one_hour": one_hour, "four_hour": four_hour,
            "one_hour_direction": direction(one_hour),
            "four_hour_direction": direction(four_hour),
            "one_hour_open": one_hour.open_time,
            "one_hour_close": one_hour.close,
            "four_hour_open": four_hour.open_time,
            "four_hour_close": four_hour.close,
        }
        confirmed, reason = confirmation_rule(context, signal)
        recorded_confirmed = (
            row.get("confirmation_reason") not in _RECORDED_REJECTION_REASONS
        )
        outcomes.append({
            "cycle_id": row.get("cycle_id"),
            "symbol": symbol,
            "recorded_confirmation_reason": row.get("confirmation_reason"),
            "recorded_confirmed": recorded_confirmed,
            "rule_result": confirmed,
            "rule_reason": reason,
        })
    agreements = sum(
        1 for outcome in outcomes
        if outcome["rule_result"] == outcome["recorded_confirmed"]
    )
    reason_counts = Counter(outcome["rule_reason"] for outcome in outcomes)
    return {
        # A gate-agreement report, not a financial simulation: this compares the
        # rule's decision with the recorded one and states that exits, funding
        # and PnL are unavailable from a decision-only export.
        "kind": "gate_agreement_replay",
        "disclaimer": DISCLAIMER,
        "recorded_cycles": len(cycles),
        "replayed_with_snapshot": len(outcomes),
        "skipped_missing_signal": missing_signal,
        "skipped_missing_snapshot": missing_snapshot,
        "decision_agreements": agreements,
        "decision_disagreements": len(outcomes) - agreements,
        "rule_reason_distribution": dict(sorted(reason_counts.items())),
        "financial_simulation": {
            "status": "unavailable",
            "reason": (
                "the recorded export holds only the two decision candles (1h + 4h) "
                "per cycle: no post-decision candle history, no recorded portfolio "
                "or proposal execution terms, so exits, funding and net PnL cannot "
                "be replayed from this data. Missing data is not zero."
            ),
        },
    }


def main() -> int:  # pragma: no cover - CLI entry lives in scripts/replay_futures_decisions.py
    raise SystemExit(
        "the recorded-decision replay CLI is scripts/replay_futures_decisions.py"
    )


if __name__ == "__main__":
    main()
