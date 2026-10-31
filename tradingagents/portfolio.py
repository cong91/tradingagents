"""The caller's book, as the decision agents see it.

Optional input to a run: what is held, at what average price, and how much cash
is free. Without it the agents cannot tell adding to a full position from
opening a new one. Three states are distinct and must stay so: a position, a
flat book, and no context at all, since treating "not provided" as "flat" would
invent a fact about the caller's account.

Broker-neutral by construction: quantities are generic units and the currency is
whatever label the caller passes, so nothing here implies a venue or an
execution path.

Numeric book fields reject inf/NaN (`allow_inf_nan=False`): a non-finite book
must fail validation at the boundary instead of silently disarming
RiskGuard's percentage caps.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradingagents.dataflows.symbols import normalize_symbol


class Position(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    ticker: str = Field(description="Instrument symbol, e.g. AAPL")
    quantity: float | None = Field(
        description="Signed units held; negative is short; None when unknown "
        "(derivatives books report notional instead)", allow_inf_nan=False,
    )
    average_price: float | None = Field(
        default=None, description="Average entry price per unit", allow_inf_nan=False,
    )
    mark_price: float | None = Field(
        default=None, description="Snapshot mark price per unit", allow_inf_nan=False,
    )
    price_currency: str | None = Field(default=None, description="Quote currency of the mark price")
    base_currency: str | None = Field(default=None, description="Base asset currency of the mark price")
    notional: float | None = Field(
        default=None, description="Absolute marked exposure when provided", allow_inf_nan=False,
    )
    notional_currency: str | None = Field(default=None, description="Currency for notional exposure")
    market: Literal["spot", "derivatives"] = Field(default="spot")
    market_symbol: str | None = Field(default=None, description="Venue symbol for the held instrument")


def _units_text(quantity: float | None) -> str:
    """Unit phrase for ``render``; a None count is reported, never formatted.

    quantity is nullable (derivatives books report notional instead), so a
    spot consumer must describe the unknown instead of raising mid-graph.
    """
    return f"{quantity:,.4g} units" if quantity is not None else "quantity unknown"


class PortfolioContext(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    cash: float | None = Field(default=None, description="Free cash available or account equity")
    currency: str | None = Field(default=None, description="Currency label for cash and prices")
    positions: list[Position] = Field(default_factory=list)

    def positions_in(self, ticker: str) -> list[Position]:
        """The holdings of the instrument the run prices for ``ticker``, under any
        broker spelling (BTCUSDT, 700.HK). Holdings keep the spelling they were
        given: a symbol can also name another instrument (WTI the stock)."""
        symbol = normalize_symbol(ticker)
        return [p for p in self.positions if normalize_symbol(p.ticker) == symbol]

    def position_in(self, ticker: str) -> Position | None:
        held = self.positions_in(ticker)
        return held[0] if held else None

    def render(self, ticker: str) -> str:
        """The portfolio block for the decision agents, led by the analyzed instrument."""
        symbol = normalize_symbol(ticker)
        held = self.positions_in(symbol)
        lines = []
        for position in held:
            price = f", average price {position.average_price:,.2f}" if position.average_price is not None else ""
            spelled = position.ticker.strip().upper()
            listed = f" (listed as {position.ticker})" if spelled != symbol else ""
            lines.append(f"- Current position in {symbol}: {_units_text(position.quantity)}{price}{listed}")
        if not held:
            lines.append(f"- No current position in {symbol}")
        if self.cash is not None:
            lines.append(f"- Cash available: {self.cash:,.2f}{' ' + self.currency if self.currency else ''}")
        others = [p for p in self.positions if all(p is not h for h in held)]
        if others:
            lines.append(
                "- Other positions: "
                + ", ".join(f"{p.ticker.upper()} {_units_text(p.quantity)}" for p in others)
            )
        return "Portfolio at the analysis date:\n" + "\n".join(lines)

    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]


def load_portfolio(path: str | Path) -> PortfolioContext:
    """Read a portfolio JSON file, failing before the run rather than mid-graph."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return PortfolioContext.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"portfolio file {path} is not usable: {exc}") from exc
