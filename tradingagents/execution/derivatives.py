"""Derivatives planning and gated execution (FR-D): one responsibility.

A ``DerivativesExecutor`` composes an ``ExchangeBridge`` (shared config
merge, venue client, audit shape, redaction and RiskGuard) and adds only
the derivatives-specific surface: unified symbols ``base/quote:settle``
(e.g. ``BTC/USDT:USDT``), short semantics (side ``sell`` opens a short, no
held position required), margin sizing with leverage, and the three-layer
fail-closed gate:

(a) the config ``exec_derivatives`` key must be a literal True;
(b) the ``TRADINGAGENTS_EXEC_DERIVATIVES`` env flag must read true at call
    time, fail-closed (same pattern as TRADINGAGENTS_EXEC_LIVE);
(c) every execution needs ``confirm=True`` -- INCLUDING dry-runs, unlike
    the L1 spot bridge, per FR-D2.

Missing any layer refuses with an audit line and sends nothing.

PRD principle, restated for the operator: the derivatives gates ship OFF.
A sandbox or dry-run never proves the live path -- per-order confirm is
required even in dry-run so the operator consciously re-arms every send,
and the live path additionally needs both FR5 gates plus a raised
``risk_max_derivatives_exposure_pct`` (default 0 blocks all notional).

Risk: the shared RiskGuard caps leverage at ``risk_max_derivatives_leverage``
and derivatives notional at ``risk_max_derivatives_exposure_pct`` of equity;
plan sizing uses margin (``cost``) with notional = margin * leverage.
"""

from __future__ import annotations

import contextlib
import math
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from server import job_store, snapshot_observer
from tradingagents.agents.rating import RATINGS_5_TIER, is_review
from tradingagents.dataflows.symbols import futures_base
from tradingagents.default_config import _BOOL_TRUE
from tradingagents.execution import futures_identity
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.futures_entry_journal import (
    create_guarded_entry_intent,
    entry_journal_leg,
    update_guarded_entry_intent,
)
from tradingagents.execution.futures_operation_lock import OperationGuard, operation_lock
from tradingagents.execution.futures_safety import (
    FuturesPlanError,
    risk_limited_quantity,
    validate_bracket,
)
from tradingagents.execution.scale_out import plan_scale_out
from tradingagents.execution.sizing import PlannedOrder, SizingRules
from tradingagents.portfolio import PortfolioContext

_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER


def _refresh_live_risk_context(executor: DerivativesExecutor, order: PlannedOrder, exchange: Any) -> None:
    from server.futures_decision_context import build_futures_portfolio
    from tradingagents.execution.futures_account import (
        observe_futures_positions,
        read_futures_balance,
    )

    snapshot = read_futures_balance(executor._bridge)
    if executor._bridge.client() is not exchange:
        raise ValueError("futures client changed during account read")
    positions = observe_futures_positions(executor._bridge)
    if executor._bridge.client() is not exchange:
        raise ValueError("futures client changed during positions read")
    if not positions.scope_complete or positions.unknown_scope or positions.unknown_symbols:
        raise ValueError("futures position scope is unverified")
    snapshot = replace(snapshot, positions=positions.positions)
    if _positive_number(snapshot.equity) is None or not snapshot.equity_currency:
        raise ValueError("futures equity or currency is unavailable")
    portfolio = build_futures_portfolio(snapshot)
    open_orders = exchange.fetch_open_orders(order.ccxt_symbol)
    if executor._bridge.client() is not exchange:
        raise ValueError("futures client changed during open order read")
    if not isinstance(open_orders, list) or any(not isinstance(item, dict) for item in open_orders):
        raise ValueError("open order response is invalid")
    if open_orders:
        raise ValueError("existing open orders make a new entry unsafe")
    decision = executor.risk_guard.check(
        order, portfolio, executor.config["exec_log_path"], fail_closed=True,
    )
    executor._record_risk_decision("execute_locked", decision)
    if decision.halted or not decision.allowed:
        raise PermissionError(f"risk guard refused execution: {decision.reason}")


def _env_derivatives_flag() -> bool:
    """Read TRADINGAGENTS_EXEC_DERIVATIVES at call time, fail-closed.

    The flag is deliberately absent from _ENV_OVERRIDES: its only consumer
    is this read, so it can never fold into the config half of the gate.
    None/"" or anything outside true/1/yes/on keeps derivatives closed.
    """
    import os

    raw = os.environ.get("TRADINGAGENTS_EXEC_DERIVATIVES")
    if not raw:
        return False
    return raw.strip().lower() in _BOOL_TRUE


class DerivativesExecutor:
    """Plan and execute leveraged orders behind the three-layer gate."""

    def __init__(
        self,
        config: dict | None = None,
        exchange_factory: Any = None,
        rules: SizingRules | None = None,
        exchange_id: str | None = None,
        risk_guard: Any = None,
    ) -> None:
        self._bridge = ExchangeBridge(
            config=config,
            exchange_factory=exchange_factory,
            rules=rules,
            exchange_id=exchange_id,
            risk_guard=risk_guard,
        )
        self.config = self._bridge.config
        self.rules = self._bridge.rules
        self.risk_guard = self._bridge.risk_guard
        self.risk_guard_decisions: list[dict[str, Any]] = []

    def _record_risk_decision(self, stage: str, decision: Any) -> None:
        self.risk_guard_decisions.append({
            "stage": stage,
            "allowed": bool(decision.allowed),
            "halted": bool(decision.halted),
            "reason": decision.reason,
        })

    # --- symbols ---------------------------------------------------------

    def symbol_for(self, ticker: str) -> str:
        """Return the offline derivatives symbol shape for a ticker."""
        pinned = self._bridge.pinned_futures_symbol(ticker)
        if pinned:
            return pinned
        override = self.config.get("exec_symbol_overrides")
        if isinstance(override, dict) and override.get(ticker):
            spot = str(override[ticker])
            if ":" in spot:
                return spot
            if "/" not in spot:
                raise ValueError(
                    f"exec_symbol_overrides[{ticker!r}] = {spot!r} is not a recognizable "
                    "ccxt symbol (expected BASE/QUOTE, optionally BASE/QUOTE:SETTLE)"
                )
            quote = spot.split("/", 1)[1]
            return f"{spot}:{quote}"
        base = futures_base(ticker)
        if not base:
            raise ValueError(f"{ticker!r} is not a valid futures ticker")
        quote = str(self.config.get("exec_quote_currency") or "USDT").strip().upper()
        if not quote:
            raise ValueError("exec_quote_currency is empty; cannot build a futures symbol")
        return f"{base}/{quote}:{quote}"

    def resolve_market(self, ticker: str) -> str:
        """Resolve an active venue contract only after derivatives gates open."""
        offline_symbol = self.symbol_for(ticker)
        shell = PlannedOrder(
            ticker=ticker, ccxt_symbol=offline_symbol, signal="REVIEW",
            market="derivatives",
        )
        if self.config.get("exec_derivatives") is not True:
            raise self._refuse(
                shell,
                "exec_derivatives is not armed in config (literal True required); "
                "nothing was planned",
                phase="resolve",
            )
        if not _env_derivatives_flag():
            raise self._refuse(
                shell,
                "TRADINGAGENTS_EXEC_DERIVATIVES is not set to true at call time; "
                "nothing was planned",
                phase="resolve",
            )
        return self._bridge.futures_symbol_for(ticker)

    # --- planning (FR-D1) --------------------------------------------------

    def plan_order(
        self,
        ticker: str,
        signal: str,
        portfolio: PortfolioContext | None = None,
        *,
        price: float | None = None,
        leverage: float = 1.0,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        risk_fraction: float | None = None,
        margin_fraction: float | None = None,
        margin_mode: str | None = None,
        futures_execution_intent: str | None = None,
    ) -> PlannedOrder:
        """Size a derivatives plan; refuses (audited) unless gates (a)+(b) are open.

        ``side`` ``sell`` opens a SHORT -- no held position is needed, so
        Sell/Underweight size like Buy/Overweight instead of reading the
        book. ``cost`` is the margin; quantity covers notional*leverage.
        Without ``price`` the plan is a network read.

        ``futures_execution_intent`` is the authorization for a NEW position:
        a tradeable rating is only a market view, so planning refuses unless
        the caller passes exactly ``"enter"`` (the Portfolio Manager's typed
        decision). Underweight/Sell never authorize a short on their own.
        ``Hold``/``REVIEW`` stay no-order plans and need no authorization.

        ``risk_fraction``/``margin_fraction``/``margin_mode`` are the decision
        agents' terms; ``None`` keeps the configured sizing defaults. The
        plan refuses (never substitutes) when a term is unusable.
        """
        offline_symbol = self.symbol_for(ticker)
        # The shell stays valid while the terms are still unvalidated: an
        # unrecognized margin_mode must become a refused plan (audited) rather
        # than a pydantic ValidationError escaping the planner.
        authorized = "enter" if futures_execution_intent == "enter" else None
        shell = PlannedOrder(
            ticker=ticker, ccxt_symbol=offline_symbol, signal=signal,
            market="derivatives", leverage=leverage,
            futures_execution_intent=authorized,
        )
        if self.config.get("exec_derivatives") is not True:
            raise self._refuse(
                shell,
                "exec_derivatives is not armed in config (literal True required); "
                "nothing was planned",
                phase="plan",
            )
        if not _env_derivatives_flag():
            raise self._refuse(
                shell,
                "TRADINGAGENTS_EXEC_DERIVATIVES is not set to true at call time; "
                "nothing was planned",
                phase="plan",
            )
        if signal in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT) and authorized is None:
            raise self._refuse(
                shell,
                "futures entry requires futures_execution_intent='enter' "
                f"(got {futures_execution_intent!r}): an investment rating alone "
                "never authorizes a new futures position; nothing was planned",
                phase="plan",
            )
        ccxt_symbol = self.resolve_market(ticker)
        if price is None:
            price = self._bridge.fetch_price(ticker)
        mode = self._bridge.effective_mode()
        side = "buy" if signal in (_BUY, _OVERWEIGHT) else "sell" if signal in (_SELL, _UNDERWEIGHT) else None
        if side is not None and stop_loss is None and take_profit is None:
            order = self._size(
                signal=signal, ticker=ticker, ccxt_symbol=ccxt_symbol,
                portfolio=portfolio, price=price, leverage=leverage,
                stop_loss=None, take_profit=None,
                risk_fraction=risk_fraction, margin_fraction=margin_fraction,
                margin_mode=margin_mode,
                futures_execution_intent=authorized,
            )
            order.mode = mode
            self._bridge.audit_plan(order, action="no_order", reason=order.reason, phase="plan")
            return order
        order = self._size(
            signal=signal, ticker=ticker, ccxt_symbol=ccxt_symbol,
            portfolio=portfolio, price=price, leverage=leverage,
            stop_loss=stop_loss, take_profit=take_profit,
            risk_fraction=risk_fraction, margin_fraction=margin_fraction,
            margin_mode=margin_mode,
            futures_execution_intent=authorized,
        )
        order.mode = mode
        decision = self.risk_guard.check(order, portfolio, self.config["exec_log_path"])
        self._record_risk_decision("plan", decision)
        if decision.halted:
            self._bridge.audit_plan(
                order, action="rejected_by_risk", reason=decision.reason, phase="plan"
            )
            raise PermissionError(f"risk guard halted trading: {decision.reason}")
        if not decision.allowed:
            rejected = order.model_copy(
                update={"side": None, "quantity": None, "cost": None, "reason": decision.reason}
            )
            self._bridge.audit_plan(
                rejected, action="rejected_by_risk", reason=decision.reason, phase="plan"
            )
            return rejected
        self._bridge.audit_plan(order, action=order.side or "no_order", reason=order.reason)
        return order

    # --- execution (FR-D2) --------------------------------------------------

    def execute_order(
        self,
        order: PlannedOrder,
        confirm: bool = False,
        portfolio: PortfolioContext | None = None,
    ) -> str | None:
        """Execute a derivatives plan behind gates (a)+(b)+(c).

        Layer (c): ``confirm=True`` is required for EVERY send, including
        dry-runs. ``portfolio`` is MANDATORY and fail-closed: without it the
        risk guard's percentage caps (leverage, derivatives notional) would
        skip fail-open, so a None portfolio refuses before the guard runs.
        The risk guard re-checks immediately before anything is sent; live
        sends need the FR5 double gate as well.
        """
        if order.market != "derivatives":
            raise ValueError("DerivativesExecutor executes derivatives orders only")
        if order.needs_review or order.signal not in RATINGS_5_TIER:
            raise ValueError(
                f"order signal {order.signal!r} is not a tradeable rating; nothing was sent"
            )
        if order.side is None or order.quantity is None:
            raise ValueError(
                f"not an executable order: side={order.side!r}, quantity={order.quantity!r}"
            )
        if order.futures_execution_intent != "enter":
            self._bridge.audit_plan(
                order,
                action="derivatives_denied",
                reason=(
                    "futures execution requires explicit futures_execution_intent='enter'; "
                    "nothing was sent"
                ),
                confirmed=confirm,
                phase="execute",
            )
            raise PermissionError(
                "futures execution requires explicit futures_execution_intent='enter'; "
                "nothing was sent"
            )
        try:
            validate_bracket(
                side=order.side,
                entry=order.estimated_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
            )
        except FuturesPlanError as exc:
            self._bridge.audit_plan(
                order, action="derivatives_denied", reason=str(exc),
                confirmed=confirm, phase="execute",
            )
            raise PermissionError(str(exc)) from exc
        if self.config.get("exec_derivatives") is not True:
            raise self._refuse(
                order,
                "exec_derivatives is not armed in config (literal True required); "
                "nothing was sent",
                phase="execute",
            )
        if not _env_derivatives_flag():
            raise self._refuse(
                order,
                "TRADINGAGENTS_EXEC_DERIVATIVES is not set to true at call time; "
                "nothing was sent",
                phase="execute",
            )
        if portfolio is None or (portfolio.cash is None and not portfolio.positions):
            # Fail-closed on purpose: the guard's percentage caps need equity.
            # A missing portfolio OR an unmarkable one (no cash, no positions)
            # would both make _equity return None and silently disarm
            # risk_max_derivatives_exposure_pct (default 0 blocks all notional).
            raise self._refuse(
                order,
                "derivatives execution requires a portfolio with computable equity: "
                "the risk guard's percentage caps fail open without it; "
                "nothing was sent",
                phase="execute",
            )
        mode = self._bridge.effective_mode()
        if confirm is not True:
            self._bridge.audit_plan(
                order, action="derivatives_denied",
                reason="derivatives execution requires confirm=True, even in dry-run",
                confirmed=confirm, phase="execute",
            )
            raise PermissionError(
                "derivatives execution requires confirm=True, even in dry-run; "
                "nothing was sent"
            )
        if mode == "live":
            try:
                from server.futures_decision_context import build_futures_portfolio
                from tradingagents.execution.futures_account import (
                    observe_futures_positions,
                    read_futures_balance,
                )

                client = self._bridge.client()
                snapshot = read_futures_balance(self._bridge)
                if self._bridge.client() is not client:
                    raise ValueError("futures client changed during account read")
                positions = observe_futures_positions(self._bridge)
                if self._bridge.client() is not client:
                    raise ValueError("futures client changed during positions read")
                if not positions.scope_complete or positions.unknown_scope or positions.unknown_symbols:
                    raise ValueError("futures position scope is unverified")
                snapshot = replace(snapshot, positions=positions.positions)
                if _positive_number(snapshot.equity) is None or not snapshot.equity_currency:
                    raise ValueError("futures equity or currency is unavailable")
                portfolio = build_futures_portfolio(snapshot)
                open_orders = client.fetch_open_orders(order.ccxt_symbol)
                if self._bridge.client() is not client:
                    raise ValueError("futures client changed during open order read")
                if not isinstance(open_orders, list) or any(not isinstance(item, dict) for item in open_orders):
                    raise ValueError("open order response is invalid")
                if open_orders:
                    raise ValueError("existing open orders make a new entry unsafe")
            except Exception as exc:
                # Carry the cause itself, not only its type: the branches above
                # raise distinct ValueErrors (unverified scope, missing equity
                # currency, resting orders) and the venue raises its own, so a
                # bare type name leaves every refusal indistinguishable in the
                # audit trail and the cycle funnel.
                raise self._refuse(
                    order,
                    "fresh futures account or open orders unavailable: "
                    f"{type(exc).__name__}: {exc}",
                    phase="execute",
                ) from exc
        # fail_closed=True: same rule as the spot execute re-check -- an
        # unreadable audit log hides the halt/streak state a send decision
        # needs, so it refuses instead of passing.
        decision = self.risk_guard.check(
            order, portfolio, self.config["exec_log_path"], fail_closed=True
        )
        self._record_risk_decision("execute", decision)
        if decision.halted or not decision.allowed:
            self._bridge.audit_plan(
                order, action="rejected_by_risk", reason=decision.reason,
                confirmed=confirm, phase="execute",
            )
            raise PermissionError(f"risk guard refused execution: {decision.reason}")
        if mode == "dry":
            self._bridge.audit_plan(
                order, action=order.side or "no_order",
                reason="dry-run: order not sent to exchange",
                confirmed=confirm, phase="execute",
            )
            return None
        identity = futures_identity.read_futures_account_identity(self._bridge)
        if identity is None:
            raise self._refuse(
                order,
                "live entry unavailable: authenticated account identity cannot be bound to protective state",
                phase="execute",
            )
        return self._execute_live(order, confirm, identity)

    # --- internals -----------------------------------------------------------

    def _size(
        self,
        *,
        signal: str,
        ticker: str,
        ccxt_symbol: str,
        portfolio: PortfolioContext | None,
        price: float | None,
        leverage: float,
        stop_loss: float | None,
        take_profit: float | None,
        risk_fraction: float | None = None,
        margin_fraction: float | None = None,
        margin_mode: str | None = None,
        futures_execution_intent: str | None = None,
    ) -> PlannedOrder:
        """Derivatives mapping of the 5-tier signal; always returns a plan."""
        book = portfolio if portfolio is not None else PortfolioContext()
        floor = self.rules.min_order_cost
        # PlannedOrder only carries a valid literal; an unrecognized mode is
        # refused below with its own reason instead of failing validation here.
        plan_margin_mode = margin_mode if margin_mode in ("isolated", "cross") else None

        def no_order(reason: str, *, needs_review: bool = False) -> PlannedOrder:
            return PlannedOrder(
                ticker=ticker, ccxt_symbol=ccxt_symbol, signal=signal,
                market="derivatives", leverage=leverage,
                margin_mode=plan_margin_mode,
                futures_execution_intent=futures_execution_intent,
                needs_review=needs_review, reason=reason,
            )

        if is_review(signal):
            return no_order("REVIEW signal: no order without human review", needs_review=True)
        if signal == _HOLD:
            return no_order("Hold: no trade")
        if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
            return no_order(
                f"unrecognized signal {signal!r}: no order without human review",
                needs_review=True,
            )
        if price is None or price <= 0:
            return no_order("price unavailable")
        if book.cash is None:
            return no_order("cash unavailable")
        if margin_mode is not None and margin_mode not in ("isolated", "cross"):
            return no_order(
                f"unrecognized margin_mode {margin_mode!r}: expected 'isolated' or 'cross'"
            )
        long = signal in (_BUY, _OVERWEIGHT)
        side = "buy" if long else "sell"
        if stop_loss is None or take_profit is None:
            return no_order("derivatives entry requires explicit stop_loss and take_profit")
        risk_fraction = (
            float(risk_fraction) if risk_fraction is not None
            else float(self.config.get("risk_derivatives_risk_fraction", 0.01))
        )
        margin_fraction = (
            float(margin_fraction) if margin_fraction is not None
            else float(self.config.get("risk_derivatives_margin_fraction", 0.25))
        )
        risk_cap = float(self.config.get("risk_derivatives_max_risk_fraction", 0.02))
        margin_cap = float(self.config.get("risk_derivatives_max_margin_fraction", 0.5))
        if risk_fraction > risk_cap:
            return no_order(
                f"proposed risk_fraction {risk_fraction:g} exceeds the hard cap {risk_cap:g}"
            )
        if margin_fraction > margin_cap:
            return no_order(
                f"proposed margin_fraction {margin_fraction:g} exceeds the hard cap {margin_cap:g}"
            )
        try:
            validate_bracket(side=side, entry=price, stop_loss=stop_loss, take_profit=take_profit)
            equity = float(book.cash)
            quantity = risk_limited_quantity(
                side=side, equity=equity, entry=price, stop_loss=stop_loss,
                take_profit=take_profit, leverage=leverage,
                risk_fraction=risk_fraction,
                margin_fraction=margin_fraction,
            )
        except (FuturesPlanError, TypeError, ValueError) as exc:
            return no_order(f"derivatives sizing refused: {exc}")
        cost = quantity * price / leverage
        if cost * leverage < floor:
            return no_order("cost below exchange minimum")
        return PlannedOrder(
            ticker=ticker, ccxt_symbol=ccxt_symbol, signal=signal,
            side=side, quantity=quantity, estimated_price=price, cost=cost,
            market="derivatives", leverage=leverage, margin_mode=plan_margin_mode,
            futures_execution_intent=futures_execution_intent,
            stop_loss=stop_loss, take_profit=take_profit,
        )

    def _execute_live(self, order: PlannedOrder, confirm: bool, identity: Any = None) -> str | None:
        exchange = self._bridge.client()
        if not getattr(exchange, "apiKey", "") or not getattr(exchange, "secret", ""):
            self._bridge.audit_plan(
                order, action="derivatives_denied",
                reason="missing exchange API credentials",
                confirmed=confirm, phase="execute",
            )
            from tradingagents.execution.exchange_client import credential_env_names

            key_env, secret_env = credential_env_names(
                self._bridge.exchange_id, self._bridge.config.get("exec_base_url")
            )
            raise RuntimeError(
                f"live derivatives execution requires {key_env} and {secret_env}; "
                "nothing was sent"
            )
        with operation_lock(order.ccxt_symbol) as guard:
            return self._execute_live_locked(order, confirm, identity, guard)

    def _execute_live_locked(
        self, order: PlannedOrder, confirm: bool, identity: Any, guard: OperationGuard,
    ) -> str | None:
        guard.check()
        exchange = self._bridge.client()
        api_key = getattr(exchange, "apiKey", "") or ""
        secret = getattr(exchange, "secret", "") or ""
        if not api_key or not secret:
            self._bridge.audit_plan(
                order, action="derivatives_denied",
                reason="missing exchange API credentials",
                confirmed=confirm, phase="execute",
            )
            from tradingagents.execution.exchange_client import credential_env_names

            key_env, secret_env = credential_env_names(
                self._bridge.exchange_id, self._bridge.config.get("exec_base_url")
            )
            raise RuntimeError(
                f"live derivatives execution requires {key_env} and {secret_env}; "
                "nothing was sent"
            )
        if identity is None:
            # Direct callers (recovery tooling) read the same seam the public
            # entry gate uses; nothing is sent without a binding.
            identity = futures_identity.read_futures_account_identity(self._bridge)
        if identity is None:
            raise RuntimeError(
                "live derivatives entry requires an authenticated account identity; "
                "nothing was sent"
            )
        import ccxt

        entry_client_id = "entry-" + secrets.token_hex(12)
        intent_id = f"entry-intent:{order.ccxt_symbol}:{entry_client_id}"
        created_at = datetime.now(UTC).isoformat()
        selected_venue = venue_key(self._bridge)
        intent_leg = entry_journal_leg(
            order, entry_client_id, quantity=str(order.quantity), created_at=created_at,
            identity=identity, venue=selected_venue, config=self.config,
        )
        try:
            create_guarded_entry_intent(
                guard, intent_id, order, entry_client_id, identity, selected_venue, intent_leg,
            )
        except PermissionError as exc:
            raise self._refuse(order, str(exc), phase="execute") from exc
        entry_sent = False
        try:
            guard.check()
            exchange = self._bridge.client()
            api_key = getattr(exchange, "apiKey", "") or ""
            secret = getattr(exchange, "secret", "") or ""
            if not api_key or not secret:
                from tradingagents.execution.exchange_client import credential_env_names

                key_env, secret_env = credential_env_names(
                    self._bridge.exchange_id, self._bridge.config.get("exec_base_url")
                )
                raise RuntimeError(
                    f"live derivatives execution requires {key_env} and {secret_env}; nothing was sent"
                )
            if futures_identity.compare_futures_account_identity(self._bridge, identity) \
                    is not futures_identity.IdentityComparison.MATCH:
                raise RuntimeError("futures account identity changed before entry; nothing was sent")
            guard.check()
            _refresh_live_risk_context(self, order, exchange)

            # Precision depends on loaded market metadata; journal the exact
            # venue string before margin, leverage, or order mutations.
            guard.check()
            markets = exchange.load_markets()
            if self._bridge.client() is not exchange:
                raise ValueError("futures client changed while loading markets")
            resolved_symbol = self._bridge.futures_symbol_for(order.ticker, markets=markets)
            if resolved_symbol != order.ccxt_symbol:
                raise ValueError(
                    f"futures market changed from {order.ccxt_symbol!r} to "
                    f"{resolved_symbol!r}; nothing was sent"
                )
            amount = exchange.amount_to_precision(order.ccxt_symbol, order.quantity)
            intent_leg = entry_journal_leg(
                order, entry_client_id, quantity=str(amount), created_at=created_at,
                identity=identity, venue=selected_venue, config=self.config,
            )
            update_guarded_entry_intent(
                guard, intent_id, status="submission_uncertain", leg=intent_leg,
                order_id=None, error="exact rounded entry terms journaled before venue mutation",
            )

            if self._bridge.exchange_id == "binance":
                guard.check()
                position_mode = exchange.fetch_position_mode(
                    order.ccxt_symbol, params={"subType": "linear"},
                )
                if self._bridge.client() is not exchange:
                    raise ValueError("futures client changed during position-mode read")
                if not isinstance(position_mode, dict) or position_mode.get("hedged") is not False:
                    raise ValueError(
                        "venue is in hedge mode (or reported no position mode); "
                        "reduceOnly protective legs need one-way mode; nothing was sent"
                    )
            if order.margin_mode is not None:
                guard.check()
                with contextlib.suppress(ccxt.MarginModeAlreadySet):
                    snapshot_observer.run(
                        self._bridge, exchange,
                        operation="set_margin_mode", symbol=order.ccxt_symbol,
                        call=lambda: (guard.check(), exchange.set_margin_mode(
                            order.margin_mode, order.ccxt_symbol,
                        ))[1],
                    )
            guard.check()
            snapshot_observer.run(
                self._bridge, exchange,
                operation="set_leverage", symbol=order.ccxt_symbol,
                call=lambda: (guard.check(), exchange.set_leverage(
                    int(order.leverage), order.ccxt_symbol,
                ))[1],
            )
            guard.check()
            entry_sent = True
            response = snapshot_observer.run(
                self._bridge, exchange,
                operation="create_futures_entry", symbol=order.ccxt_symbol,
                call=lambda: (guard.check(), exchange.create_order(
                    order.ccxt_symbol, "market", order.side, amount,
                    params={"clientOrderId": entry_client_id},
                ))[1],
            )
            guard.check()
            order_id = response.get("id") if isinstance(response, dict) else None
        except (ccxt.NetworkError, ccxt.ExchangeError, RuntimeError, ValueError) as exc:
            if not entry_sent:
                try:
                    intent_leg["status"] = "cancelled"
                    update_guarded_entry_intent(
                        guard, intent_id, status="cancelled", leg=intent_leg,
                        order_id=None, error=f"entry not submitted: {type(exc).__name__}",
                    )
                except Exception:
                    pass
            if isinstance(exc, (ccxt.NetworkError, ccxt.ExchangeError)):
                self._bridge.audit_plan(
                    order, action="rejected_at_execute",
                    reason=f"exchange error: {exc}", confirmed=confirm, phase="execute",
                )
                raise RuntimeError(
                    f"live derivatives order on {order.ccxt_symbol} failed: {exc}"
                ) from exc
            self._bridge.audit_plan(
                order, action="rejected_at_execute",
                reason=f"entry preflight or lock error: {exc!r}", confirmed=confirm, phase="execute",
            )
            raise RuntimeError(
                f"live derivatives order on {order.ccxt_symbol} failed: {exc!r}"
            ) from exc
        except Exception as exc:  # safety #4: nothing escapes the path unaudited
            if not entry_sent:
                try:
                    intent_leg["status"] = "cancelled"
                    update_guarded_entry_intent(
                        guard, intent_id, status="cancelled", leg=intent_leg,
                        order_id=None, error=f"entry not submitted: {type(exc).__name__}",
                    )
                except Exception:
                    pass
            self._bridge.audit_plan(
                order, action="rejected_at_execute",
                reason=f"unexpected error: {exc!r}", confirmed=confirm, phase="execute",
            )
            raise RuntimeError(
                f"live derivatives order on {order.ccxt_symbol} failed: {exc!r}"
            ) from exc

        intent_leg.update(status="uncertain", order_id=order_id)
        response = response if isinstance(response, dict) else {}
        actual_quantity = _positive_number(response.get("filled"))
        actual_entry = _positive_number(response.get("average")) or _positive_number(response.get("price"))
        response_status = str(response.get("status") or "").lower()
        complete = (
            actual_quantity is not None
            and response_status in {"closed", "filled"}
            and math.isclose(actual_quantity, float(amount), rel_tol=1e-9, abs_tol=1e-12)
        )
        residual_warning = (
            "entry partial or remaining quantity unknown; residual exposure unresolved; "
            "account identity unverified, remaining entry not cancelled; "
            "only confirmed fill can be protected, protection unverified"
        )
        post_ack_errors: list[tuple[str, Exception]] = []
        try:
            job_store.update_bracket_recovery(
                intent_id, status="submission_uncertain", placed_ids=[str(order_id)] if order_id else [],
                attempts=0, last_error="bracket resting status unverified; reconciliation required" if complete else residual_warning,
                now=datetime.now(UTC).isoformat(), legs=[intent_leg],
            )
        except Exception as exc:
            post_ack_errors.append(("entry intent update", exc))
        if actual_quantity is None or actual_entry is None:
            if actual_quantity is not None:
                try:
                    BracketManager(self._bridge).place(
                        order.model_copy(update={"quantity": actual_quantity}), confirm, order_id,
                    )
                except Exception as exc:
                    post_ack_errors.append(("bracket attempt failed or unverified", exc))
            try:
                self._bridge.audit_plan(
                    order, action="entry_uncertain", confirmed=confirm, order_id=order_id,
                    reason="entry fill quantity or price is unknown; manual reconciliation required",
                    phase="execute",
                )
            except Exception as exc:
                post_ack_errors.append(("entry audit", exc))
            if not complete:
                try:
                    self._bridge.audit_plan(
                        order, action="entry_partial_or_unknown", confirmed=confirm,
                        order_id=order_id, reason=residual_warning, phase="execute",
                    )
                except Exception as exc:
                    post_ack_errors.append(("residual audit", exc))
            details = "; ".join(f"{stage}: {type(exc).__name__}" for stage, exc in post_ack_errors)
            message = "entry fill is unknown; durable intent retained for reconciliation"
            if not complete:
                message += f"; {residual_warning}"
            if actual_quantity is not None:
                message += "; bracket attempted for confirmed quantity; protection unverified"
            if details:
                message += f"; post-ACK errors: {details}"
            raise RuntimeError(message) from (post_ack_errors[0][1] if post_ack_errors else None)
        actual_order = order.model_copy(update={
            "quantity": actual_quantity,
            "estimated_price": actual_entry,
        })
        try:
            self._bridge.audit_plan(
                actual_order,
                action=order.side or "no_order",
                confirmed=confirm,
                order_id=order_id,
                phase="execute",
            )
        except Exception as exc:
            post_ack_errors.append(("entry audit", exc))
        if order.stop_loss is not None or order.take_profit is not None:
            position_key = f"{order.ccxt_symbol}:{order.side}:{order_id or 'unknown'}"
            try:
                rounded_stop = (
                    float(exchange.price_to_precision(order.ccxt_symbol, order.stop_loss))
                    if order.stop_loss is not None else actual_entry
                )
                if not math.isfinite(rounded_stop) or rounded_stop <= 0:
                    raise ValueError("rounded stop is not finite and positive")
                scale_out = self._scale_out_plan(actual_order)
                target_prices = (
                    [level.price for level in scale_out.levels] if scale_out is not None
                    else [order.take_profit] if order.take_profit is not None else []
                )
                persisted = job_store.persist_futures_entry_state(
                    position_key,
                    side="long" if order.side == "buy" else "short",
                    entry=actual_entry,
                    quantity=actual_quantity,
                    stop=rounded_stop,
                    targets=[float(exchange.price_to_precision(order.ccxt_symbol, price)) for price in target_prices],
                    mark=actual_entry,
                    order_id=str(order_id) if order_id is not None else None,
                    identity=identity,
                )
                if not persisted:
                    raise RuntimeError("durable position state could not be initialized")
            except Exception as exc:
                post_ack_errors.append(("position state", exc))
                try:
                    self._bridge.audit_plan(
                        order,
                        action="position_state_failed",
                        reason="live entry was placed but durable position state could not be initialized",
                        confirmed=confirm,
                        order_id=order_id,
                        phase="execute",
                    )
                except Exception as audit_exc:
                    post_ack_errors.append(("position state audit", audit_exc))
            try:
                self._place_bracket(actual_order, confirm, order_id)
            except Exception as exc:
                post_ack_errors.append(("bracket attempt failed or unverified", exc))
        if post_ack_errors:
            details = "; ".join(f"{stage}: {type(exc).__name__}" for stage, exc in post_ack_errors)
            warning = f"; {residual_warning}" if not complete else ""
            raise RuntimeError(
                f"post-ACK processing failed for order {order_id!r} on {order.ccxt_symbol}; "
                f"bracket attempted for confirmed quantity; protection unverified; {details}{warning}"
            ) from post_ack_errors[0][1]
        if not complete:
            self._bridge.audit_plan(
                actual_order, action="entry_partial_or_unknown", confirmed=confirm,
                order_id=order_id, reason=residual_warning, phase="execute",
            )
            raise RuntimeError(f"{residual_warning}; durable intent retained")
        raise RuntimeError(
            f"entry order {order_id!r} filled on {order.ccxt_symbol}, but bracket resting status "
            "is unverified; durable intent retained for reconciliation"
        )

    def _place_bracket(self, order: PlannedOrder, confirm: bool, entry_order_id: str | None) -> None:
        manager = BracketManager(self._bridge)
        try:
            plan = self._scale_out_plan(order)
        except Exception:
            manager.place(order, confirm, entry_order_id)
            raise
        if plan is None:
            manager.place(order, confirm, entry_order_id)
            return
        manager.place_ladder(
            order,
            [(level.price, level.quantity) for level in plan.levels],
            confirm,
            entry_order_id,
        )

    def _scale_out_plan(self, order: PlannedOrder):
        """The agent's single TP is replaced by deliberate R tranches.

        An invalid configuration falls back to the single bracket (audited)
        rather than leaving the filled entry unprotected.
        """
        if self.config.get("risk_derivatives_scale_out_enabled") is not True:
            return None
        if order.stop_loss is None or order.quantity is None or order.side is None:
            return None
        try:
            return plan_scale_out(
                side="long" if order.side == "buy" else "short",
                entry=float(order.estimated_price),
                initial_stop=float(order.stop_loss),
                contracts=float(order.quantity),
                first_r=float(self.config.get("risk_derivatives_scale_out_first_r", 1.0)),
                first_fraction=float(self.config.get("risk_derivatives_scale_out_first_fraction", 0.33)),
                second_r=float(self.config.get("risk_derivatives_scale_out_second_r", 2.0)),
                second_fraction=float(self.config.get("risk_derivatives_scale_out_second_fraction", 0.33)),
            )
        except (TypeError, ValueError) as exc:
            self._bridge.audit_plan(
                order, action="scale_out_refused", reason=f"single bracket used: {exc}",
                confirmed=True, phase="execute",
            )
            return None

    def recover_bracket(self, task_id: str, *, confirm: bool = True) -> str:
        """Repair protective exits only while live and derivatives gates are open."""
        if confirm is not True:
            raise PermissionError("bracket recovery requires confirm=True")
        if self.config.get("exec_derivatives") is not True or not _env_derivatives_flag():
            raise PermissionError("bracket recovery requires derivatives execution gates")
        if self.effective_mode() != "live":
            raise PermissionError("bracket recovery requires live execution gates")
        return BracketManager(self._bridge).reconcile(
            task_id, repair_missing=True, allow_sibling_cancel=True,
        )

    def monitor_bracket(self, task_id: str) -> str:
        """Inspect protective exits; entry intents use read-only recovery first."""
        allow_cancel = (
            self.config.get("exec_derivatives") is True
            and _env_derivatives_flag()
            and self.effective_mode() == "live"
        )
        task = job_store.load_bracket_recovery(task_id)
        if task is None:
            raise KeyError(task_id)
        with operation_lock(task["symbol"]) as guard:
            guard.check()
            current = job_store.load_bracket_recovery(task_id)
            if current is None:
                raise KeyError(task_id)
            if current["task_id"].startswith("entry-intent:"):
                from tradingagents.execution.futures_entry_recovery import reconcile_entry

                return reconcile_entry(self._bridge, task_id)
            return BracketManager(self._bridge).reconcile(
                task_id, repair_missing=False, allow_sibling_cancel=allow_cancel,
            )

    def effective_mode(self) -> str:
        """The gate-decided mode right now: ``"live"`` or ``"dry"`` (FR5).

        Delegated to the composed bridge so orchestrators (the daily runner,
        the autoloop) see the same value the executor's own gate stack uses.
        """
        return self._bridge.effective_mode()

    def audit_plan(self, order: PlannedOrder, **kwargs: Any) -> None:
        """Public audit writer, shaped exactly like the bridge's (delegated).

        ``_handle_plan`` waits for approval through this surface; keeping the
        delegation here means callers never reach into ``executor._bridge``.
        """
        self._bridge.audit_plan(order, **kwargs)

    def _refuse(self, order: PlannedOrder, reason: str, *, phase: str) -> PermissionError:
        """Audit a gate refusal, then raise (fail-closed with a paper trail)."""
        self._bridge.audit_plan(order, action="derivatives_denied", reason=reason, phase=phase)
        return PermissionError(reason)


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None
