"""The once-a-day runner: analyze a watchlist, plan, risk-check, maybe execute.

``run_daily`` is the scheduling surface for FR-K3/FR-D: one independent
process per invocation, one coin at a time, a failing coin never stops the
rest. Each coin runs the full crypto pipeline (LLM in the loop), syncs the
venue balance, plans through the bridge (which applies the RiskGuard), and
then applies the market-specific automation policy:

- Auto Spot authorizes unattended spot plan execution;
- Auto Futures authorizes unattended futures plan execution;
- a closed mode leaves an actionable plan waiting for approval;
- managed sends require the bridge's explicit confirmation/RiskGuard checks;
- standalone runs choose dry or live using ``exec_live``.

WARNING: enabling Auto Spot or Auto Futures for managed work allows real orders
without a human in the loop. A sandbox or dry-run does not prove the live path.

Scheduling examples (one process per run; no long-lived daemon here):

cron (Linux/macOS), weekdays at 13:30 UTC:
    30 13 * * 1-5  cd /path/to/project && .venv/bin/python -c \\
        "from tradingagents.execution.daily import run_daily; run_daily()" \\
        >> ~/.tradingagents/logs/daily.log 2>&1

Windows Task Scheduler (schtasks), daily at 08:30 local:
    schtasks /Create /TN "TradingAgents daily" /SC DAILY /ST 08:30 /TR \\
        "cmd /c cd /d C:\\path\\to\\project && .venv\\Scripts\\python.exe -c \\\"from tradingagents.execution.daily import run_daily; run_daily()\\\""

Note that each run spends LLM quota (one full agent pipeline per coin) and,
with the live gates armed, can place real orders. Do NOT configure
overlapping cron/schtasks schedules for the same watchlist: the
check-then-execute sequence has no cross-process lock, so two overlapping
runs can both pass their re-checks and double the exposure. The date is
UTC today:
``propagate`` receives ``YYYY-MM-DD`` and the pipeline's point-in-time
contract applies unchanged.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from server.auto_modes import AutoMode, enabled_modes
from tradingagents.execution.bridge import ExchangeBridge
from tradingagents.portfolio import PortfolioContext

# Seconds before the first propagate retry (then linear: +wait per attempt).
# Relay outages are usually short windows; backoff lets them pass instead of
# burning all attempts inside one bad window.
_PROPAGATE_RETRY_WAIT = 20.0

logger = logging.getLogger(__name__)


def _mode_enabled(bridge: ExchangeBridge, mode: AutoMode) -> bool:
    reader = getattr(bridge, "auto_mode_enabled", None)
    if not callable(reader):
        reader = getattr(getattr(bridge, "_bridge", None), "auto_mode_enabled", None)
    if callable(reader):
        return reader(mode, live=bridge.effective_mode() == "live")
    return mode in enabled_modes(getattr(bridge, "config", None))


class _SpotModeCancelled(Exception):
    """The run's captured Auto Spot authorization is no longer valid."""


def _check_spot_mode(bridge: ExchangeBridge) -> None:
    if not _mode_enabled(bridge, AutoMode.SPOT):
        raise _SpotModeCancelled("auto spot is disabled or authorization is stale")


class DailyResult(BaseModel):
    """Per-coin outcome of one ``run_daily`` invocation."""

    ticker: str
    signal: str | None = None
    plan: Any | None = None  # PlannedOrder | None
    executed: bool = False
    order_id: str | None = None
    reason: str | None = None
    candle_identity: dict[str, Any] | None = None


def _today_iso() -> str:
    """UTC today as ``YYYY-MM-DD``; tests freeze the day by monkeypatching."""
    return datetime.now(UTC).date().isoformat()


def _normalize_watchlist(raw: Any) -> list[str]:
    """Coerce a watchlist at the runner boundary (FR-S4).

    A bare string wraps into a one-element list (never ``list()``-split into
    characters), every entry is stripped, empties are dropped and
    duplicates removed keeping first-seen order.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    coins: list[str] = []
    for entry in raw:
        coin = str(entry).strip()
        if coin and coin not in coins:
            coins.append(coin)
    return coins


def run_daily(
    watchlist: list[str] | str | None = None,
    config: dict | None = None,
) -> list[DailyResult]:
    """Analyze ``watchlist`` (default ``exec_watchlist``) and handle each plan.

    The watchlist is normalized at this boundary: a bare string becomes a
    one-coin list, entries are stripped, empties dropped and duplicates
    deduped in order. Returns one ``DailyResult`` per coin. A coin that
    raises anywhere in its own pipeline (propagate, balance sync, planning)
    is recorded with the error and the loop continues. Never raises for a
    single coin's failure.
    """
    bridge = ExchangeBridge(config=config)
    raw = watchlist if watchlist is not None else bridge.config.get("exec_watchlist")
    coins = _normalize_watchlist(raw)
    results: list[DailyResult] = []
    if not _mode_enabled(bridge, AutoMode.SPOT):
        for coin in coins:
            results.append(DailyResult(ticker=coin, reason="auto spot is disabled"))
        return results
    today = _today_iso()
    # Lazy import (like ccxt in bridge.py): importing tradingagents.execution
    # stays cheap and tests can mock the graph class wholesale.
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    for coin in coins:
        result = DailyResult(ticker=coin)
        try:
            # The daily runner is crypto-only (symbol_for rejects anything
            # else), so the fundamentals analyst is dropped here exactly like
            # the CLI's filter_analysts_for_asset_type does for crypto: Yahoo
            # has no quoteSummary fundamentals for crypto symbols, and running
            # that analyst only burns LLM calls on an empty report.
            # Relay relapses: a fresh graph per attempt, because a failed
            # propagate can leave LangGraph checkpoint state mid-pipeline.
            signal = None
            for attempt in range(3):
                _check_spot_mode(bridge)
                try:
                    graph = TradingAgentsGraph(
                        config=bridge.config,
                        selected_analysts=("market", "social", "news"),
                    )
                    _check_spot_mode(bridge)
                    propagate = getattr(graph, "propagate_with_telemetry", None)
                    if callable(propagate) and "check_cancelled" in inspect.signature(propagate).parameters:
                        _, signal = propagate(
                            coin, today, asset_type="crypto", telemetry=None,
                            check_cancelled=lambda: _check_spot_mode(bridge),
                        )
                    else:
                        _, signal = graph.propagate(coin, today, asset_type="crypto")
                    _check_spot_mode(bridge)
                    break
                except _SpotModeCancelled:
                    raise
                except Exception as exc:
                    _check_spot_mode(bridge)
                    if attempt == 2:
                        raise
                    wait = _PROPAGATE_RETRY_WAIT * (attempt + 1)
                    logger.warning(
                        "run_daily: %s propagate failed (attempt %d, %s: %s); "
                        "retrying in %.0fs",
                        coin, attempt + 1, type(exc).__name__, exc, wait,
                    )
                    time.sleep(wait)
            result.signal = signal
            _check_spot_mode(bridge)
            portfolio = bridge.sync_portfolio()
            _check_spot_mode(bridge)
            plan = bridge.plan_order(coin, signal, portfolio=portfolio)
            result.plan = plan
            _check_spot_mode(bridge)
            _handle_plan(bridge, plan, portfolio, result)
        except _SpotModeCancelled as exc:
            result.reason = str(exc)
        except Exception as exc:  # one coin's failure never stops the watchlist
            logger.exception("run_daily: %s failed", coin)
            result.reason = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def _handle_plan(
    bridge: ExchangeBridge,
    plan: Any,
    portfolio: PortfolioContext,
    result: DailyResult,
    execute_guard: Callable[[Callable[[], Any]], Any] | None = None,
    on_entry_evidence: Callable[[dict], None] | None = None,
    check_entry_allowed: Callable[[], None] | None = None,
) -> None:
    """Apply the confirm policy to one plan, recording the outcome."""
    def execute() -> Any:
        kwargs = {"confirm": True, "portfolio": portfolio}
        if on_entry_evidence is not None:
            kwargs["on_entry_evidence"] = on_entry_evidence
        if check_entry_allowed is not None:
            kwargs["check_entry_allowed"] = check_entry_allowed
        order_id = bridge.execute_order(plan, **kwargs)
        result.order_id = order_id
        result.executed = True
        return order_id

    def execute_once() -> Any:
        return execute_guard(execute) if execute_guard is not None else execute()

    if plan.side is None:
        result.reason = plan.reason  # Hold, REVIEW, sizing refusal or risk rejection
        return

    mode = AutoMode.FUTURES if getattr(plan, "market", "spot") == "derivatives" else AutoMode.SPOT
    if not _mode_enabled(bridge, mode):
        _wait_for_approval(bridge, plan, f"Auto {mode.name.title()} is disabled; plan awaits approval")
        result.reason = "awaiting approval"
        return
    execute_once()


def _wait_for_approval(bridge: ExchangeBridge, plan: Any, reason: str) -> None:
    """Audit the waiting-for-approval decision so nothing happens silently."""
    bridge.audit_plan(plan, action="awaiting_approval", reason=reason)
