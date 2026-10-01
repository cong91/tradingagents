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
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from server import audit as audit_log
from server.contract import ApiError, utc_now_iso
from tradingagents.dataflows.config import get_config
from tradingagents.execution.risk import _consecutive_losses, _halted_today, _replay

router = APIRouter(prefix="/api", tags=["portfolio"])

STALE_AFTER = 30

_QTY_EPSILON = 1e-12  # mirrors risk.py's tolerance for float leftovers


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
    if marked is None:
        warnings.append(
            f"position {position.ticker} cannot be marked; it counts 0 in equity_est"
        )
    return {
        "ticker": position.ticker,
        "quantity": position.quantity,
        "average_price": average,
        "basis_source": basis,
        "marked_price": marked,
        "market_value": position.quantity * marked if marked is not None else None,
        "unrealized_pnl": (marked - average) * position.quantity
        if marked is not None and average is not None else None,
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
    today = datetime.now(timezone.utc).date()
    closed, open_lots = _replay(events)

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

    # equity_est mirrors RiskGuard._equity (risk.py:381-403): None only when cash
    # is unknown AND there are no positions; unmarked positions count 0.
    equity: float | None = None
    if cash is not None or positions:
        equity = float(cash or 0.0)
        for position in positions:
            if position["marked_price"] is not None:
                equity += position["quantity"] * position["marked_price"]

    by_ticker: dict[str, float] = {}
    trade_tickers = sorted({event["ticker"] for event in events
                            if event.get("action") in ("buy", "sell")
                            and isinstance(event.get("ticker"), str)})
    for ticker in trade_tickers:
        ticker_closed, _ = _replay([e for e in events if e.get("ticker") == ticker])
        if ticker_closed:  # only tickers with actual closes appear (contract §6)
            by_ticker[ticker] = float(sum(t.pnl for t in ticker_closed if t.pnl is not None))

    total = float(sum(t.pnl for t in closed if t.pnl is not None))
    today_total = float(sum(t.pnl for t in closed if t.pnl is not None and t.day == today))

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
    }
