"""Portfolio: GET /api/portfolio (docs/ui-api-contract.md §6).

Realized P&L is a FIFO replay of the engine's audit log, reusing the risk
module's own helpers (``_replay`` / ``_consecutive_losses`` / ``_halted_today``
— the private seam §11.14 accepts). Positions come from
``bridge.sync_portfolio()`` in live mode; in dry mode the venue is never
touched — the engine's own rule is that dry never reaches the exchange — so
positions are reconstructed from the audit log's open FIFO lots (the real
fills it recorded), or an honest empty state when the log has none, with cash
and marks reported as unavailable. Live-mode venue failures answer 503
``exchange_unreachable``; an unreadable audit log answers 503
``audit_log_unreadable`` through ``audit.read_events`` — both fail-closed,
matching the execute-time stance.

Each live futures position also carries the cost of holding it: the entry
timestamp from the durable position state, the hours held, the funding paid or
received since that entry, and an estimated round-trip fee. Those are display
figures — a funding read that fails nulls only that field and adds a warning;
it never breaks the snapshot.
"""

import math
from datetime import UTC, datetime

from fastapi import APIRouter

from server import audit as audit_log, job_store
from server.contract import ApiError, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.futures_account import read_futures_account
from tradingagents.execution.risk import _consecutive_losses, _halted_today, _replay

router = APIRouter(prefix="/api", tags=["portfolio"])

STALE_AFTER = 30

_QTY_EPSILON = 1e-12  # mirrors risk.py's tolerance for float leftovers

# Position keys are "<ccxt_symbol>:<order side>:<entry order id>" (derivatives.py:577);
# the venue reports the position's own side, so the stored token needs mapping.
_ORDER_SIDE_TO_POSITION_SIDE = {"buy": "long", "sell": "short"}

_FUNDING_HISTORY_LIMIT = 1000

_UNAVAILABLE_COST = {
    "opened_at": None,
    "held_hours": None,
    "funding_usdt": None,
    "round_trip_fee_usdt": None,
}


def _finite_number(value) -> float | None:
    """A finite float or None; venue payloads and config values are untrusted."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_timestamp(value) -> datetime | None:
    """Parse a stored ISO-8601 timestamp as UTC; anything malformed is absent."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        # Python 3.10's fromisoformat does not accept the RFC 3339 suffix.
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def position_cost(notional, rate, income_rows, opened_at, now) -> dict:
    """Derive one position's holding cost; pure, so tests need no venue.

    ``income_rows`` are the venue's FUNDING_FEE entries already scoped to this
    position by the caller (``None`` when the read failed or the window is
    unknown), ``opened_at`` the raw stored timestamp and ``rate`` the
    configured per-side fee rate. Anything missing or malformed stays ``None``
    instead of becoming a guessed number: a null says "unknown", a zero would
    claim "free".
    """
    opened = _parse_timestamp(opened_at)

    held_hours = None
    if opened is not None and isinstance(now, datetime):
        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        # Clamped like the monitor's own age (futures_protection.py:170): a clock
        # skew must not render as a negative holding time.
        held_hours = max(
            0.0, (reference.astimezone(UTC) - opened).total_seconds() / 3600.0
        )

    round_trip_fee = None
    size = _finite_number(notional)
    fee_rate = _finite_number(rate)
    if size is not None and fee_rate is not None and fee_rate >= 0:
        round_trip_fee = abs(size) * fee_rate * 2  # one side at entry, one at exit

    funding = None
    if income_rows is not None:
        funding = 0.0
        for row in income_rows:
            if not isinstance(row, dict):
                continue
            amount = _finite_number(row.get("amount"))
            if amount is not None:
                funding += amount

    return {
        # The stored text is echoed back as-is; an unparseable value is not a date.
        "opened_at": opened_at.strip() if opened is not None else None,
        "held_hours": held_hours,
        "funding_usdt": funding,
        "round_trip_fee_usdt": round_trip_fee,
    }


def _active_states() -> list[dict]:
    """Durable position states, or none when the store is unavailable.

    The portfolio endpoint stays useful without a database: an unreadable
    state store only makes the holding time unknown.
    """
    try:
        return job_store.active_position_states()
    except Exception:
        return []


def _opened_at_for(states: list[dict], symbol: str | None, side: str | None) -> str | None:
    """The recorded entry timestamp for this venue position, newest state first.

    ``position_key`` is ``"<symbol>:<order side>:<entry order id>"``; the venue
    position carries no order id, so the key is matched on symbol + side and
    then re-read through ``load_position_state`` for its current revision. Only
    the newest matching state is consulted: an older position's timestamp must
    never be passed off as this one's.
    """
    if not symbol or not side:
        return None
    for state in reversed(states):
        key = state.get("position_key")
        if not isinstance(key, str):
            continue
        parts = key.rsplit(":", 2)
        if len(parts) != 3 or parts[0] != symbol:
            continue
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        state_side = str(snapshot.get("side") or _ORDER_SIDE_TO_POSITION_SIDE.get(parts[1], parts[1]))
        if state_side != side:
            continue
        stored = _load_state(key)
        if isinstance(stored, dict) and isinstance(stored.get("snapshot"), dict):
            snapshot = stored["snapshot"]
        opened = snapshot.get("opened_at")
        return opened.strip() if isinstance(opened, str) and opened.strip() else None
    return None


def _load_state(position_key: str) -> dict | None:
    """One durable state; a store hiccup only makes the timestamp unknown."""
    try:
        return job_store.load_position_state(position_key)
    except Exception:
        return None


def _funding_income_rows(bridge, symbol: str, since_ms: int, warnings: list[str]):
    """The symbol's FUNDING_FEE income rows from ``since_ms`` on, or None.

    A venue error only appends a warning: the caller keeps a null funding
    figure and the rest of the snapshot stays intact.
    """
    try:
        fetch = getattr(bridge.client(), "fetch_funding_history", None)
        if not callable(fetch):
            # The venue publishes no income history: nothing to show, nothing failed.
            return None
        rows = fetch(symbol, since_ms, _FUNDING_HISTORY_LIMIT, {})
    except Exception as exc:
        warnings.append(f"funding history unavailable for {symbol}: {type(exc).__name__}")
        return None
    return rows if isinstance(rows, list) else None


def _add_position_costs(futures: dict, bridge, config: dict, warnings: list[str]) -> None:
    """Attach held time, funding and round-trip fee to each futures position."""
    positions = futures.get("positions")
    if not isinstance(positions, list) or not positions:
        return
    now = datetime.now(UTC)
    rate = config.get("risk_derivatives_round_trip_fee_rate", 0.001)
    states = _active_states()
    for position in positions:
        if not isinstance(position, dict):
            continue
        position.update(_UNAVAILABLE_COST)
        symbol = position.get("symbol")
        try:
            opened_at = _opened_at_for(states, symbol, position.get("side"))
            opened = _parse_timestamp(opened_at)
            # The venue filters the income window itself; without a known entry
            # time there is no defensible window, so funding stays unknown
            # rather than summing an earlier position's rows on the same symbol.
            rows = (
                _funding_income_rows(bridge, symbol, int(opened.timestamp() * 1000), warnings)
                if opened is not None and isinstance(symbol, str) and symbol else None
            )
            position.update(position_cost(
                position.get("notional"), rate, rows, opened_at, now,
            ))
        except Exception as exc:  # display fields must never sink the snapshot
            warnings.append(
                f"position cost unavailable for {symbol or 'unknown'}: {type(exc).__name__}"
            )
            position.update(_UNAVAILABLE_COST)


def _build_bridge(config: dict):
    """The venue for live-mode reads; tests monkeypatch this seam to inject a
    fake exchange factory (same seam as approvals._build_bridge, bridge.py:126-129)."""
    from tradingagents.execution.bridge import ExchangeBridge

    return ExchangeBridge(config=config)


def _fifo_basis(lots) -> tuple[float | None, str | None]:
    """(weighted-average price, "fifo") over the ticker's still-open lots, or
    (None, None) when the log has none — a position opened before the log began
    has an unknown basis (risk.py:15-17). Short lots are negative; the average
    prices their gross size."""
    if not lots:
        return None, None
    gross = sum(abs(lot.quantity) for lot in lots)
    if gross <= _QTY_EPSILON:
        return None, None
    return sum(abs(lot.quantity) * lot.price for lot in lots) / gross, "fifo"


def _venue_position(bridge, position, open_lots, warnings) -> dict:
    try:
        marked = bridge.fetch_price(position.ticker)
    except Exception as exc:  # the venue served the balance but cannot mark: §6 wants 503
        raise ApiError(503, "exchange_unreachable",
                       f"venue cannot mark {position.ticker}: {exc}") from exc
    average, basis = _fifo_basis(open_lots.get(position.ticker))
    quantity = position.quantity
    if quantity is None:
        warnings.append(
            f"position {position.ticker} has unknown quantity; equity_est is unavailable"
        )
    elif marked is None:
        warnings.append(
            f"position {position.ticker} cannot be marked; it counts 0 in equity_est"
        )
    return {
        "ticker": position.ticker,
        "quantity": quantity,
        "average_price": average,
        "basis_source": basis,
        "marked_price": marked,
        "market_value": quantity * marked if quantity is not None and marked is not None else None,
        "unrealized_pnl": (marked - average) * quantity
        if quantity is not None and marked is not None and average is not None else None,
    }


def _audit_position(ticker: str, lots) -> dict:
    """A position reconstructed from open FIFO lots: real quantity and basis,
    but no venue mark in dry mode."""
    quantity = sum(lot.quantity for lot in lots)
    average, basis = _fifo_basis(lots)
    return {
        "ticker": ticker,
        "quantity": quantity,
        "average_price": average,
        "basis_source": basis,
        "marked_price": None,
        "market_value": None,
        "unrealized_pnl": None,
    }


@router.get("/portfolio")
def get_portfolio():
    config = get_config()
    events, skipped = audit_log.read_events()  # 503 when the log exists but cannot be read
    today = datetime.now(UTC).date()
    # This response's top-level portfolio is Spot-only. Futures positions and
    # account P&L come from the venue snapshot below, not the Spot FIFO replay.
    spot_events = [event for event in events if event.get("market", "spot") != "derivatives"]
    closed, open_lots = _replay(spot_events)

    warnings: list[str] = []
    if skipped:
        warnings.append(f"{skipped} unparseable audit lines were skipped")

    cash = None
    currency = str(config.get("exec_quote_currency") or "USDT").strip().upper() or None
    positions: list[dict] = []

    bridge = None
    live = False
    if config.get("exec_live") is True:  # only then can the env flag open the venue
        try:
            bridge = _build_bridge(config)
        except Exception as exc:
            raise ApiError(503, "exchange_unreachable", f"venue not available: {exc}") from exc
        live = bridge.effective_mode() == "live"

    if live:
        try:
            portfolio = bridge.sync_portfolio()
        except Exception as exc:
            raise ApiError(503, "exchange_unreachable",
                           f"venue cannot serve a balance: {exc}") from exc
        cash = portfolio.cash
        if portfolio.currency:
            currency = portfolio.currency
        for position in portfolio.positions:
            positions.append(_venue_position(bridge, position, open_lots, warnings))
    else:
        for ticker in sorted(open_lots):
            lots = open_lots[ticker]
            if abs(sum(lot.quantity for lot in lots)) <= _QTY_EPSILON:
                continue  # offsetting long/short leftovers net to flat
            positions.append(_audit_position(ticker, lots))
        if positions:
            warnings.append(
                "dry mode: positions are reconstructed from open FIFO lots in the "
                "audit log; cash and market marks are unavailable"
            )

    # Unknown quantities leave equity unavailable; known but unmarked positions
    # retain the partial estimate reported in the warnings above.
    equity: float | None = None
    if (cash is not None or positions) and all(p["quantity"] is not None for p in positions):
        equity = float(cash or 0.0)
        for position in positions:
            if position["market_value"] is not None:
                equity += position["market_value"]

    by_ticker: dict[str, float] = {}
    trade_tickers = sorted({event["ticker"] for event in spot_events
                            if event.get("action") in ("buy", "sell")
                            and isinstance(event.get("ticker"), str)})
    for ticker in trade_tickers:
        ticker_closed, _ = _replay([e for e in spot_events if e.get("ticker") == ticker])
        if ticker_closed:  # only tickers with actual closes appear (contract §6)
            by_ticker[ticker] = float(sum(t.pnl for t in ticker_closed if t.pnl is not None))

    total = float(sum(t.pnl for t in closed if t.pnl is not None))
    today_total = float(sum(t.pnl for t in closed if t.pnl is not None and t.day == today))

    futures = {
        "equity": None,
        "wallet_balance": None,
        "available_balance": None,
        "unrealized_pnl": None,
        "venue_profile": None,
        "equity_currency": None,
        "wallet_balance_currency": None,
        "available_balance_currency": None,
        "unrealized_pnl_currency": None,
        "account_scope": "derivatives",
        "positions": [],
        "status": "unavailable",
        "error": None,
    }
    if live:
        try:
            futures = read_futures_account(bridge).as_dict()
        except Exception as exc:
            futures["status"] = "error"
            futures["error"] = f"futures account read failed ({type(exc).__name__})"
            warnings.append(futures["error"])
        else:
            try:
                _add_position_costs(futures, bridge, config, warnings)
            except Exception as exc:
                warnings.append(f"futures position cost unavailable ({type(exc).__name__})")

    return {
        "generated_at": utc_now_iso(),
        "currency": currency,
        "cash": cash,
        "equity_est": equity,
        "positions": positions,
        "realized_pnl": {
            "total": total,
            "today": today_total,
            "by_ticker": by_ticker,
            "closed_trades": sum(1 for t in closed if t.pnl is not None),
            "unknown_basis_closes": sum(1 for t in closed if t.pnl is None),
            "consecutive_losses": _consecutive_losses(
                closed, int(config.get("risk_max_consecutive_loss_count") or 3)
            ),
        },
        "halted_today": _halted_today(events, today),
        "warnings": warnings,
        "stale_after": STALE_AFTER,
        "futures": futures,
    }
