"""Active Kronos context for the futures decision path.

One concern: turn the closed 4h candles the cycle already read into a compact
typed forecast block and append it to the futures execution context, so the
Trader and the Portfolio Manager see the model's sample BEFORE they decide.
This module owns the runtime forecast preparation AND the model lifecycle for
the active path; the autoloop only calls it.

Forecast process ownership stays here, while scheduler, candidate analysis and
Futures execution each have their own autoloop module. The caller supplies the
cycle budget and consumes the forecast without holding execution locks.

The boundaries this module holds:

- **It never decides.** The block is data for the agents. No candidate is
  filtered by direction, no plan is altered, no risk limit is touched. The
  failure POLICY below is the only lever, and it acts on failures, not on the
  forecast's direction.
- **It never holds a lock.** No ``execution_lock`` is passed in or acquired; the
  inference happens on the candidate's own thread, before the graph is built, so
  bracket monitoring and position protection are untouched.
- **Off is off.** With ``kronos_active_enabled`` false, nothing is read, the
  model is never started, and the caller's execution context is byte-identical
  to what it was.
- **Scoped lifecycle ownership.** When no service is supplied, this module
  builds one for the call and closes it in ``finally`` — on success, on error
  and on timeout. Nothing is cached across candidates and no global pool
  exists, so no model child can outlive its owner. The price is honest: every
  scoped call pays cold start plus inference (measured ~5.6–7.1 s per
  candidate for 96 closed bars and 16 paths); there is NO warm
  reuse across candidates in this ownership model.
- **The inference receives the cycle budget.** A ``deadline`` (monotonic) is
  turned into a sampler chunk limit when the owned service is constructed.
  Artifact checks, venue history, process startup and cleanup have their own
  duration, so this is not an end-to-end cycle deadline. The caller checks
  ``cancelled`` and the remaining budget again after the forecast returns.
  Nothing here takes or blocks an execution/protection lock.
- **Failure is a status in the context.** ``unavailable``/``timeout``/``error``
  render as a status line the agents can read. The cycle continues or defers
  per the operator's failure policy; nothing pretends a missing forecast was a
  passed check.
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
from datetime import UTC, datetime
from typing import Any

from tradingagents.dataflows.market_data import Candle, fetch_closed_candles
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


class ActiveForecastUnavailable(RuntimeError):
    """The active forecast could not be produced; the caller degrades."""


def settings_for(config: dict[str, Any]) -> ForecastSettings:
    """The active settings the operator configured (never invented here)."""
    return settings_from_config(config)


def history_for(
    exchange: Any, symbol: str, settings: ForecastSettings, *, as_of: datetime,
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
        limit=settings.lookback_bars,
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
    deadline: float | None = None,
    cancelled: threading.Event | None = None,
) -> ForecastBlock | None:
    """One block for one symbol, or None when the feature is off.

    ``None`` means "the operator did not enable this", and the caller must leave
    its context exactly as it was — an empty string appended to a decision
    context would change the prompt for every candidate.

    Ownership: when ``service`` is None, one is built here for THIS call and
    closed in ``finally`` — success, error and timeout all close it. A caller
    that owns a service passes it in and keeps the close obligation itself.

    Budget: when constructing an owned service, the current remaining cycle
    time caps the sampler's pipe deadline. No service is built if that time is
    already spent. Artifact verification and history reads take additional time;
    the caller checks the deadline and cancellation again after this call.
    A supplied service keeps the deadline configured by its owner.
    """
    settings = settings_for(config)
    if not settings.enabled:
        return None
    started = time.perf_counter()
    owned = service is None
    if owned:
        budget = None if deadline is None else deadline - time.monotonic()
        if budget is not None and budget <= 0:
            logger.info("kronos active: cycle budget spent before inference for %s", symbol)
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="the cycle deadline was already reached; no inference was started",
            )
        service = KronosForecastService.from_config(config, chunk_deadline_seconds=budget)
    try:
        if cancelled is not None and cancelled.is_set():
            return ForecastBlock(
                symbol=symbol, status=STATUS_UNAVAILABLE,
                reason="the cycle was cancelled before inference",
            )
        try:
            candles = history_for(exchange, symbol, settings, as_of=as_of)
        except Exception as exc:  # a history read failure is a status, never a crash
            logger.info("kronos active: history read failed for %s (%r)", symbol, exc)
            return ForecastBlock(
                symbol=symbol,
                status="unavailable",
                reason=f"closed {settings.timeframe} history could not be read: {exc}",
            )
        series = CandleSeries(
            symbol=symbol, candles=candles,
            as_of=as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC),
        )
        seed = stable_seed(cycle_id, "kronos_active")
        block = service.forecast(series, seed=seed)
        block = _out_of_active_decisions(block, symbol)
        block = _with_latency(block, (time.perf_counter() - started) * 1000.0)
        logger.info(
            "kronos active: %s status=%s direction=%s latency_ms=%.0f",
            symbol, block.status, block.direction, block.latency_ms or -1.0,
        )
        return block
    finally:
        if owned:
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
    """Carry the caller-observed wall time, cold start and sampler included.

    The number the operator needs is what the cycle actually spent, including
    the interpreter's cold start under scoped ownership — there is no warm
    child to exclude, and a cached hit still paid the history read.
    """
    from dataclasses import replace

    return replace(block, latency_ms=measured_ms)


def append_to_context(execution_context: str, block: ForecastBlock | None) -> str:
    """Append the rendered block to a decision context; off adds nothing.

    ``None`` (feature disabled) returns the context UNCHANGED, including when the
    context is empty — so a cycle with the feature off produces the same string,
    and therefore the same run signature, as before the feature existed.
    """
    if block is None:
        return execution_context
    rendered = render_forecast_block(block)
    if not execution_context:
        return rendered
    return f"{execution_context}\n\n{rendered}"


def policy_for(block: ForecastBlock | None, settings: ForecastSettings) -> str:
    """What the cycle must do about a block: ``use``, ``defer`` or ``pass``.

    ``defer`` is the honest default for a failure: the cycle has no forecast for
    this candidate, which is different from having one that was checked. With
    ``defer``, the CALLER stops the candidate before the graph/LLM and
    persists the reason; with ``pass_through``, the cycle continues with the
    failure stated in its context. There is no ``context_only`` failure policy:
    it had no behavior distinct from ``defer`` (``mode`` is where the context
    contract is named), and a knob with no difference is not a choice.

    Never returns a value that would cut a candidate: the direction gate is not
    used (see the module docstring), so every outcome keeps the candidate.
    """
    if block is None:
        return "use"  # feature off: the incumbent path
    if block.ok:
        return "use"
    if settings.failure_policy == "pass_through":
        return "pass"
    return "defer"


__all__ = [
    "ActiveForecastUnavailable",
    "append_to_context",
    "forecast_for_cycle",
    "history_for",
    "policy_for",
    "settings_for",
]
