"""Active Kronos context for the futures decision path.

Turn the selected timeframe's closed candles into a forecast block and append
it to the Futures execution context before the Trader and Portfolio Manager
decide. Cycle-scoped service ownership lives in ``server.kronos_lifecycle``;
the caller supplies the cycle budget without holding execution locks.

The boundaries this module holds:

- **It never decides.** The block is data for the agents. No candidate is
  filtered by direction, no plan is altered, no risk limit is touched. The
  failure POLICY below is the only lever, and it acts on failures, not on the
  forecast's direction.
- **It never holds an execution lock.** The owner serializes service leases,
  but no ``execution_lock`` is passed in or acquired; inference runs before the
  graph is built, so bracket monitoring and position protection are untouched.
- **Every candidate gets an attempt.** A missing or failed forecast defers the
  Futures candidate before graph construction and LLM analysis.
- **Cycle-scoped warm ownership.** One Futures mode cycle owns one lazy service
  and sampler, reused through exclusive leases across candidates. Closing stops
  new leases and defers cleanup until the active lease exits. A call without an
  owner or supplied service builds and closes its own service exactly once.
  No global pool or cross-cycle reuse exists.
- **The inference receives the original cycle deadline.** The absolute monotonic
  ``deadline`` reaches the sampler unchanged, so history, artifact verification,
  construction and lock waiting all consume the same budget. It never becomes
  the warm sampler's lifetime chunk limit. Blocking setup and cleanup are not
  preemptible; cancellation and deadline are rechecked before inference, and
  the caller checks them again after the forecast returns.
- **Failure defers the candidate.** ``unavailable``/``timeout``/``error`` are
  recorded on the cycle; the graph and LLM do not run without a forecast.
- **Research output stays out of active decisions.** A block whose
  ``generation_kind`` is ``retrospective`` — drawn after its own first
  predicted bar closed — is converted to an explicit failure here. A
  ``created_at`` before the outcome bar closes is NOT proof the forecast
  existed at the decision timestamp, so retrospective labels are never fed to
  a live decision as if they were forward forecasts.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from server import kronos_lifecycle
from tradingagents.auto_timeframes import timeframe_ms
from tradingagents.dataflows.market_data import Candle, fetch_closed_candles
from tradingagents.forecast.artifact import MIN_LOOKBACK_BARS
from tradingagents.forecast.block import CandleSeries, ForecastBlock
from tradingagents.forecast.contract import (
    GENERATION_RETROSPECTIVE,
    STATUS_UNAVAILABLE,
    stable_seed,
)
from tradingagents.forecast.rendering import render_forecast_block
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import ForecastSettings, settings_from_config

logger = logging.getLogger(__name__)

def settings_for(config: dict[str, Any]) -> ForecastSettings:
    """The active settings the operator configured (never invented here)."""
    return settings_from_config(config)


def history_for(
    exchange: Any, symbol: str, settings: ForecastSettings, *, as_of: datetime,
    minimum_bars: int | None = None,
) -> tuple[Candle, ...]:
    """Closed candles for one symbol, cut at ``as_of``, same venue reader.

    ``fetch_closed_candles`` drops every bar whose close is after ``as_of``, so
    the series cannot contain a bar that had not closed at the decision moment.
    When the requested lookback exceeds the venue page cap, the same reader
    paginates forward and validates the complete closed-bar grid before returning.
    That is the same reader and the same cutoff discipline the confirmation path
    uses — the forecast is not given a more recent view of the market than the
    decision itself has. The service re-validates the series' identity (symbol,
    closed-bar grid, staleness) before anything is sampled.
    """
    candles = fetch_closed_candles(
        exchange, symbol, settings.timeframe, as_of=as_of,
        limit=settings.lookback_bars, minimum_bars=minimum_bars,
    )
    return tuple(candles)


def forecast_for_cycle(
    *,
    service: KronosForecastService | None,
    exchange: Any,
    symbol: str,
    config: dict[str, Any],
    as_of: datetime,
    cycle_id: str,
    expected_candle: Candle | None = None,
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
    owner: kronos_lifecycle.KronosForecastOwner | None = None,
) -> ForecastBlock | None:
    """Produce one forecast block, returning a status when inference cannot run.

    A locally created service closes exactly once; a warm owner retains its
    service across calls. The original deadline includes setup and lease wait,
    while the caller rechecks cancellation after the call.
    """
    settings = settings_for(config)
    started = time.perf_counter()
    owned = service is None and owner is None
    if deadline is not None and deadline - time.monotonic() <= 0:
        logger.info("kronos active: cycle budget spent before inference for %s", symbol)
        return ForecastBlock(
            symbol=symbol, status=STATUS_UNAVAILABLE,
            reason="the cycle deadline was already reached; no inference was started",
        )
    try:
        if cancelled is not None and cancelled.is_set():
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="the cycle was cancelled before inference",
            )
        try:
            candles = history_for(
                exchange, symbol, settings, as_of=as_of,
                minimum_bars=MIN_LOOKBACK_BARS,
            )
        except Exception as exc:  # a history read failure is a status, never a crash
            logger.info("kronos active: history read failed for %s (%r)", symbol, exc)
            return ForecastBlock(
                symbol=symbol,
                status=STATUS_UNAVAILABLE,
                reason=f"closed {settings.timeframe} history could not be read: {exc}",
            )
        if expected_candle is not None and (not candles or candles[-1] != expected_candle):
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="forecast history differs from the selected cycle candle",
            )
        if not candles:
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="closed forecast history is empty",
            )
        first_realized_close = datetime.fromtimestamp(
            candles[-1].close_time / 1000, UTC,
        ) + timedelta(milliseconds=timeframe_ms(settings.timeframe))
        if datetime.now(UTC) >= first_realized_close:
            return ForecastBlock(
                symbol=symbol,
                status=STATUS_UNAVAILABLE,
                reason=(
                    "the selected candle's first predicted bar already closed before "
                    "inference started; the forecast would be retrospective"
                ),
            )
        series = CandleSeries(symbol=symbol, candles=candles, as_of=as_of)
        if cancelled is not None and cancelled.is_set():
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="the cycle was cancelled after history was read; no inference was started",
            )
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="the cycle deadline was reached after history was read; no inference was started",
            )
        if owned:
            service = KronosForecastService.from_config(config, lookback_bars=len(candles))
        lease = owner.lease(deadline, cancelled) if owner is not None else nullcontext(service)
        try:
            with lease as active_service:
                if cancelled is not None and cancelled.is_set():
                    return ForecastBlock(
                        symbol=symbol, status=STATUS_UNAVAILABLE,
                        reason="the cycle was cancelled during service preparation; no inference was started",
                    )
                if deadline is not None and time.monotonic() >= deadline:
                    return ForecastBlock(
                        symbol=symbol, status=STATUS_UNAVAILABLE,
                        reason="the cycle deadline was reached during service preparation; no inference was started",
                    )
                if active_service is None:
                    raise RuntimeError("Kronos forecast service is unavailable")
                seed = stable_seed(cycle_id, "kronos_active")
                forecast_kwargs = {"seed": seed, "deadline": deadline}
                if owner is not None or owned:
                    forecast_kwargs["lookback_bars"] = len(candles)
                block = active_service.forecast(series, **forecast_kwargs)
        except kronos_lifecycle.ActiveForecastUnavailable as exc:
            return ForecastBlock(symbol=symbol, status=STATUS_UNAVAILABLE, reason=str(exc))
        block = _out_of_active_decisions(block, symbol)
        block = _with_latency(block, (time.perf_counter() - started) * 1000.0)
        logger.info(
            "kronos active: %s status=%s direction=%s latency_ms=%.0f",
            symbol, block.status, block.direction, block.latency_ms or -1.0,
        )
        return block
    finally:
        if owned and service is not None:
            service.close()


def _out_of_active_decisions(block: ForecastBlock, symbol: str) -> ForecastBlock:
    """A retrospective block never enters an active decision as a forecast.

    ``forward`` is a timestamp claim, and a retrospective draw is research
    output. Feeding it to a live decision as if it were a forward forecast
    would quietly turn replay rows into decision context, so the label is
    replaced by an explicit failure the caller's failure policy acts on.
    """
    if block.ok and block.generation_kind == GENERATION_RETROSPECTIVE:
        return ForecastBlock(
            symbol=symbol, status=STATUS_UNAVAILABLE,
            reason=(
                "the forecast is retrospective (drawn after its first predicted bar "
                "closed); research output is kept out of active decisions"
            ),
        )
    return block


def _with_latency(block: ForecastBlock, measured_ms: float) -> ForecastBlock:
    """Carry total call latency: history, lease wait, setup and inference.

    The first lease pays cold start; later leases reuse the warm child. Both
    report the time this candidate actually spent, including cached history reads.
    """
    return replace(block, latency_ms=measured_ms)


def append_to_context(execution_context: str, block: ForecastBlock | None) -> str:
    """Append a forecast or failure status to the decision context."""
    if block is None:
        return execution_context
    rendered = render_forecast_block(block)
    if not execution_context:
        return rendered
    return f"{execution_context}\n\n{rendered}"


def policy_for(block: ForecastBlock | None) -> str:
    """Return ``use`` only for a valid forecast; otherwise defer the candidate."""
    if block is None:
        return "defer"
    return "use" if block.ok else "defer"


__all__ = [
    "append_to_context",
    "forecast_for_cycle",
    "history_for",
    "policy_for",
    "settings_for",
]
