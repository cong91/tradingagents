"""Discover, screen and refill executable autoloop candidates."""

from __future__ import annotations

import inspect
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from server import (
    autoloop_cycle_records,
    autoloop_execution,
)
from server.auto_modes import AutoMode
from server.autoloop_telemetry import CycleTelemetry
from tradingagents.execution.auto_authorization import (
    AuthorizationSnapshot,
)

logger = logging.getLogger(__name__)


_screening_worker_lock = threading.Lock()


AUTO_UNIVERSE_PER_ASSET = 30


AUTO_TOP_N = 3


AUTO_SCREEN_POOL = AUTO_TOP_N * 4


AUTO_FUTURES_SCREEN_POOL = AUTO_UNIVERSE_PER_ASSET * 2


AUTO_FUTURES_REFILL_ROUNDS = 2


AUTO_FUTURES_MAX_UNIVERSE = AUTO_UNIVERSE_PER_ASSET * 8


SCREENING_TIMEOUT_SECONDS = 180.0


AUTO_REFILL_BUDGET_SHARE = 0.5


def _call_crypto_universe(
    crypto_universe: Callable, config: dict, limit: int, futures_mode: bool,
) -> list[str]:
    """Call the universe seam, opting into the futures-only filter when armed.

    ``crypto_universe`` is injected by tests and by the daily spot path; only
    the autoloop's futures mode wants contract-bearing bases, so the flag is
    passed only when the callable declares it. A seam that predates the flag
    keeps its two-argument shape (same pattern as ``cancel_event`` below).
    """
    if not futures_mode:
        return crypto_universe(config, limit)
    try:
        parameters = inspect.signature(crypto_universe).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_flag = "futures_only" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_flag:
        return crypto_universe(config, limit, futures_only=True)
    return crypto_universe(config, limit)


def _screen_with_timeout(
    crypto_universe: Callable,
    stock_universe: Callable,
    screen_universe: Callable,
    config: dict,
    trade_date: str,
    *,
    telemetry: CycleTelemetry | None = None,
    universe_limit: int | None = None,
    screen_pool: int | None = None,
    timeout_seconds: float | None = None,
    futures_mode: bool | None = None,
    mode: AutoMode | None = None,
    authorization: AuthorizationSnapshot | None = None,
) -> tuple[list[str], list[str], list[dict]]:
    """Run external screening off-cycle so a vendor stall cannot block forever.

    ``universe_limit``/``screen_pool`` widen the inspected pool for a discovery
    refill round; ``None`` keeps the mode default (the full futures pool in
    futures mode, ``AUTO_SCREEN_POOL`` in spot mode). ``timeout_seconds`` caps
    this call so a refill round can never outlive the cycle's own budget.
    """
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise TimeoutError("screening deadline expired before worker start")
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    telemetry = telemetry or CycleTelemetry()
    cancel_screening = threading.Event()
    if not _screening_worker_lock.acquire(blocking=False):
        raise TimeoutError("screening worker from a timed-out cycle is still stopping")

    def worker() -> None:
        try:
            futures_mode_active = (
                config.get("auto_futures_enabled") is True
                if futures_mode is None
                else futures_mode
            )
            crypto_limit = (
                universe_limit
                if universe_limit is not None
                else (AUTO_FUTURES_SCREEN_POOL if futures_mode_active else AUTO_UNIVERSE_PER_ASSET)
            )
            autoloop_execution.raise_if_mode_disabled(mode, "crypto universe", authorization)
            with telemetry.span("screening:crypto_universe"):
                crypto = _call_crypto_universe(
                    crypto_universe, config, crypto_limit, futures_mode_active,
                )
            if cancel_screening.is_set():
                return
            if futures_mode_active:
                # Futures candidates must come from the crypto universe only.
                stocks = []
            else:
                autoloop_execution.raise_if_mode_disabled(mode, "stock universe", authorization)
                with telemetry.span("screening:stock_universe"):
                    stocks = stock_universe(config, AUTO_UNIVERSE_PER_ASSET)
            if cancel_screening.is_set():
                return
            autoloop_execution.raise_if_mode_disabled(mode, "screening metrics", authorization)
            with telemetry.span("screening:metrics"):
                pool_limit = (
                    screen_pool
                    if screen_pool is not None
                    else (
                        AUTO_FUTURES_SCREEN_POOL
                        if futures_mode_active
                        else AUTO_SCREEN_POOL
                    )
                )
                try:
                    screen_parameters = inspect.signature(screen_universe).parameters
                except (TypeError, ValueError):
                    screen_parameters = {}
                accepts_cancel_event = "cancel_event" in screen_parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in screen_parameters.values()
                )
                if accepts_cancel_event:
                    candidates = screen_universe(
                        crypto, stocks, trade_date, pool_limit,
                        cancel_event=cancel_screening,
                    )
                else:
                    candidates = screen_universe(crypto, stocks, trade_date, pool_limit)
            if not cancel_screening.is_set():
                result_queue.put((crypto, stocks, candidates))
        except BaseException as exc:  # hand exception back to cycle thread
            if not cancel_screening.is_set():
                result_queue.put(exc)
        finally:
            _screening_worker_lock.release()

    threading.Thread(target=worker, name="server-autoloop-screen", daemon=True).start()
    budget = SCREENING_TIMEOUT_SECONDS if timeout_seconds is None else min(
        SCREENING_TIMEOUT_SECONDS, timeout_seconds
    )
    try:
        outcome = result_queue.get(timeout=budget)
    except queue.Empty as exc:
        cancel_screening.set()
        telemetry.close()
        snapshot = telemetry.snapshot()
        stage = snapshot["active_stage"] or snapshot["last_stage"] or "screening"
        raise TimeoutError(
            f"screening timed out during {stage} after {budget:.0f}s"
        ) from exc
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _preflight_candidates(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    telemetry: CycleTelemetry | None = None,
) -> tuple[list[dict], list[dict]]:
    runnable, skipped, _metadata_available = _preflight_candidates_with_metadata(
        candidates, derivatives, telemetry=telemetry
    )
    return runnable, skipped


def _preflight_candidates_with_metadata(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    telemetry: CycleTelemetry | None = None,
    rejections_recorded: set[str] | None = None,
) -> tuple[list[dict], list[dict], bool | None]:
    """Drop candidates that can never become an order, before the LLM pipeline.

    In futures mode two kinds of screened candidate are guaranteed to end with
    no order: stocks (the venue bridge only plans crypto) and symbols with no
    live USDT-settled contract. Each one still costs a full 8-10 minute LLM
    pipeline, which is how a cycle used to time out with its executable
    candidates never analysed.

    Returns ``(runnable, skipped, market_metadata_available)``. ``skipped``
    entries are ``{"ticker", "reason"}`` for the cycle summary and audit. A
    metadata failure fails open for crypto only: the per-candidate path
    re-checks and rejects it on its own, while stocks never enter the futures
    path.

    ``rejections_recorded`` carries tickers a previous discovery round already
    persisted a rejection for. A refill round re-screens the same pool, so it
    must report them in ``skipped`` without writing the same cycle row again;
    the set is extended with the tickers this call records.
    """
    if derivatives is None:
        return candidates, [], None  # spot mode: stocks still carry advisory value

    bridge = getattr(derivatives, "_bridge", None)
    if bridge is None:
        skipped = []
        for candidate in candidates:
            asset_type = candidate.get("asset_type")
            if asset_type == "crypto":
                continue
            reason = (
                "advisory only in futures mode — the venue bridge cannot plan a stock order"
                if asset_type == "stock"
                else f"unsupported asset_type in futures mode: {asset_type!r}"
            )
            skipped.append({"ticker": str(candidate.get("ticker")), "reason": reason})
        return [candidate for candidate in candidates if candidate.get("asset_type") == "crypto"], skipped, None

    telemetry = telemetry or CycleTelemetry()
    markets: dict[str, Any] | None
    try:
        with telemetry.span("preflight:futures_markets"):
            markets = bridge.client().load_markets()
    except Exception as exc:  # venue unreachable: leave the decision to the candidate path
        logger.warning("autoloop preflight: futures markets unavailable (%s)", exc)
        markets = None

    runnable: list[dict] = []
    skipped: list[dict] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker"))
        asset_type = candidate.get("asset_type")
        if asset_type != "crypto":
            reason = (
                "advisory only in futures mode — the venue bridge cannot plan a stock order"
                if asset_type == "stock"
                else f"unsupported asset_type in futures mode: {asset_type!r}"
            )
            skipped.append({"ticker": ticker, "reason": reason})
            continue
        if markets is None:
            # Keep crypto candidates for the per-candidate resolver/context path,
            # but never let an unavailable futures venue route a stock into the
            # derivatives pipeline.
            runnable.append(candidate)
            continue
        try:
            bridge.futures_symbol_for(ticker, markets=markets)
        except ValueError as exc:
            reason = f"no active futures contract: {exc}"
            skipped.append({"ticker": ticker, "reason": reason})
            if rejections_recorded is None or ticker not in rejections_recorded:
                autoloop_cycle_records._record_futures_cycle_rejection(
                    ticker,
                    market_rejection_reason=str(exc),
                    phase="market",
                    telemetry={"execution_status": "not_started"},
                )
                if rejections_recorded is not None:
                    rejections_recorded.add(ticker)
            continue
        runnable.append(candidate)
    return runnable, skipped, markets is not None


def _select_cycle_candidates(
    candidates: list[dict],
    derivatives: Any | None,
    *,
    limit: int,
    telemetry: CycleTelemetry | None = None,
    rejections_recorded: set[str] | None = None,
) -> tuple[list[dict], list[dict], dict | None]:
    runnable, skipped, metadata_available = _preflight_candidates_with_metadata(
        candidates, derivatives, telemetry=telemetry,
        rejections_recorded=rejections_recorded,
    )
    runnable.sort(key=lambda entry: str(entry.get("asset_type") or "crypto") != "crypto")
    selected = runnable[:limit]
    if derivatives is None:
        return selected, skipped, None
    return selected, skipped, {
        "mode": "futures",
        "requested": limit,
        "screened": len(candidates),
        "executable": len(runnable) if metadata_available else None,
        "selected": len(selected),
        "shortfall": max(0, limit - len(runnable)) if metadata_available else None,
        "pool_exhausted": len(runnable) < limit if metadata_available else None,
        "market_metadata_available": metadata_available,
    }


def _futures_discovery_limit(round_index: int) -> int:
    """Universe size inspected on discovery round ``round_index`` (0 = initial).

    Each refill doubles the inspected pool so lower-ranked contracts can
    surface, capped so one cycle never becomes a full-market sweep.
    """
    widened = AUTO_FUTURES_SCREEN_POOL * (2 ** max(0, round_index))
    return min(widened, AUTO_FUTURES_MAX_UNIVERSE)


def _refill_futures_candidates(
    crypto_universe: Callable,
    stock_universe: Callable,
    screen_universe: Callable,
    config: dict,
    trade_date: str,
    derivatives: Any | None,
    *,
    limit: int,
    selected: list[dict],
    skipped: list[dict],
    selection: dict | None,
    crypto: list[str],
    inspected: int,
    deadline: float | None = None,
    telemetry: CycleTelemetry | None = None,
    mode: AutoMode | None = None,
    authorization: AuthorizationSnapshot | None = None,
) -> tuple[list[str], list[str], list[dict], list[dict], dict | None]:
    """Widen discovery until ``limit`` executable futures exist, or the pool ends.

    The first screen inspects one broad pool, and its top can be all
    contractless pairs, so preflight leaves the cycle short while the LLM
    budget goes unspent. Each refill round re-discovers a wider crypto
    universe and re-screens it, making contracts ranked below the first cut
    reachable. Screening keeps each ticker's own ``asset_type`` and futures
    mode never queries the stock universe, so no stock can enter the futures
    path.

    Rounds MERGE into the already-vetted set and never replace it.
    ``crypto_universe`` refetches the venue on every call (screening.py:138-167),
    so a wider round is a fresh snapshot rather than a superset; replacing would
    drop a contract that already passed preflight and misreport the shortfall.

    Stops when the target is met, when the venue metadata is unavailable (the
    per-candidate path must then decide), when discovery returned fewer
    tickers than requested (the pool is genuinely exhausted), at the round
    cap, or when the remaining cycle budget is spent. The remaining shortfall
    stays in ``selection`` as ``shortfall``/``pool_exhausted``.

    Returns ``(crypto, stocks, selected, skipped, selection)`` so the caller
    reports the discovery that actually produced the cycle's candidates.

    The refill is an optimisation layered on top of a working selection: a
    failed round is reported in ``selection["refill_error"]`` and keeps every
    candidate the earlier rounds already vetted, and each round is capped by the
    remaining cycle budget so it can never outlive the run.
    """
    if selection is None:
        return crypto, [], selected, skipped, selection

    rounds = 0
    requested = inspected
    discovered = len(crypto)
    # Distinct tickers discovery returned across every round: the union is what
    # actually got inspected, so the summary never under-reports it. A ticker can
    # appear in one round's snapshot and vanish from the next (screening.py:138-167).
    inspected_tickers: set[str] = set(crypto)
    stocks: list[str] = []
    vetted: list[dict] = list(selected)
    seen_vetted = {str(entry.get("ticker")) for entry in vetted}
    seen_skipped = {str(entry.get("ticker")) for entry in skipped}
    # The first round already persisted a rejection for every ticker it
    # dropped; a refill re-screens the same pool, so those rows must not be
    # written a second time for the same cycle.
    rejections_recorded = set(seen_skipped)
    metadata_available = selection.get("market_metadata_available")
    screened_total = int(selection.get("screened") or 0)
    refill_error: str | None = None
    while len(vetted) < limit:
        if metadata_available is not True:
            break  # cannot tell contractless from unreachable: fail open
        if discovered < requested:
            break  # discovery offered no wider pool than the one just inspected
        if rounds >= AUTO_FUTURES_REFILL_ROUNDS:
            break
        widened = _futures_discovery_limit(rounds + 1)
        if widened <= requested:
            break  # widening cap reached
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None:
            # A round is optional work: it may spend at most a share of what is
            # left, so the candidates already vetted keep a budget to run in.
            # Without this a round could eat the remainder and the post-refill
            # deadline check would discard a full selection.
            remaining *= AUTO_REFILL_BUDGET_SHARE
            if remaining <= 0:
                break
        autoloop_execution.raise_if_mode_disabled(mode, "screening refill", authorization)
        try:
            round_crypto, stocks, candidates = _screen_with_timeout(
                crypto_universe,
                stock_universe,
                screen_universe,
                config,
                trade_date,
                telemetry=telemetry,
                universe_limit=widened,
                screen_pool=widened,
                timeout_seconds=remaining,
                mode=mode,
                authorization=authorization,
            )
            autoloop_execution.raise_if_mode_disabled(mode, "candidate preflight", authorization)
            round_vetted, round_skipped, metadata_available = (
                _preflight_candidates_with_metadata(
                    candidates, derivatives, telemetry=telemetry,
                    rejections_recorded=rejections_recorded,
                )
            )
        except autoloop_execution.AutoModeDisabled:
            raise
        except Exception as exc:
            # A refill round is optional: report it and keep what round 1
            # vetted instead of failing the whole cycle (or discarding it).
            logger.warning("autoloop refill round %d failed (%s)", rounds + 1, exc)
            refill_error = f"{type(exc).__name__}: {exc}"
            break
        rounds += 1
        crypto = round_crypto
        requested = widened
        discovered = len(crypto)
        inspected_tickers.update(crypto)
        screened_total += len(candidates)
        for entry in round_vetted:
            ticker = str(entry.get("ticker"))
            if ticker not in seen_vetted:
                seen_vetted.add(ticker)
                vetted.append(entry)
        for entry in round_skipped:
            ticker = str(entry.get("ticker"))
            if ticker not in seen_skipped:
                seen_skipped.add(ticker)
                skipped.append(entry)
    selected = vetted[:limit]
    selection.update({
        "screened": screened_total,
        "executable": len(vetted) if metadata_available else None,
        "selected": len(selected),
        "shortfall": max(0, limit - len(vetted)) if metadata_available else None,
        "pool_exhausted": len(vetted) < limit if metadata_available else None,
        "market_metadata_available": metadata_available,
        "refill_rounds": rounds,
        "discovery_universe": requested,
        "discovery_pool": discovered,
        "discovery_inspected": len(inspected_tickers),
    })
    if refill_error is not None:
        selection["refill_error"] = refill_error
    return crypto, stocks, selected, skipped, selection


def _futures_crypto_universe(derivatives: Any, contracts: dict, summary: dict) -> Callable:
    """Universe builder for futures mode, run inside the screening timeout.

    Reads which settle assets the futures wallet holds (intersected with
    exec_futures_settle_currencies), then the liquid perpetuals settled in
    them. Fills ``contracts`` (ticker -> contract row) for pinning afterwards.
    Called once per discovery round, so a refill round widens it too.
    """
    from tradingagents.dataflows.screening import (
        _public_futures_ranking_client,
        futures_universe,
    )
    from tradingagents.execution.futures_account import read_futures_settle_balances

    def build(config: dict, top_n: int) -> list[str]:
        bridge = derivatives._bridge
        balances = read_futures_settle_balances(bridge)
        settles = bridge.futures_settle_currencies() & set(balances)
        summary["settle_assets"] = sorted(settles)
        if not settles:
            logger.warning("autoloop: futures wallet holds no allowed settle asset (%s)", balances)
            return []
        try:
            ranking = _public_futures_ranking_client(config)
        except Exception as exc:  # noqa: BLE001 - fail closed, see below
            # Demo Trading reports synthetic volumes, so ranking on the venue
            # would rank by fake liquidity. Without the production ranking
            # there is no trustworthy universe; skip the cycle instead.
            logger.warning(
                "autoloop: production volume ranking unavailable (%s); "
                "futures universe skipped", exc,
            )
            summary["universe_error"] = f"ranking_unavailable: {exc}"
            return []
        # ``top_n`` is the discovery size: the initial pool, then each refill
        # round's widened pool (_futures_discovery_limit).
        rows = futures_universe(bridge.client(), settles, top_n, ranking_client=ranking)
        contracts.update({row["ticker"]: row for row in rows})
        return [row["ticker"] for row in rows]

    return build


def _pinning_screen(
    screen_universe: Callable,
    derivatives: Any,
    contracts: dict[str, dict],
    mismatched: dict[str, dict],
    *,
    max_mismatch: float,
    price_mismatch: Callable,
) -> Callable:
    """Wrap ``screen_universe`` so every discovery round pins its picks.

    Each screened candidate with a known contract is checked against it: a
    Yahoo close more than ``max_mismatch`` off the contract price means the
    ``BASE-USD`` series is another asset sharing the ticker (analysts and
    brackets would read the wrong market), so it is dropped into
    ``mismatched``. The rest are pinned to their exact contract (for example
    ``SOL/USDC:USDC``) before preflight resolves them.
    """

    try:
        parameters = inspect.signature(screen_universe).parameters
    except (TypeError, ValueError):
        parameters = {}
    forwards_cancel = "cancel_event" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )

    def screen(crypto, stocks, trade_date, pool_limit, *, cancel_event=None) -> list[dict]:
        if forwards_cancel:
            candidates = screen_universe(
                crypto, stocks, trade_date, pool_limit, cancel_event=cancel_event,
            )
        else:
            candidates = screen_universe(crypto, stocks, trade_date, pool_limit)
        kept: list[dict] = []
        for candidate in candidates:
            ticker = str(candidate.get("ticker"))
            contract = contracts.get(ticker)
            if contract is None:
                kept.append(candidate)
                continue
            gap = price_mismatch(candidate.get("last_close"), contract.get("last_price"))
            if gap is not None and gap > max_mismatch:
                mismatched[ticker] = {
                    "ticker": ticker,
                    "reason": f"price data mismatch {gap:.0%} vs {contract['futures_symbol']}",
                }
                continue
            candidate["futures_symbol"] = contract["futures_symbol"]
            candidate["settle"] = contract["settle"]
            derivatives._bridge.pin_futures_contract(ticker, contract["futures_symbol"])
            kept.append(candidate)
        return kept

    return screen
