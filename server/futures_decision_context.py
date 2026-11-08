"""Pure futures-account context for risk checks and decision agents."""

from __future__ import annotations

from typing import Any

from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.portfolio import PortfolioContext, Position


def build_futures_portfolio(
    snapshot: FuturesAccountSnapshot,
    position_stops: dict[str, float] | None = None,
) -> PortfolioContext:
    """Keep futures equity as the sizing balance and positions as exposure.

    Equity already includes open-position PnL. The derivatives marker keeps
    RiskGuard from adding the positions' notional to that denominator again.
    ``position_stops`` maps "BASE-USD" to the recorded stop of an open
    position; the venue book carries no protective levels, and RiskGuard's
    open-heat cap reads ``Position.stop_loss`` (a missing stop fails that
    cap closed), so callers fetch the map from the persisted entry state.
    """
    equity = snapshot.equity if snapshot.equity is not None else snapshot.available_balance
    currency = snapshot.equity_currency or snapshot.available_balance_currency
    if not currency:
        notional_currencies = {
            str(position.notional_currency or "").strip().upper()
            for position in snapshot.positions
            if position.notional is not None
        }
        if notional_currencies and "" not in notional_currencies and len(notional_currencies) == 1:
            currency = next(iter(notional_currencies))
    positions = []
    for position in snapshot.positions:
        if not position.symbol or "/" not in position.symbol:
            raise ValueError("futures position has no usable market symbol")
        base, pair = position.symbol.split("/", 1)
        quote, _, settle = pair.partition(":")
        market_symbol = f"{base}-USD"
        positions.append(Position(
            ticker=normalize_symbol(market_symbol),
            quantity=position.quantity,
            average_price=position.entry_price,
            mark_price=position.mark_price,
            price_currency=position.price_currency or quote,
            base_currency=base,
            notional=position.notional,
            notional_currency=(
                position.notional_currency
                or (settle if settle and settle != quote else quote)
            ),
            market="derivatives",
            market_symbol=market_symbol,
            stop_loss=(position_stops or {}).get(market_symbol),
        ))
    return PortfolioContext(
        cash=float(equity) if equity is not None else None,
        currency=currency,
        positions=positions,
    )


def effective_futures_leverage_cap(*, risk_guard_cap: float, auto_cap: float) -> float:
    """The lowest leverage ceiling enforced by RiskGuard and autoloop."""
    return min(float(risk_guard_cap), float(auto_cap))


def _current_notional_line(account: FuturesAccountSnapshot, config: dict) -> str:
    """The PM-facing derivatives-notional line with the numbers already computed.

    Mirrors RiskGuard's own arithmetic (open derivatives notional / equity);
    a missing or non-positive equity renders "unavailable" instead of a
    fabricated percentage: RiskGuard derives an infinite deriv_pct from such
    an account and refuses every entry, so any headroom line would invent
    capacity the guard does not grant.
    """
    soft_cap = float(config.get("risk_max_derivatives_exposure_pct", 0.0))
    if account.equity is None or account.equity <= 0:
        return "- Current derivatives notional: unavailable"
    open_notional = sum(
        float(position.notional) for position in account.positions
        if position.notional is not None
    )
    current_pct = open_notional / float(account.equity) * 100
    headroom_pct = max(soft_cap - current_pct, 0.0)
    return (
        f"- Current derivatives notional: {current_pct:.2f}% of equity | "
        f"Cap: {soft_cap:g}% | Remaining headroom: {headroom_pct:.2f}%"
    )


def _override_rules_lines(config: dict) -> list[str]:
    """The conditional override rule, spelled out, only when the tier is armed.

    hard = 0 (the default) keeps the historical single-bracket refusal and the
    rule must not appear: it describes a mechanism that does not exist then.
    """
    hard_cap = float(config.get("risk_max_derivatives_exposure_hard_pct", 0.0))
    if hard_cap <= 0:
        return []
    min_r = float(config.get("risk_derivatives_override_min_r", 2.0))
    max_risk_fraction = float(
        config.get("risk_derivatives_override_max_risk_fraction", 0.01)
    )
    max_per_day = int(config.get("risk_derivatives_override_max_per_day", 0))
    return [
        f"- Between the normal cap and the hard cap of {hard_cap:g}%, an entry is "
        "allowed ONLY if ALL hold: closed 1h+4h candles confirm the direction, "
        f"reward/risk >= {min_r:g}, risk-at-stop <= {max_risk_fraction:g}, funding "
        f"is not adverse, and the daily override budget ({max_per_day:g}) is not "
        "spent. The executor verifies these mechanically — a proposal exceeding "
        "the normal cap without meeting every condition is refused, not resized."
    ]


def _heat_lines(config: dict) -> list[str]:
    """The portfolio-heat cap line, only when the cap is enabled (> 0)."""
    heat_cap = float(config.get("risk_max_open_heat_pct", 0.0))
    if heat_cap <= 0:
        return []
    return [
        f"- Maximum open heat (total risk-at-stop across open positions): "
        f"{heat_cap:g}% of equity"
    ]


def render_futures_decision_context(
    market: dict,
    account: FuturesAccountSnapshot,
    ticker: str,
    config: dict,
    brackets: dict,
    *,
    max_leverage: float,
) -> str:
    """Render supplied account, market, brackets and effective execution caps."""
    def number(value: Any, suffix: str = "") -> str:
        return f"{float(value):,.4f}{suffix}" if value is not None else "unavailable"

    def percent(value: Any) -> str:
        return f"{float(value) * 100:.4f}%" if value is not None else "unavailable"

    currency = account.equity_currency or account.available_balance_currency or ""
    lines = [
        f"Venue profile: {account.venue_profile or 'unreported'}",
        f"Account scope: {account.account_scope}",
        f"Futures equity: {number(account.equity, ' ' + currency if currency else '')}",
        f"Available margin: {number(account.available_balance, ' ' + currency if currency else '')}",
        f"Wallet balance: {number(account.wallet_balance, ' ' + currency if currency else '')}",
        f"Unrealized PnL: {number(account.unrealized_pnl, ' ' + currency if currency else '')}",
    ]
    if account.positions:
        lines.append("Open futures positions:")
        for position in account.positions:
            lines.append(
                f"- {position.symbol} {position.side} contracts={position.contracts} "
                f"entry={position.entry_price} mark={position.mark_price} "
                f"leverage={position.leverage} margin_mode={position.margin_mode} "
                f"uPnL={position.unrealized_pnl}"
            )
    else:
        lines.append("Open futures positions: none")
    lines.extend([
        "",
        f"Instrument: {market.get('unified_symbol')} (requested as {ticker})",
        f"Closed 1h candle: open={market['one_hour'].open} high={market['one_hour'].high} "
        f"low={market['one_hour'].low} close={market['one_hour'].close} "
        f"direction={market.get('one_hour_direction')}",
        f"Closed 4h candle: open={market['four_hour'].open} high={market['four_hour'].high} "
        f"low={market['four_hour'].low} close={market['four_hour'].close} "
        f"direction={market.get('four_hour_direction')}",
        f"Funding rate: {percent(market.get('funding_rate'))} "
        f"(next funding {market.get('funding_datetime') or 'unavailable'})",
        f"Mark price: {number(market.get('mark_price'))}",
        f"Open interest: {number(market.get('open_interest'))} "
        f"contracts (notional {number(market.get('open_interest_value'))})",
    ])
    long_bracket, short_bracket = brackets["long"], brackets["short"]
    for side, bracket in (("long", long_bracket), ("short", short_bracket)):
        if bracket[0] is not None:
            lines.append(f"Engine ATR bracket ({side}): stop={bracket[0]} target={bracket[1]}")
        else:
            # Every other unavailable metric is printed as "unavailable"; a
            # bracket line that silently vanishes reads as criterion 2 failing
            # and invites an unexplained abstain while the ATR source is empty.
            lines.append(f"Engine ATR bracket ({side}): unavailable")
    lines.extend([
        "",
        "Hard limits the execution layer enforces (a proposal outside them is refused, "
        "not resized):",
        f"- Maximum leverage: {max_leverage:g}x",
        f"- Maximum risk_fraction: {float(config.get('risk_derivatives_max_risk_fraction', 0.02)):g}",
        f"- Maximum margin_fraction: {float(config.get('risk_derivatives_max_margin_fraction', 0.5)):g}",
        f"- Maximum derivatives notional: "
        f"{float(config.get('risk_max_derivatives_exposure_pct', 0.0)):g}% of equity",
        _current_notional_line(account, config),
        *_override_rules_lines(config),
        *_heat_lines(config),
        f"- Maximum position per asset: "
        f"{float(config.get('risk_max_position_pct_per_asset', 25.0)):g}% of equity",
        f"- Maximum total exposure: "
        f"{float(config.get('risk_max_total_exposure_pct', 80.0)):g}% of equity",
        "- One open position per symbol: a symbol that already holds a futures "
        "position (see the list above) is managed by the exit layer; a new entry "
        "on it is refused",
        "A FuturesExecutionProposal is required for any entry: margin mode, leverage, "
        "risk_fraction, margin_fraction, and an evidence-based rationale. Without one "
        "no order is placed. If a proposal omits its stop and take-profit (they are "
        "proposed together), the engine ATR bracket substitutes for those two "
        "protective levels only and is revalidated; margin mode, leverage, and "
        "budgets are never substituted — a proposal missing them is refused.",
        "",
        "Enter criteria for this cycle (the bar is symmetric for longs and shorts):",
        "1. Setup direction: the closed 1h and 4h candles above must both confirm the "
        "rating's direction (Buy/Overweight long, Sell/Underweight short); a doji or a "
        "mismatch means the direction fails.",
        "2. Executable bracket: propose entry/stop/target at prices consistent with the "
        "mark price and the engine ATR brackets above, stop on the invalidation side; "
        "if the brackets above are unavailable, propose your own stop and take-profit "
        "together (without both protective levels no order is placed) and treat the "
        "missing bracket as a named risk factor, not a reason to skip the analysis.",
        "3. Reward/risk and carry: the take-profit distance must justify the stop "
        "distance after the funding cost shown above (positive funding penalizes a "
        "long, negative a short); treat unavailable funding or open interest as a "
        "named risk factor, not a reason to skip the analysis.",
        "4. Account constraints: available margin, open positions, and the caps above "
        "must leave room for the proposed leverage and budgets; propose inside the "
        "caps, never above them.",
        "When all four criteria hold, enter with a complete proposal instead of "
        "defaulting to abstain. When one genuinely fails, abstain and say which one.",
    ])
    return "\n".join(lines)
