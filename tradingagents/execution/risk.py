"""RiskGuard: pre-trade limits evaluated from the execution audit log (FR-K3/K4).

Every decision is grounded in two sources: the append-only audit JSONL (what
was planned/executed, written by bridge.py and derivatives.py) and the
caller's PortfolioContext plus an injectable ``price_of`` for marking.

Interpretation pinned here, because the L1 audit carries no order ids that
match a buy to its sell:

- P&L matching is FIFO per pipeline ticker over ``price_est``: a sell
  consumes the oldest still-open long lots of the same ticker, across days.
  A derivatives sell beyond any long lots OPENS a short (a negative lot,
  recognized by the audit line's ``market="derivatives"``); a later buy
  covers open shorts first at entry-minus-exit P&L, the mirror of a long's.
  A closing trade with no open lot (a position bought before logging began)
  has an unknown basis: it is excluded from the daily P&L and skipped --
  neither counted nor streak-breaking -- by the consecutive-loss walk.
- Realized trades classify the consecutive-loss streak backwards from the
  newest event across all days: a losing sell extends it, a profitable sell
  ends it, an unknown-basis sell is skipped. ``max_consecutive_loss_count``
  is absolute -- it never needs an equity denominator, so it keeps working
  on days the percentage checks cannot.
- Halts are day-scoped and sticky: once a ``{"event": "halt"}`` line exists
  for the current UTC day without a following ``{"event": "halt_reset"}``,
  every check returns halted until ``reset_halt()`` is called or the UTC day
  rolls over (the halt self-expires). Breaching either the consecutive-loss
  cap or the daily-loss cap appends the halt line itself, once per day. Two
  events forgive everything before them -- a halt line (that breach already
  fired the kill switch once, so a day rollover starts a fresh window) and
  a halt_reset (the operator's explicit decision for the day): breach
  conditions are re-evaluated only over events AFTER the newest of those,
  so a forgiven breach cannot re-fire the halt -- while a new breach after
  the forgiveness point still can. One accepted tradeoff: a lot opened
  BEFORE the forgiveness point drops out of the daily-loss figure until it
  is closed (its close lands after the point and counts then); an ongoing
  unrealized loss on that lot can therefore drift past the cap for the
  rest of the UTC day without re-firing the halt.
- Equity is ``cash + sum(position quantity x current price)`` from the
  PortfolioContext. When it cannot be computed (no portfolio, or cash None
  with no positions), the percentage checks are skipped fail-open with the
  reason recorded on the decision -- the halt machinery above still fires.
  A book that is present but not usable is the opposite case and fails
  closed: non-finite (inf/NaN) cash, quantity, mark price or notional --
  whether from a schema bypass (``model_copy``) or a hand-built context --
  refuses the order, because an infinite denominator would drive every
  percentage cap toward zero. Likewise, an existing position whose exposure
  cannot be valued in the book's currency (no mark, a non-positive or
  non-finite value, or an unconvertible currency) refuses capped orders:
  unknown exposure is never counted as zero or dropped from the totals.

Fail-open, deliberately: a ``price_of`` that raises or returns None is
treated as "no price" for equity and unrealized P&L, and that value is
skipped and logged (an understated denominator only tightens the checks);
the exposure caps above still refuse rather than drop the position. Only the
consecutive-loss cap is immune to missing data. One deliberate exception to
the fail-open stance: the execute-time re-check (``check(...,
fail_closed=True)``, called wherever a real order is about to be sent)
reads the log FAIL-CLOSED -- an unreadable audit log hides the halt and
loss-streak state exactly when a send is being decided, so the caller must
refuse ("audit log unreadable; failing closed") instead of passing.
Plan-time checks keep the fail-open read: a blind plan must not fabricate
a halt either way.
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.execution.audit import append_event
from tradingagents.portfolio import PortfolioContext

logger = logging.getLogger(__name__)

_QTY_EPSILON = 1e-12


def _finite_number(raw: Any) -> bool:
    """True when ``raw`` converts to a finite float (rejects inf/NaN/garbage)."""
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(value)


def _non_finite_account_fields(portfolio: PortfolioContext) -> list[str]:
    """Book fields the percentage math cannot use, as ``ticker.field`` labels."""
    bad: list[str] = []
    if portfolio.cash is not None and not _finite_number(portfolio.cash):
        bad.append("cash")
    for position in portfolio.positions:
        ticker = str(getattr(position, "ticker", "") or "") or "?"
        for name in ("quantity", "mark_price", "notional"):
            raw = getattr(position, name, None)
            if raw is not None and not _finite_number(raw):
                bad.append(f"{ticker}.{name}")
    return bad


class RiskLimits(BaseModel):
    """The six FR-K limits; defaults mirror DEFAULT_CONFIG's risk_* keys."""

    model_config = ConfigDict(validate_assignment=True, allow_inf_nan=False)
    max_daily_loss_pct: float = Field(default=5.0, ge=0)
    max_position_pct_per_asset: float = Field(default=25.0, ge=0)
    max_total_exposure_pct: float = Field(default=80.0, ge=0)
    max_consecutive_loss_count: int = Field(default=3, ge=1)
    max_derivatives_leverage: float = Field(default=1.0, ge=0)
    max_derivatives_exposure_pct: float = Field(default=0.0, ge=0)


class RiskDecision(BaseModel):
    """Outcome of one guard check. ``halted`` implies ``allowed`` False."""

    allowed: bool
    halted: bool
    reason: str | None = None


def _utc_now() -> datetime:
    """UTC clock used for both day scoping and written timestamps.

    Single seam on purpose: tests freeze the day by monkeypatching this one
    function, so halt lines the guard appends carry the frozen day too.
    """
    return datetime.now(UTC)


def _utc_today() -> date:
    return _utc_now().date()


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _event_date(event: dict[str, Any]) -> date | None:
    """The UTC date of an audit line, or None when it has no usable timestamp."""
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _read_events(
    path: str | Path, *, fail_closed: bool = False
) -> list[dict[str, Any]] | None:
    """Every parseable JSON line in file order (append-only => chronological).

    A missing file is a fresh log. An unreadable log (permission, wrong
    parent type) reads as empty fail-open -- the write side fails loudly
    anyway, and a blind guard must not fabricate a halt either way. Corrupt
    lines are skipped with a warning.

    ``fail_closed=True`` (execute-time re-checks) returns None instead when
    the log EXISTS but cannot be read: the halt and loss-streak state would
    be invisible to a send decision, so the caller must refuse. A missing
    file is still a fresh log and never returns None.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        if fail_closed:
            logger.warning("risk: cannot read audit log %s (%r); failing closed", target, exc)
            return None
        logger.warning("risk: cannot read audit log %s (%r); treating as empty", target, exc)
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("risk: skipping unparseable audit line in %s", target)
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _halted_today(events: list[dict[str, Any]], today: date) -> bool:
    """Sticky halt: a halt line today not yet followed by a halt_reset today."""
    halted = False
    for event in events:
        kind = event.get("event")
        if kind not in ("halt", "halt_reset"):
            continue
        if _event_date(event) != today:
            continue  # day-scoped: a halt from a previous day has expired
        halted = kind == "halt"
    return halted


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    """The full UTC timestamp of an audit line, or None when unusable."""
    raw = event.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _considered_from(events: list[dict[str, Any]], today: date) -> datetime | None:
    """The moment after which breach conditions are (re)evaluated, or None.

    Two events forgive everything before them: a ``halt`` line (everything
    up to it already fired the kill switch once, so a day rollover starts a
    fresh window instead of re-halting from history) and today's
    ``halt_reset`` (the operator's explicit decision for the day). The
    newest of these wins; without one the whole history is in play.
    """
    moment: datetime | None = None
    for event in events:
        kind = event.get("event")
        is_halt = kind == "halt"
        is_today_reset = kind == "halt_reset" and _event_date(event) == today
        if not (is_halt or is_today_reset):
            continue
        when = _event_datetime(event)
        if when is not None and (moment is None or when > moment):
            moment = when
    return moment


def _after_reset(when: datetime | None, considered_from: datetime | None) -> bool:
    """True when ``when`` falls after the forgiveness point (not forgiven).

    Events without a comparable timestamp count (never forgiven) -- the
    conservative direction for a kill switch.
    """
    if considered_from is None or when is None:
        return True
    try:
        return when > considered_from
    except TypeError:  # naive vs aware timestamps: do not forgive
        return True


class _Lot:
    """One open lot awaiting a FIFO match; negative quantity = a short."""

    __slots__ = ("quantity", "price", "opened", "opened_when")

    def __init__(
        self, quantity: float, price: float, opened: date, opened_when: datetime
    ) -> None:
        self.quantity = quantity
        self.price = price
        self.opened = opened
        self.opened_when = opened_when


class _ClosedTrade:
    """One FIFO-matched close: realized P&L, or None when the basis is unknown."""

    __slots__ = ("day", "pnl", "when")

    def __init__(self, day: date, pnl: float | None, when: datetime) -> None:
        self.day = day
        self.pnl = pnl
        self.when = when


def _replay(events: list[dict[str, Any]]) -> tuple[list[_ClosedTrade], dict[str, deque[_Lot]]]:
    """Walk the whole log chronologically, matching trades FIFO per ticker.

    Lines marked ``phase="plan"`` are skipped: a plan that later executed
    must not count twice. Phase-less lines follow the L1 upgrade rule: the
    pre-phase writer logged PLANNED orders as action=side with
    ``confirmed=False`` (the matching execute line carried confirmed=True),
    so a phase-less confirmed=False line is a PLAN and is skipped, while a
    phase-less confirmed=True line -- and hand-written lines with no
    confirmed field at all -- still count as executed history. Matching is
    net-position FIFO: a buy first covers open SHORT lots (only derivatives
    sells create those, so spot-only logs behave exactly as before), then
    opens a long lot; a sell first closes long lots, and a derivatives
    sell's leftover opens a short lot whose realized P&L is entry minus
    exit. Returns every closed trade (with realized P&L, None if the close
    had no open lot to match) and the still-open lots per ticker, each
    carrying the moment it was opened so unrealized P&L can be scoped to
    today and to the post-reset window (the sign of a short lot's quantity
    makes the same arithmetic mark shorts correctly).
    """
    closed: list[_ClosedTrade] = []
    lots: dict[str, deque[_Lot]] = {}
    for event in events:
        phase = event.get("phase")
        if phase == "plan":
            continue
        if phase is None and event.get("confirmed") is False:
            # L1 upgrade rule: a phase-less confirmed=False buy/sell line is
            # a PLAN, never a fill -- counting it fabricates P&L and halts
            # from orders that may never have been sent. Accepted cost: L1
            # dry fills (dry needs no confirm) are skipped too, so their
            # simulated rehearsal history does not feed the streak; live L1
            # fills always carry confirmed=True and still count.
            continue
        action = event.get("action")
        if action not in ("buy", "sell"):
            continue
        when = _event_datetime(event)
        quantity = event.get("qty")
        price = event.get("price_est")
        ticker = event.get("ticker")
        if when is None or not isinstance(ticker, str) or not ticker.strip():
            continue
        if not isinstance(quantity, (int, float)) or not isinstance(price, (int, float)):
            continue
        quantity = float(quantity)
        price = float(price)
        if not math.isfinite(quantity) or not math.isfinite(price):
            # A non-finite line is as unusable as a corrupt one: NaN compares
            # false everywhere, so carrying it into the FIFO P&L would make
            # the daily-loss cap silently skip while still looking healthy.
            logger.warning("risk: skipping audit line with non-finite qty/price")
            continue
        if quantity <= 0 or price <= 0:
            continue
        day = when.date()
        queue = lots.setdefault(ticker, deque())
        if action == "buy":
            # Cover open shorts first (front of the queue): a short's P&L is
            # entry minus exit, the mirror of a long's. Short lots only exist
            # below derivatives sells, so spot-only logs never enter this loop.
            remaining = quantity
            pnl = 0.0
            matched_any = False
            while remaining > _QTY_EPSILON and queue and queue[0].quantity < 0:
                lot = queue[0]
                matched = min(remaining, -lot.quantity)
                pnl += (lot.price - price) * matched
                lot.quantity += matched
                remaining -= matched
                matched_any = True
                if -lot.quantity <= _QTY_EPSILON:
                    queue.popleft()
            if matched_any:
                closed.append(_ClosedTrade(day, pnl, when))
            if remaining > _QTY_EPSILON:
                queue.append(_Lot(remaining, price, day, when))
            continue
        # sell: close the oldest long lots first; leftover has an unknown
        # basis on spot, while a derivatives sell's leftover OPENS a short
        # (a negative lot) so a later buy matches its close (fixes shorts
        # being invisible to the daily-loss and streak checks).
        remaining = quantity
        pnl = 0.0
        matched_any = False
        while remaining > _QTY_EPSILON and queue and queue[0].quantity > 0:
            lot = queue[0]
            matched = min(remaining, lot.quantity)
            pnl += (price - lot.price) * matched
            lot.quantity -= matched
            remaining -= matched
            matched_any = True
            if lot.quantity <= _QTY_EPSILON:
                queue.popleft()
        if event.get("market") == "derivatives" and remaining > _QTY_EPSILON:
            queue.append(_Lot(-remaining, price, day, when))
        closed.append(_ClosedTrade(day, pnl if matched_any else None, when))
    return closed, lots


def _consecutive_losses(closed: list[_ClosedTrade], limit: int) -> int:
    """Losing sells in a row, walking backwards from the newest across days.

    A profitable sell ends the walk; an unknown-basis sell (no open lot) is
    skipped without breaking or extending the streak.
    """
    streak = 0
    for trade in reversed(closed):
        if trade.pnl is None:
            continue
        if trade.pnl > 0:
            break
        if trade.pnl < 0:
            streak += 1
            if streak >= limit:
                break
        # pnl == 0: neither a loss nor a win; keep walking
    return streak


class RiskGuard:
    """Evaluates one order plan against the FR-K limits before it is sent."""

    def __init__(
        self,
        limits: RiskLimits | None = None,
        price_of: Callable[[str], float | None] | None = None,
    ) -> None:
        self.limits = limits if limits is not None else RiskLimits()
        self.price_of = price_of if price_of is not None else (lambda ticker: None)

    def _price(self, ticker: str) -> float | None:
        try:
            price = float(self.price_of(ticker))
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning("risk: price_of(%r) failed: %r", ticker, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - a marking failure is "no price"
            logger.warning("risk: price_of(%r) failed: %r", ticker, exc)
            return None
        return price if math.isfinite(price) and price > 0 else None

    def _position_exposure(self, position: Any, currency: str | None = None) -> float | None:
        """Return gross exposure in account currency; ``None`` means unknown.

        Unknown exposure must fail closed: the caller refuses the order
        instead of counting the position as zero or dropping it from the
        totals. A spot position without a ``price_currency`` is priced in the
        book's declared currency (``PortfolioContext.currency`` labels cash
        and prices); a declared mismatching quotation is converted through
        the mark only when the pair is base<->quote, otherwise it stays
        unknown.
        """
        def currency_key(value: Any) -> str | None:
            normalized = str(value or "").strip().upper()
            return normalized or None

        def finite_positive(raw: Any) -> float | None:
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            return value if math.isfinite(value) and value > 0 else None

        target = currency_key(currency)
        base = currency_key(getattr(position, "base_currency", None))
        quote = currency_key(getattr(position, "price_currency", None))

        def convert(value: float, source: str | None, rate: float | None) -> float | None:
            """Translate ``value`` from ``source`` into the account currency."""
            if target is None or source is None:
                return None
            if source == target:
                return value
            if rate is None:
                return None
            if source == base and target == quote and base and quote:
                converted = value * rate
            elif source == quote and target == base and base and quote:
                converted = value / rate
            else:
                return None
            return finite_positive(converted)

        market = getattr(position, "market", "spot")
        mark = finite_positive(getattr(position, "mark_price", None))
        notional_raw = getattr(position, "notional", None)
        if market == "derivatives" and notional_raw is not None:
            notional = finite_positive(notional_raw)
            if notional is None:
                return None
            return convert(
                notional,
                currency_key(getattr(position, "notional_currency", None)),
                mark,
            )
        if mark is None:
            mark = finite_positive(self._price(getattr(position, "ticker", "")))
        try:
            quantity = float(getattr(position, "quantity", None))
        except (TypeError, ValueError, OverflowError):
            return None
        if mark is None or not math.isfinite(quantity):
            return None
        value = abs(quantity) * mark
        if not math.isfinite(value) or value <= 0:
            return None
        if market == "derivatives":
            return convert(value, quote, mark)
        # Spot: an undeclared quote follows the book convention; a declared
        # one must match the account currency or be convertible base<->quote.
        return value if quote is None else convert(value, quote, mark)

    def _equity(self, portfolio: PortfolioContext | None) -> float | None:
        """Return cash/equity plus markable spot holdings when computable.

        Futures autoloop books already carry account equity as cash. Their
        positions are included in exposure checks, but not added to this
        denominator a second time.
        """
        if portfolio is None:
            return None
        if portfolio.cash is None and not portfolio.positions:
            return None
        equity = float(portfolio.cash or 0.0)
        for position in portfolio.positions:
            if getattr(position, "market", "spot") == "derivatives":
                continue
            ticker = str(getattr(position, "ticker", "") or "")
            quantity = getattr(position, "quantity", None)
            price = self._price(ticker)
            if price is None:
                # Counting an unmarkable spot position as 0 understates equity,
                # which only makes the percentage checks stricter. The exposure
                # caps separately refuse such an order instead of dropping it.
                logger.warning(
                    "risk: cannot mark position %r (%s); counting it as 0 in equity",
                    ticker,
                    quantity,
                )
                continue
            try:
                contribution = float(quantity) * price
            except (TypeError, ValueError, OverflowError):
                contribution = None
            if contribution is None or not math.isfinite(contribution):
                # quantity is nullable (derivatives share the Position schema);
                # a None or overflowing unit count must never raise here --
                # the exposure caps refuse such an order instead.
                logger.warning(
                    "risk: position %r has unusable quantity %s; counting it as 0 in equity",
                    ticker,
                    quantity,
                )
                continue
            equity += contribution
        return equity

    def _daily_pnl(
        self,
        closed: list[_ClosedTrade],
        open_lots: dict[str, deque[_Lot]],
        today: date,
        considered_from: datetime | None = None,
    ) -> float:
        """Today's realized P&L plus unrealized on lots opened today, marked now.

        Trades and lots at or before the forgiveness point (the newest halt
        line, or today's halt_reset) are already-forgiven history: they stay
        out of the figure so a forgiven breach cannot re-fire the halt.
        """
        pnl = 0.0
        for trade in closed:
            if (
                trade.day == today
                and trade.pnl is not None
                and _after_reset(trade.when, considered_from)
            ):
                pnl += trade.pnl
        for ticker, queue in open_lots.items():
            current = self._price(ticker)
            if current is None:
                for lot in queue:
                    if lot.opened == today and _after_reset(lot.opened_when, considered_from):
                        logger.warning(
                            "risk: cannot mark open lot %r (%s bought at %s); "
                            "its unrealized P&L is excluded from the daily figure",
                            ticker,
                            lot.quantity,
                            lot.price,
                        )
                continue
            for lot in queue:
                if lot.opened == today and _after_reset(lot.opened_when, considered_from):
                    pnl += (current - lot.price) * lot.quantity
        return pnl

    def check(
        self,
        order_plan: Any,
        portfolio: PortfolioContext | None,
        audit_log: str | Path,
        *,
        fail_closed: bool = False,
    ) -> RiskDecision:
        """Evaluate ``order_plan`` against the limits; never raises for risk.

        Halted always wins: the audit log is the shared, cross-process state,
        so a halt appended by any process blocks every check until reset or
        day rollover. Percentage checks run only when equity is computable;
        the consecutive-loss cap always runs. A non-finite account state
        (cash, position quantity/mark price/notional) refuses fail-closed --
        an infinite denominator would silently disarm every percentage cap.
        ``fail_closed=True`` marks an
        execute-time re-check (a send is being decided): an audit log that
        exists but cannot be read then REFUSES the order instead of passing,
        because the halt and streak state is unverifiable. Plan-time checks
        keep the documented fail-open read.
        """
        # model_copy can bypass Pydantic validation; revalidate at the send
        # boundary and keep the coerced instance, so the comparisons below
        # never mix floats with the raw bypassed values.
        try:
            self.limits = RiskLimits.model_validate(vars(self.limits))
        except (AttributeError, ValidationError, TypeError, ValueError) as exc:
            return RiskDecision(
                allowed=False, halted=False, reason=f"invalid risk limits: {exc}",
            )
        path = Path(audit_log).expanduser()
        events = _read_events(path, fail_closed=fail_closed)
        if events is None:
            return RiskDecision(
                allowed=False,
                halted=False,
                reason="audit log unreadable; failing closed",
            )
        today = _utc_today()
        if _halted_today(events, today):
            return RiskDecision(
                allowed=False,
                halted=True,
                reason="risk halt is active for today (audit log); call reset_halt to clear",
            )

        closed, open_lots = _replay(events)
        # A halt line (the breach already fired once) and a halt_reset today
        # (the operator's decision) both forgive what came before them:
        # breach conditions are re-evaluated only over events after that
        # point, so a forgiven breach cannot re-fire the halt -- a new
        # breach after it still can.
        considered_from = _considered_from(events, today)
        considered = [t for t in closed if _after_reset(t.when, considered_from)]
        streak = _consecutive_losses(considered, self.limits.max_consecutive_loss_count)
        if streak >= self.limits.max_consecutive_loss_count:
            reason = (
                f"{streak} consecutive losing trades reached the limit "
                f"{self.limits.max_consecutive_loss_count}; trading halted for today"
            )
            self._append_halt(path, reason)
            return RiskDecision(allowed=False, halted=True, reason=reason)

        # Validate the candidate before marking the book or calculating any
        # percentage exposure. A bad value must never reach fail-open handling.
        reasons: list[str] = []
        side = getattr(order_plan, "side", None)
        is_derivatives = getattr(order_plan, "market", "spot") == "derivatives"
        cost = getattr(order_plan, "cost", None)
        quantity = getattr(order_plan, "quantity", None)
        leverage_raw = getattr(order_plan, "leverage", 1.0)
        supplied_notional = getattr(order_plan, "notional", None)
        leverage = 1.0
        notional = None

        if side is not None:
            invalid_fields: list[str] = []
            for name, raw, required in (
                ("cost", cost, side == "buy" or is_derivatives),
                ("quantity", quantity, True),
                ("leverage", leverage_raw, True),
                ("notional", supplied_notional, False),
            ):
                if raw is None and not required:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError, OverflowError):
                    invalid_fields.append(name)
                    continue
                if not math.isfinite(value) or value <= 0:
                    invalid_fields.append(name)
                    continue
                if name == "leverage":
                    leverage = value
            if invalid_fields:
                return RiskDecision(
                    allowed=False,
                    halted=False,
                    reason=(
                        "invalid order terms: "
                        + ", ".join(invalid_fields)
                        + " must be finite and positive"
                    ),
                )
            if side == "buy" or is_derivatives:
                notional = float(cost) * leverage
                if not math.isfinite(notional) or notional <= 0:
                    return RiskDecision(
                        allowed=False,
                        halted=False,
                        reason="invalid order terms: notional must be finite and positive",
                    )
                try:
                    price = float(getattr(order_plan, "estimated_price", None))
                    quantity_value = float(quantity)
                except (TypeError, ValueError, OverflowError):
                    return RiskDecision(
                        allowed=False,
                        halted=False,
                        reason="invalid order terms: estimated_price and quantity are required",
                    )
                if not math.isfinite(price) or price <= 0:
                    return RiskDecision(
                        allowed=False,
                        halted=False,
                        reason="invalid order terms: estimated_price must be finite and positive",
                    )
                actual_notional = quantity_value * price
                if not math.isfinite(actual_notional) or actual_notional <= 0:
                    return RiskDecision(
                        allowed=False,
                        halted=False,
                        reason="invalid order terms: actual notional must be finite and positive",
                    )
                if not math.isclose(actual_notional, notional, rel_tol=1e-9, abs_tol=0.0):
                    return RiskDecision(
                        allowed=False,
                        halted=False,
                        reason=(
                            "inconsistent order terms: quantity * estimated_price "
                            f"({actual_notional:g}) does not match cost * leverage ({notional:g})"
                        ),
                    )
                notional = max(notional, actual_notional)

        # The derivatives leverage cap needs no denominator, so it is
        # evaluated BEFORE the equity-None fail-open branch: a 100x plan is
        # refused even when equity cannot be computed.
        if is_derivatives and side is not None and leverage > self.limits.max_derivatives_leverage:
            reasons.append(
                f"leverage {leverage:g} exceeds the derivatives limit "
                f"{self.limits.max_derivatives_leverage:g}"
            )

        # A book that is present but not usable (schema validation bypassed
        # via model_copy, or a hand-built context) must refuse, not fail open:
        # an infinite or NaN denominator drives every percentage cap toward
        # zero and silently disarms them.
        if portfolio is not None:
            bad_fields = _non_finite_account_fields(portfolio)
            if bad_fields:
                return RiskDecision(
                    allowed=False,
                    halted=False,
                    reason=(
                        "non-finite account state ("
                        + ", ".join(bad_fields)
                        + "); failing closed"
                    ),
                )
            unknown_spot_quantity = next(
                (
                    position for position in portfolio.positions
                    if getattr(position, "market", "spot") == "spot"
                    and getattr(position, "quantity", None) is None
                ),
                None,
            )
            if unknown_spot_quantity is not None:
                ticker = str(getattr(unknown_spot_quantity, "ticker", "") or "?")
                return RiskDecision(
                    allowed=False,
                    halted=False,
                    reason=f"existing spot position {ticker} has unknown quantity; failing closed",
                )

        # One open derivatives position per symbol: the entry path must
        # reconfigure margin mode and leverage before sending, and the venue
        # refuses both while a position (or its protective legs) is open
        # (Binance demo answers -4067/-4161 there). An entry on a held symbol
        # is a deliberate "no" at plan and execute stage, not a venue crash.
        if is_derivatives and side is not None and portfolio is not None:
            entry_ticker = str(getattr(order_plan, "ticker", "") or "").strip().upper()
            held_symbol = next(
                (
                    position
                    for position in portfolio.positions
                    if getattr(position, "market", "spot") == "derivatives"
                    and (
                        str(getattr(position, "ticker", "") or "").strip().upper() == entry_ticker
                        or (
                            getattr(position, "market_symbol", None) is not None
                            and str(getattr(position, "market_symbol", "")).strip().upper() == entry_ticker
                        )
                    )
                ),
                None,
            )
            if held_symbol is not None:
                reasons.append(
                    f"symbol {entry_ticker or '?'} already has an open derivatives "
                    "position; a new entry on a non-flat symbol is refused "
                    "(margin mode and leverage cannot be reconfigured while the "
                    "venue holds a position)"
                )

        equity = self._equity(portfolio)
        if equity is not None and not math.isfinite(equity):
            return RiskDecision(
                allowed=False,
                halted=False,
                reason="account equity is not finite; failing closed",
            )

        if equity is None:
            if reasons:
                return RiskDecision(allowed=False, halted=False, reason="; ".join(reasons))
            skipped = (
                "equity not computable (no portfolio, or cash None with no positions): "
                "percentage limits skipped fail-open"
            )
            logger.warning("risk: %s", skipped)
            return RiskDecision(allowed=True, halted=False, reason=skipped)

        # Daily-loss cap (FR-K4): a breach halts the rest of the UTC day.
        pnl = self._daily_pnl(closed, open_lots, today, considered_from)
        if not math.isfinite(pnl):
            # Finite inputs can still overflow in the P&L arithmetic. NaN
            # compares false to everything, so letting it through would skip
            # the loss cap entirely; refuse instead.
            return RiskDecision(
                allowed=False,
                halted=False,
                reason="daily P&L is not finite; failing closed",
            )
        if pnl < 0:
            loss_pct = (-pnl / equity) * 100 if equity > 0 else float("inf")
            if loss_pct > self.limits.max_daily_loss_pct:
                reason = (
                    f"daily loss {loss_pct:.2f}% exceeds the limit "
                    f"{self.limits.max_daily_loss_pct:.2f}% of equity {equity:.2f}; "
                    "trading halted for today"
                )
                self._append_halt(path, reason)
                return RiskDecision(allowed=False, halted=True, reason=reason)

        # Position/exposure caps: buys always; derivatives sells too (a short
        # adds real exposure). Spot sells reduce exposure and stay uncapped.
        if (side == "buy" or is_derivatives) and notional is not None:
            ticker = str(getattr(order_plan, "ticker", "") or "")
            ticker_key = ticker.strip().upper()
            current_asset = 0.0
            total_exposure = 0.0
            derivatives_exposure = 0.0
            for position in (portfolio.positions if portfolio is not None else []):
                position_ticker = str(getattr(position, "ticker", "") or "")
                market_symbol = getattr(position, "market_symbol", None)
                same_asset = position_ticker.strip().upper() == ticker_key or (
                    market_symbol is not None
                    and str(market_symbol).strip().upper() == ticker_key
                )
                exposure = self._position_exposure(position, portfolio.currency)
                if exposure is None:
                    # An unvalued position is unknown exposure even when it is
                    # neither this order's asset nor derivatives: spot books
                    # from sync_portfolio carry no mark price, and silently
                    # dropping it understates the totals.
                    reasons.append(
                        f"existing position {position_ticker or '?'} has unknown "
                        "or unconvertible exposure"
                    )
                    continue
                if same_asset:
                    current_asset += exposure
                total_exposure += exposure
                if getattr(position, "market", "spot") == "derivatives":
                    derivatives_exposure += exposure

            per_asset = notional if is_derivatives else float(cost)
            position_pct = ((current_asset + per_asset) / equity) * 100 if equity > 0 else float("inf")
            if position_pct > self.limits.max_position_pct_per_asset:
                reasons.append(
                    f"position {ticker} would reach {position_pct:.2f}% of equity, "
                    f"above the per-asset limit {self.limits.max_position_pct_per_asset:.2f}%"
                )
            exposure_pct = ((total_exposure + notional) / equity) * 100 if equity > 0 else float("inf")
            if exposure_pct > self.limits.max_total_exposure_pct:
                reasons.append(
                    f"total exposure would reach {exposure_pct:.2f}% of equity, "
                    f"above the limit {self.limits.max_total_exposure_pct:.2f}%"
                )
            if is_derivatives:
                deriv_pct = ((derivatives_exposure + notional) / equity) * 100 if equity > 0 else float("inf")
                if deriv_pct > self.limits.max_derivatives_exposure_pct:
                    reasons.append(
                        f"derivatives notional would reach {deriv_pct:.2f}% of equity, "
                        f"above the limit {self.limits.max_derivatives_exposure_pct:.2f}%"
                    )

        if reasons:
            return RiskDecision(allowed=False, halted=False, reason="; ".join(reasons))
        return RiskDecision(allowed=True, halted=False, reason=None)

    def _append_halt(self, path: Path, reason: str) -> None:
        """Append the halt line once the breach is detected (sticky for today)."""
        append_event(path, {"event": "halt", "timestamp": _utc_now_iso(), "reason": reason})

    def reset_halt(self, audit_log: str | Path) -> None:
        """Clear today's halt by appending a halt_reset line (audit stays append-only)."""
        append_event(
            Path(audit_log).expanduser(),
            {"event": "halt_reset", "timestamp": _utc_now_iso()},
        )
