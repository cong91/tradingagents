"""Background shadow worker: observations that cannot reach an order.

A worker of its own, independent of the autoloop (plan §6.4). It reads terminal
``futures_cycles`` rows that are already durable, rebuilds the closed-candle
context as it stood at the decision moment, asks each configured provider for an
observation, and stores the result together with its eventual evaluation.

The bounds that make "shadow" mean something:

- **no decision path.** This module is never imported by ``server/autoloop.py``,
  ``tradingagents/graph`` or ``tradingagents/agents`` (a test pins it), and it
  never sends an order, never touches risk limits, never writes cycle state. The
  only tables it writes are ``shadow_*``;
- **off by default.** ``shadow_observations_enabled`` gates the worker entirely;
  while off, the loop returns immediately and the incumbent behaviour of every
  other component is exactly what it was;
- **bounded work.** One poll handles at most ``SHADOW_BATCH_SIZE`` cycles and one
  evaluation batch, each provider call has its own deadline, and any provider
  failure (unavailable, error, timeout) is stored as that status — it never
  raises into the poll loop and never blocks the scheduler;
- **restart catch-up.** The cursor is the last processed ``(updated_at,
  cycle_id)`` pair, persisted in ``shadow_state``, so cycles completed while the
  worker was down are covered on the next poll and nothing is silently skipped;
- **no look-ahead.** The candle series handed to a provider is cut at the
  decision moment (``as_of``); a retrospective replay is labelled
  ``retrospective`` by the recorder rather than presented as forward.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from server import shadow_store
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.market_data import Candle
from tradingagents.shadow.evaluation import evaluate_forecast
from tradingagents.shadow.provider import (
    ForecastAttempt,
    ForecastProvider,
    ForecastRequest,
    stable_seed,
)
from tradingagents.shadow.recorder import (
    RequestError,
    build_observation,
    merge_candles,
    validate_candles,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_BATCH_SIZE = shadow_store.DEFAULT_BATCH_SIZE
# How long stop() waits for the worker thread. Deliberately a short constant, not
# the poll interval: joining for a whole interval would block shutdown for minutes.
SHUTDOWN_GRACE_SECONDS = 10.0
# A cycle whose observation raises is retried on the next poll up to this many
# attempts; past the bound the failure is logged once and the cursor moves on so
# a poison cycle can never stall the worker forever.
MAX_CYCLE_ATTEMPTS = 2

# The timeframe an observation is made on. 1h is the resolution the runtime's
# confirmation path reads and the one with enough history for Kronos' context.
OBSERVATION_TIMEFRAME = "1h"
OBSERVATION_HORIZON_HOURS = 24
OBSERVATION_LOOKBACK_BARS = 128

# Historical closed candles per symbol, cutoff at the decision moment.
CandleSource = Callable[[str, str, int, int], list[Candle]]
ProviderFactory = Callable[[], Sequence[ForecastProvider]]
# The persistence step of one observation; injectable so the no-database property
# (a provider failure is data, never an exception) is testable on its own.
RecordForecast = Callable[[dict[str, Any]], bool]

_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def _config_value(key: str, default: Any) -> Any:
    try:
        return get_config().get(key, default)
    except Exception:  # a broken config read must not take the worker down
        return default


def observations_enabled() -> bool:
    return _config_value("shadow_observations_enabled", False) is True


def interval_seconds() -> float:
    value = _config_value("shadow_observations_interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_INTERVAL_SECONDS)
    return number if number > 0 else float(DEFAULT_INTERVAL_SECONDS)


def providers() -> list[str]:
    """Configured provider ids; the filter is always available, the model is not."""
    configured = _config_value("shadow_providers", "filter:donchian_breakout")
    if isinstance(configured, str):
        return [name.strip() for name in configured.split(",") if name.strip()]
    if isinstance(configured, (list, tuple)):
        return [str(name) for name in configured]
    return ["filter:donchian_breakout"]


def _default_provider_factory() -> Sequence[ForecastProvider]:
    """Build the configured providers, skipping (loudly) any that is not installed.

    A missing optional model is NOT an error for the worker: the plan requires
    the module to run without it, so an absent provider is dropped here and the
    dashboard reports ``unavailable`` from the config side instead of fabricating
    observations.
    """
    from tradingagents.shadow.filters import FilterProvider
    from tradingagents.shadow.kronos import KronosProvider

    built: list[ForecastProvider] = []
    for name in providers():
        if name.startswith("filter:"):
            try:
                built.append(FilterProvider(name.split(":", 1)[1]))
            except ValueError as exc:
                logger.warning("shadow: skipping provider %r: %s", name, exc)
            continue
        if name == "kronos":
            provider = KronosProvider()
            problem = provider.unavailable_reason()
            if problem is not None:
                logger.info("shadow: kronos unavailable: %s", problem)
                continue
            built.append(provider)
            continue
        logger.warning("shadow: unknown provider %r skipped", name)
    return built


def _parse_iso(value: Any) -> datetime | None:
    """A recorded moment as a tz-aware datetime; psycopg hands back datetimes."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decision_moment(cycle: dict[str, Any], payload: dict[str, Any]) -> datetime:
    """The moment the runtime decided for this cycle, from recorded timestamps.

    Preference order is the runtime's own phase timestamps, then the row's
    creation time. A cycle without any recorded moment cannot be observed: using
    "now" would turn a historical cycle into a forward forecast it never was.
    """
    timestamps = payload.get("phase_timestamps")
    if isinstance(timestamps, dict):
        for key in ("execution_finished_at", "execution_started_at", "entry_finished_at"):
            parsed = _parse_iso(timestamps.get(key))
            if parsed is not None:
                return parsed
    parsed = _parse_iso(cycle.get("updated_at")) or _parse_iso(cycle.get("created_at"))
    if parsed is None:
        raise RequestError("the cycle records no decision timestamp")
    return parsed


def _snapshot_candles(payload: dict[str, Any], symbol: str) -> list[Candle]:
    """Every closed candle the cycle snapshot carries — its 1h AND 4h pair.

    A production snapshot always records both timeframes (autoloop's futures
    context -> ``snapshot_payload``), so callers must narrow to the timeframe they
    observe before validating. Returning both is deliberate: the 4h bar is real
    recorded context, and a caller that wants it (or wants to prove the two agree)
    should not have to re-parse the payload.
    """
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    candles: list[Candle] = []
    for key in ("one_hour", "four_hour"):
        raw = snapshot.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            candles.append(Candle(
                symbol=str(raw.get("symbol") or symbol),
                timeframe=str(raw.get("timeframe")),
                open_time=int(raw["open_time"]),
                close_time=int(raw["close_time"]),
                open=float(raw["open"]),
                high=float(raw["high"]),
                low=float(raw["low"]),
                close=float(raw["close"]),
                volume=float(raw.get("volume") or 0.0),
                is_closed=bool(raw.get("is_closed", True)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return candles


def _venue_candle_source(symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list[Candle]:
    """READ-ONLY closed candles from the configured venue, cut at ``until_ms``.

    The same reader the replay harness uses (``fetch_closed_candles``): it drops
    every candle whose close is after the cutoff, so the series can never contain
    a bar that had not closed at the decision moment. Nothing is sent, nothing is
    subscribed; this is a market-data read like any other.
    """
    from tradingagents.dataflows.market_data import fetch_closed_candles
    from tradingagents.execution.derivatives import DerivativesExecutor

    executor = DerivativesExecutor(config=get_config())
    bridge = executor._bridge  # the bridge is the executor's venue reader
    as_of = datetime.fromtimestamp(until_ms / 1000, UTC)
    lookback = max(1, (until_ms - since_ms) // _timeframe_ms(timeframe))
    candles = fetch_closed_candles(
        bridge.client(), symbol, timeframe, as_of=as_of, limit=min(int(lookback), 1000),
    )
    return [candle for candle in candles if candle.open_time >= since_ms]


def _timeframe_ms(timeframe: str) -> int:
    from tradingagents.dataflows.market_data import timeframe_ms

    return timeframe_ms(timeframe)


def _series_for(
    cycle: dict[str, Any], payload: dict[str, Any], *, candle_source: CandleSource | None,
    timeframe: str, as_of: datetime,
) -> tuple[Candle, ...]:
    """The contiguous closed series an observation is built on.

    A cycle snapshot records BOTH decision timeframes (1h and 4h); an observation
    is made on ONE of them. So the snapshot's contribution is narrowed to
    ``timeframe`` FIRST — feeding both into the contiguous-series check would
    refuse every production cycle as "mixed timeframes", which is the blocker this
    narrowing exists to prevent (a probe of the real export: 77/77 snapshots carry
    both, so without this no observation could ever be recorded).

    A provider that needs more context (Kronos needs a real history) reads further
    back through the injected source, always with the decision moment as the
    cutoff. The two sources overlap by construction — the snapshot's own candle is
    also in the venue history — so bars are merged by (timeframe, open time), and
    a bar present in both is accepted only when the two copies agree exactly. A
    disagreement is refused: silently preferring one would invent which prices the
    decision saw.
    """
    symbol = str(cycle.get("symbol") or "")
    cutoff_ms = int(as_of.timestamp() * 1000)
    horizon_ms = OBSERVATION_LOOKBACK_BARS * _timeframe_ms(timeframe)
    source = candle_source
    history: list[Candle] = []
    if source is not None:
        try:
            history = list(source(symbol, timeframe, cutoff_ms - horizon_ms, cutoff_ms))
        except Exception as exc:  # a failed history read is reported, not fatal
            logger.info("shadow: candle history read failed for %s: %r", symbol, exc)
    snapshot = _candles_at_timeframe(_snapshot_candles(payload, symbol), timeframe)
    merged = merge_candles(snapshot, tuple(history))
    return validate_candles(merged, timeframe, as_of)


def _candles_at_timeframe(candles: list[Candle], timeframe: str) -> tuple[Candle, ...]:
    """The subset of a mixed snapshot that belongs to the observed timeframe."""
    return tuple(candle for candle in candles if candle.timeframe == timeframe)


def _provenance(cycle: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """What the cycle itself recorded; never filled in with present-day values."""
    provenance: dict[str, Any] = {
        "cycle_id": cycle.get("cycle_id"),
        "strategy_version": cycle.get("strategy_version"),
        "cycle_status": cycle.get("status"),
        "cycle_phase": cycle.get("phase"),
    }
    git_sha = payload.get("git_sha")
    if isinstance(git_sha, str) and git_sha:
        provenance["git_sha"] = git_sha
    else:
        provenance["git_sha"] = None
        provenance["git_sha_note"] = "the cycle row recorded no git revision"
    run_settings = payload.get("run_settings")
    if isinstance(run_settings, dict):
        provenance["run_settings"] = run_settings
    else:
        provenance["run_settings_note"] = "the cycle row recorded no run settings"
    return provenance


def _observe_cycle(
    cycle: dict[str, Any],
    *,
    built: Sequence[ForecastProvider],
    candle_source: CandleSource | None,
    now: datetime,
    record: RecordForecast | None = None,
) -> list[dict[str, Any]]:
    """Observe one terminal cycle with every provider; returns per-provider results.

    ``record`` is the persistence step, injectable so the property that matters —
    a provider failure is data and never escapes — can be exercised without a
    database. Conflating the two once hid that: "a provider bug is harmless" was
    only true while PostgreSQL happened to be up, because the store call sat in the
    same unwrapped sequence as the forecast.
    """
    store = record or shadow_store.record_forecast
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    symbol = str(cycle.get("symbol") or "")
    if not symbol:
        return [{"result": "skipped_no_symbol"}]
    as_of = _decision_moment(cycle, payload)
    execution_status = payload.get("execution_status")
    try:
        candles = _series_for(
            cycle, payload, candle_source=candle_source,
            timeframe=OBSERVATION_TIMEFRAME, as_of=as_of,
        )
    except RequestError as exc:
        return [{"result": f"unusable_series:{exc}"}]
    provenance = _provenance(cycle, payload)
    results: list[dict[str, Any]] = []
    for provider in built:
        request = ForecastRequest(
            symbol=symbol,
            timeframe=OBSERVATION_TIMEFRAME,
            horizon_hours=OBSERVATION_HORIZON_HOURS,
            candles=candles,
            as_of=as_of.isoformat(),
            seed=_seed_for(cycle, provider.provider_id),
        )
        try:
            attempt = provider.forecast(request)
        except Exception as exc:  # a provider bug is data, never a crashed poll
            logger.warning(
                "shadow: provider %s raised for %s: %r", provider.provider_id, symbol, exc,
            )
            attempt = ForecastAttempt(
                status="error",
                reason=f"provider raised {type(exc).__name__}: {exc}",
            )
        row = build_observation(
            provider=provider, request=request, attempt=attempt,
            prediction_created_at=now, decision_moment=as_of,
            cycle_id=str(cycle.get("cycle_id")),
            execution_status=execution_status if isinstance(execution_status, str) else None,
            provenance=provenance,
        )
        stored = store(row)
        results.append({
            "provider": provider.provider_id,
            "status": attempt.status,
            "stored": stored,
        })
    return results


def _seed_for(cycle: dict[str, Any], provider_id: str) -> int:
    """A stable seed per (cycle, provider) so a re-run draws the same paths."""
    return stable_seed(str(cycle.get("cycle_id")), provider_id)


def _evaluate_pending(
    *, candle_source: CandleSource | None, batch_size: int, now: datetime,
) -> list[dict[str, Any]]:
    """Score observations whose horizon should have realized by ``now``."""
    results: list[dict[str, Any]] = []
    for forecast in shadow_store.forecasts_awaiting_evaluation(batch_size=batch_size):
        cutoff = _parse_iso(forecast.get("data_open_time"))
        if cutoff is None:
            results.append({"forecast_id": forecast["forecast_id"], "result": "no_cutoff"})
            continue
        realized_from_ms = int(cutoff.timestamp() * 1000) + _timeframe_ms(str(forecast["timeframe"]))
        horizon_ms = int(forecast["horizon_hours"]) * 3_600_000
        if int(now.timestamp() * 1000) < realized_from_ms + horizon_ms:
            continue  # the outcome has not happened yet; nothing to score
        if candle_source is None:
            results.append({"forecast_id": forecast["forecast_id"], "result": "no_candle_source"})
            continue
        try:
            candles = candle_source(
                str(forecast["symbol"]), str(forecast["timeframe"]),
                realized_from_ms, realized_from_ms + horizon_ms + _timeframe_ms(str(forecast["timeframe"])),
            )
        except Exception as exc:
            results.append({
                "forecast_id": forecast["forecast_id"], "result": f"read_error:{exc!r}",
            })
            continue
        evaluation = evaluate_forecast(
            _as_stored_expectation(forecast), candles,
            net_r=None,
            net_r_reason=(
                "the shadow worker does not read the ledger; net R is claimed only "
                "by an explicit ledger join"
            ),
        )
        stored = shadow_store.record_evaluation(forecast["forecast_id"], evaluation)
        results.append({
            "forecast_id": forecast["forecast_id"],
            "status": evaluation["status"],
            "stored": stored,
        })
    return results


def _as_stored_expectation(forecast: dict[str, Any]) -> dict[str, Any]:
    """The stored row, in the shape the evaluator reads (band/samples unwrapped)."""
    row = dict(forecast)
    samples = row.get("samples")
    if isinstance(samples, str):
        import json

        try:
            row["samples"] = json.loads(samples)
        except ValueError:
            row["samples"] = {}
    return row


def poll_once(
    *,
    provider_factory: ProviderFactory | None = None,
    candle_source: CandleSource | None = None,
    record: RecordForecast | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
    evaluate: bool = True,
) -> dict[str, Any]:
    """One bounded poll; independent of the autoloop and of the scheduler.

    Disabled by config: nothing is read, nothing is written, and every other
    component keeps the behaviour it had. Enabled: a stable (updated_at,
    cycle_id) cursor advances over the batch, and one cycle failing never stops
    the poll.

    ``record`` lets a caller supply the persistence step (default: the shadow
    store). That seam is what makes "a failing provider cannot take the poll down"
    provable without a database, instead of only true while PostgreSQL is up.
    """
    if not observations_enabled():
        return {"status": "disabled"}
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    built = list((provider_factory or _default_provider_factory)())
    if not built:
        return {
            "status": "no_provider",
            "reason": "no shadow provider is available in this environment",
        }
    source = candle_source
    if source is None:
        source = _venue_candle_source
    cursor = shadow_store.get_cursor()
    if cursor is None:
        shadow_store.set_cursor(f"{moment.isoformat()}|")
        return {"status": "initialized", "note": "observations start now; no backfill"}
    cursor_updated_at, _, cursor_cycle_id = cursor.partition("|")
    batch = shadow_store.terminal_cycles_after(
        cursor_updated_at or None, cursor_cycle_id or None, batch_size=batch_size,
    )
    outcomes: list[dict[str, Any]] = []
    blocked_index: int | None = None
    for index, cycle in enumerate(batch):
        cycle_id = str(cycle.get("cycle_id"))
        try:
            results = _observe_cycle(
                cycle, built=built, candle_source=source, now=moment, record=record,
            )
            outcomes.append({"cycle_id": cycle_id, "results": results})
            shadow_store.clear_state(f"shadow-failed:{cycle_id}")
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the poll
            logger.warning("shadow worker failed to observe %s: %r", cycle_id, exc)
            outcomes.append({"cycle_id": cycle_id, "result": f"error:{type(exc).__name__}"})
            attempts = _bump_attempts(cycle_id)
            if attempts < MAX_CYCLE_ATTEMPTS:
                blocked_index = index
                break
            shadow_store.clear_state(f"shadow-failed:{cycle_id}")
    if batch and blocked_index != 0:
        last = batch[-1 if blocked_index is None else blocked_index - 1]
        shadow_store.set_cursor(f"{last['updated_at']}|{last['cycle_id']}")
    evaluated = _evaluate_pending(
        candle_source=source, batch_size=batch_size, now=moment,
    ) if evaluate else []
    return {
        "status": "polled",
        "processed": len(batch),
        "outcomes": outcomes,
        "evaluated": evaluated,
    }


def _bump_attempts(cycle_id: str) -> int:
    key = f"shadow-failed:{cycle_id}"
    raw = shadow_store.get_cursor(key)
    try:
        attempts = int(raw) + 1
    except (TypeError, ValueError):
        attempts = 1
    shadow_store.set_cursor(str(attempts), key=key)
    return attempts


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            poll_once()
        except Exception:
            logger.exception("shadow worker poll failed")
        if _stop_event.wait(interval_seconds()):
            break


def start() -> None:
    """Start the worker thread (composition-root wiring); no-op when disabled."""
    global _worker_thread
    if not observations_enabled():
        logger.info("shadow observations are disabled; worker not started")
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="server-shadow-worker", daemon=True,
    )
    _worker_thread.start()


def stop() -> None:
    """Signal the worker and wait a short, bounded time for it to finish.

    The wait is ``SHUTDOWN_GRACE_SECONDS``, not the poll interval: a 300 s interval
    would have made server shutdown block for five minutes. The thread is a daemon
    and every step of a poll is bounded (provider deadlines, one batch), so a
    thread still running after the grace period is reported rather than waited on
    — an unbounded join is what turns a slow stop into a hung process.
    """
    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
        if _worker_thread.is_alive():
            logger.warning(
                "shadow worker did not stop within %.0fs; leaving it to its daemon "
                "status (the process is exiting)", SHUTDOWN_GRACE_SECONDS,
            )
        else:
            _worker_thread = None


__all__ = [
    "CandleSource",
    "MAX_CYCLE_ATTEMPTS",
    "OBSERVATION_HORIZON_HOURS",
    "OBSERVATION_LOOKBACK_BARS",
    "OBSERVATION_TIMEFRAME",
    "RecordForecast",
    "SHUTDOWN_GRACE_SECONDS",
    "interval_seconds",
    "observations_enabled",
    "poll_once",
    "providers",
    "start",
    "stop",
]
