"""Closed-market and forecast inputs for a Futures decision."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from server import (
    kronos_context,
)
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.execution.futures_scheduler import closed_candle_pair
from tradingagents.forecast.block import ForecastBlock

logger = logging.getLogger(__name__)
_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER

def _kronos_block_for(
    derivatives: Any, ticker: str, futures_context: dict, config: dict, *, cycle_id: str,
    deadline: float | None = None, cancelled: threading.Event | None = None,
):
    """Build the Kronos block from the Futures cycle's resolved symbol and cutoff."""
    symbol = str(futures_context.get("unified_symbol") or futures_context.get("symbol") or ticker)
    try:
        exchange = derivatives._bridge.client()
        identity = futures_context["candle_identity"]
        if identity["timeframe"] != config.get("auto_futures_timeframe", "4h"):
            raise ValueError("forecast timeframe differs from the selected candle")
        as_of = datetime.fromtimestamp(identity["close_time"] / 1000, UTC)
        block = kronos_context.forecast_for_cycle(
            service=None, exchange=exchange, symbol=symbol, config=config,
            as_of=as_of, cycle_id=cycle_id, expected_candle=futures_context["selected_candle"],
            deadline=deadline, cancelled=cancelled,
        )
        if block is not None and block.ok and (
            block.symbol != symbol or block.timeframe != identity["timeframe"]
            or block.data_open_time != identity["open_time"]
        ):
            return ForecastBlock(
                symbol=symbol, status="unavailable",
                reason="forecast candle identity differs from the selected cycle candle",
            )
        return block
    except Exception as exc:  # a forecast failure must never fail the candidate
        logger.warning("kronos active: forecast failed for %s (%r)", ticker, exc)
        return ForecastBlock(
            symbol=symbol, status="error",
            reason=f"active forecast raised {type(exc).__name__}: {exc}",
        )


def _closed_futures_context(derivatives: Any, ticker: str) -> dict | None:
    now = datetime.now(UTC)
    one_hour, four_hour = closed_candle_pair(now)
    symbol = derivatives.resolve_market(ticker) if hasattr(derivatives, "resolve_market") else derivatives.symbol_for(ticker)
    exchange = derivatives._bridge.client()
    candles_1h = fetch_closed_candles(exchange, symbol, "1h", as_of=now, limit=2)
    candles_4h = fetch_closed_candles(exchange, symbol, "4h", as_of=now, limit=2)
    if not candles_1h or not candles_4h:
        return None
    candle_1h, candle_4h = candles_1h[-1], candles_4h[-1]
    if (candle_1h.open_time != int(one_hour[0].timestamp() * 1000)
            or candle_4h.open_time != int(four_hour[0].timestamp() * 1000)):
        return None
    return {
        "requested_ticker": ticker,
        "decision_as_of": now.isoformat(),
        "symbol": symbol,
        "unified_symbol": symbol,
        "one_hour": candle_1h,
        "four_hour": candle_4h,
        "one_hour_open": candle_1h.open_time,
        "one_hour_close": candle_1h.close,
        "four_hour_open": candle_4h.open_time,
        "four_hour_close": candle_4h.close,
        "one_hour_direction": _candle_direction(candle_1h),
        "four_hour_direction": _candle_direction(candle_4h),
        **_futures_derivative_metrics(exchange, symbol),
    }


def _futures_derivative_metrics(exchange: Any, symbol: str) -> dict:
    """Funding rate and open interest for the resolved contract.

    The futures agents are asked to justify an entry against funding and
    risk/reward, and without this read they report "funding unavailable" and
    abstain. Each field degrades to None instead of failing the cycle: a venue
    does not serve every metric on every market, and a missing metric must not
    block the analysis that is the deliverable.
    """
    metrics: dict[str, Any] = {
        "funding_rate": None,
        "funding_datetime": None,
        "mark_price": None,
        "open_interest": None,
        "open_interest_value": None,
    }
    try:
        funding = exchange.fetch_funding_rate(symbol)
    except Exception as exc:  # venue may not publish funding for this market
        logger.info("funding rate unavailable for %s (%s)", symbol, exc)
        funding = None
    if isinstance(funding, dict):
        metrics["funding_rate"] = funding.get("fundingRate")
        metrics["funding_datetime"] = funding.get("fundingDatetime")
        metrics["mark_price"] = funding.get("markPrice")
    try:
        interest = exchange.fetch_open_interest(symbol)
    except Exception as exc:  # venue may not publish open interest
        logger.info("open interest unavailable for %s (%s)", symbol, exc)
        interest = None
    if isinstance(interest, dict):
        metrics["open_interest"] = interest.get("openInterestAmount")
        metrics["open_interest_value"] = interest.get("openInterestValue")
    return metrics


def _candle_direction(candle: Any) -> str:
    if candle.close > candle.open:
        return "bullish"
    if candle.close < candle.open:
        return "bearish"
    return "doji"


def _confirmation_state(context: dict, signal: str) -> tuple[bool, str]:
    # Kept strict on replay evidence (2026-10-29, tests/test_futures_confirmation_replay.py):
    # the archived funnel records 86 confirmation rejects, all direction_mismatch,
    # and of the 58 rejects in the newer export only 1 carries the closed 1h/4h
    # pair — so a counterfactual is scored on 77 snapshot rows total
    # (server/futures_replay.py + tests/fixtures/futures_cycles_replay.json).
    # What that replay shows: "4h dominance + bounded 1h pullback" would
    # confirm 3 additional candidate candles (all QNT-USD shorts), but every
    # one of those cycles was refused UPSTREAM at the intent gate (abstain /
    # manage-only / no structured terms, phase=proposal before this check
    # runs) — loosening this gate creates ZERO new entries in the sample. Two
    # of the 3 confirmations are even negative-retrace range-breaks (1h close
    # below the 4h low) that the harness's bound passes and labels a pullback.
    # The 1:1 agreement between recorded reasons and this rule is
    # mirror-consistency, not out-of-sample validation: snapshot rows' payload
    # reasons were derived at persist time by this very function
    # (_record_futures_cycle_rejection). No forward-return data exists for any
    # flip, so nothing here is evidence to loosen a futures entry gate; the
    # conservative side is refusing entry. This rule stays until reject rows
    # routinely carry their candle pair (current code persists the snapshot;
    # the archive predates it) and the sample can be judged on outcomes.
    if signal not in (_BUY, _OVERWEIGHT, _SELL, _UNDERWEIGHT):
        return True, "not_required_for_non_tradeable_signal"
    expected = "bearish" if _direction(signal) == "short" else "bullish"
    directions = (context.get("one_hour_direction"), context.get("four_hour_direction"))
    if any(direction == "doji" for direction in directions):
        return False, "doji_candle"
    if directions != (expected, expected):
        return False, "direction_mismatch"
    return True, "aligned_direction"


def _unified_mark_price(bridge: Any, symbol: str) -> float | None:
    """Last price for the already-resolved unified futures symbol.

    Read straight off the venue client: the bridge's fetch_price() routes
    through the SPOT symbol resolver, which rejects off-whitelist bases. The
    futures path already resolved its contract from venue metadata, so it
    must not re-resolve (and silently drop) the mark price.
    """
    row = bridge.client().fetch_ticker(symbol)
    if not isinstance(row, dict):
        return None
    for key in ("last", "close"):
        value = row.get(key)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _direction(signal: str) -> str:
    """Map an authorized entry rating to its futures bracket direction.

    Sell/Underweight become short only after the separate PM intent gate has
    explicitly authorized a fresh ``enter`` decision; the rating alone is not
    a futures authorization.
    """
    return "short" if signal in (_SELL, _UNDERWEIGHT) else "long"


def _bracket_prices(ticker: str, direction: str = "long") -> tuple[float | None, float | None]:
    """Fetch the existing ATR bracket as values for risk sizing."""
    from tradingagents.dataflows.screening import atr_targets_signed

    return atr_targets_signed(ticker, _today_iso(), direction)


def _direction_brackets(
    ticker: str, brackets: dict | None = None,
) -> tuple[tuple[float | None, float | None], tuple[float | None, float | None]]:
    """(long, short) ATR brackets, reusing a caller's reads when supplied."""
    if brackets is not None:
        return brackets["long"], brackets["short"]
    return _bracket_prices(ticker, direction="long"), _bracket_prices(ticker, direction="short")


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()
