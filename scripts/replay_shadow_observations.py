"""Offline replay of shadow observations over recorded cycles (plan §6.5).

Feeds the exported ``futures_cycles`` rows through the shadow layer exactly as
the worker would have, then reports what the observations would have looked like
— with every honesty gate visibly on:

- a provider only ever sees CLOSED candles that had already closed at the
  decision moment, and only the most recent CONTIGUOUS run of them: a gap is
  never bridged, and the recorded export's missing cycles simply shorten the
  history rather than being interpolated;
- every row produced here is ``retrospective`` (the replay runs long after the
  decisions) and the report carries that count so nobody reads it as forward
  accuracy;
- deterministic baselines are reported as verdict COUNTS, never as rates;
- a provider that needs the optional model and does not find it is reported as
  ``unavailable``, never simulated; when the history is too short for a
  provider it says so with the count it got;
- nothing here is a profit claim: the report carries the disclaimer, and no
  realized-outcome evaluation is computed because the export has no candles
  after the decision.

Usage:
    .venv/Scripts/python.exe scripts/replay_shadow_observations.py \\
        --fixture tests/fixtures/futures_cycles_replay.json \\
        --provider filter:donchian_breakout [--provider kronos] \\
        [--candles exported_candles.json] [--max-cycles 200] [--report out.json]

``--candles`` is optional: a JSON array of closed candles
(``{"symbol", "timeframe", "open_time", "close_time", "open", "high", "low",
"close", "volume"}``) such as an operator export. Without it only the cycle
snapshots feed the providers, which is usually one candle per cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

DISCLAIMER = (
    "Offline shadow replay over recorded cycles. Observations are research data: "
    "they never reach an order, a risk limit or a protection decision, and a "
    "sample ratio is not a calibrated probability. Research framework — not "
    "financial advice."
)


def _build_provider(name: str) -> tuple[Any, str | None]:
    """The provider, or (None, reason) when it cannot run here."""
    from tradingagents.shadow.filters import FilterProvider

    if name.startswith("filter:"):
        try:
            return FilterProvider(name.split(":", 1)[1]), None
        except ValueError as exc:
            return None, str(exc)
    if name == "kronos":
        from tradingagents.shadow.kronos import KronosProvider

        provider = KronosProvider()
        problem = provider.unavailable_reason()
        return (None, problem) if problem else (provider, None)
    return None, f"unknown provider {name!r}"


def _execution_status(cycle: dict[str, Any]) -> str | None:
    """The recorded execution status, from a nested payload or the flat export.

    The export carries ``status``/``phase`` instead; those are mapped to the same
    three groups (a completed entry cycle is ``executed``, any rejection is
    ``rejected``, a market/confirmation rejection is ``abstained``) rather than
    every row being reported as "abstained" by default.
    """
    payload = _payload_of(cycle)
    status = payload.get("execution_status")
    if isinstance(status, str) and status:
        return status
    recorded = str(cycle.get("status") or "")
    phase = str(cycle.get("phase") or "")
    if recorded == "completed" and phase == "entry":
        return "executed"
    if recorded == "rejected":
        return "market_rejected" if phase == "market" else "confirmation_rejected" if phase == "confirmation" else "not_executed"
    if recorded in ("failed", "interrupted"):
        return None  # a pipeline failure is not a decision; recorded as no status
    return None


def _payload_of(cycle: dict[str, Any]) -> dict[str, Any]:
    """The cycle's payload, adapting the flat export shape.

    The exported fixture keeps ``snapshot``/``execution_status`` at the row's top
    level rather than nested under ``payload``; both shapes name the same fields,
    so one mapping serves them and no field is invented.
    """
    payload = cycle.get("payload")
    if isinstance(payload, dict):
        return payload
    adapted: dict[str, Any] = {}
    for key in ("snapshot", "execution_status", "phase_timestamps", "git_sha", "run_settings"):
        if key in cycle:
            adapted[key] = cycle[key]
    return adapted


def _decision_moment(cycle: dict[str, Any]) -> datetime | None:
    """The cycle's own recorded moment; never the replay's clock.

    Preference order:
    1. a recorded phase/row timestamp (what the worker uses);
    2. otherwise the CYCLE IDENTITY, which names the candles the cycle decided
       on (``futures:<symbol>:<4h ISO>:<1h ISO>:<strategy>``). The earliest
       moment that decision could have existed is when its newest candle closed,
       so that instant is used. It is derived from recorded identity, not
       invented, and it can only be conservative: no candle closing after it is
       ever shown to a provider.
    """
    from server.shadow_worker import _decision_moment as read_moment
    from tradingagents.shadow.recorder import RequestError, moment_from_last_close

    payload = _payload_of(cycle)
    try:
        return read_moment(cycle, payload)
    except RequestError:
        pass
    return moment_from_last_close(_candles_from_snapshot(cycle))


def _candles_from_snapshot(cycle: dict[str, Any]) -> list[Any]:
    from server.shadow_worker import _snapshot_candles

    return _snapshot_candles(_payload_of(cycle), str(cycle.get("symbol") or ""))


def load_candles(path: Path | None) -> dict[str, list[Any]]:
    """Closed candles per symbol from an operator export; empty when not given."""
    from tradingagents.dataflows.market_data import Candle

    if path is None:
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[str, list[Candle]] = {}
    for raw in rows:
        try:
            candle = Candle(
                symbol=str(raw["symbol"]), timeframe=str(raw["timeframe"]),
                open_time=int(raw["open_time"]), close_time=int(raw["close_time"]),
                open=float(raw["open"]), high=float(raw["high"]),
                low=float(raw["low"]), close=float(raw["close"]),
                volume=float(raw.get("volume") or 0.0),
                is_closed=bool(raw.get("is_closed", True)),
            )
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(candle.symbol, []).append(candle)
    return grouped


def contiguous_tail(
    candles: list[Any], as_of: datetime, timeframe: str, *, max_bars: int,
) -> list[Any]:
    """The newest contiguous run of closed candles ending at or before ``as_of``.

    A gap is a hard boundary: the tail starts after the most recent one, so no
    provider ever receives a series whose holes were filled in. This is the only
    honest way to use a recorded export whose cycles are missing.
    """
    from tradingagents.dataflows.market_data import timeframe_ms

    cutoff_ms = int(as_of.astimezone(UTC).timestamp() * 1000)
    duration = timeframe_ms(timeframe)
    relevant = sorted(
        (
            candle for candle in candles
            if candle.timeframe == timeframe and candle.is_closed
            and candle.close_time <= cutoff_ms
            and candle.close_time - candle.open_time == duration
        ),
        key=lambda candle: candle.open_time,
    )
    if not relevant:
        return []
    tail = [relevant[-1]]
    for candle in reversed(relevant[:-1]):
        if candle.close_time != tail[0].open_time:
            break
        tail.insert(0, candle)
    return tail[-max_bars:]


def replay(
    rows: list[dict[str, Any]],
    provider_names: list[str],
    *,
    timeframe: str,
    horizon_hours: int,
    history: dict[str, list[Any]] | None = None,
    max_cycles: int | None = None,
    max_bars: int = 128,
) -> dict[str, Any]:
    """Run each provider over every cycle that carries a usable decision moment."""
    from tradingagents.shadow.provider import (
        STATUS_OK,
        ForecastAttempt,
        ForecastRequest,
        stable_seed,
    )
    from tradingagents.shadow.recorder import (
        RequestError,
        build_observation,
        merge_candles,
        validate_candles,
    )

    providers = []
    unavailable: dict[str, str] = {}
    for name in provider_names:
        provider, reason = _build_provider(name)
        if provider is None:
            unavailable[name] = reason or "not available"
        else:
            providers.append(provider)

    history = history or {}
    cycles_seen = cycles_with_snapshot = cycles_with_history = cycles_observed = 0
    history_bars: Counter[int] = Counter()
    rows_out: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    now = datetime.now(UTC)
    for cycle in rows:
        if max_cycles is not None and cycles_seen >= max_cycles:
            break
        cycles_seen += 1
        symbol = str(cycle.get("symbol") or "")
        snapshot_candles = _candles_from_snapshot(cycle)
        if not snapshot_candles:
            skipped["no_snapshot"] += 1
            continue
        cycles_with_snapshot += 1
        moment = _decision_moment(cycle)
        if moment is None:
            skipped["no_decision_moment"] += 1
            continue
        # Snapshot first (the decision's own prices win), export appended for the
        # context a model needs. Both are real closed candles; an identical
        # duplicate collapses, a disagreement is refused downstream.
        pool = list(history.get(symbol, [])) + snapshot_candles
        try:
            deduped = merge_candles(tuple(pool))
        except RequestError as exc:
            skipped[f"conflicting_candles:{exc}"] += 1
            continue
        tail = contiguous_tail(list(deduped), moment, timeframe, max_bars=max_bars)
        if not tail:
            skipped["no_closed_candles_at_the_decision_moment"] += 1
            continue
        cycles_with_history += 1
        history_bars[len(tail)] += 1
        try:
            candles = validate_candles(tail, timeframe, moment)
        except RequestError as exc:
            skipped[f"unusable_series:{exc}"] += 1
            continue
        execution_status = _execution_status(cycle)
        observed_here = False
        for provider in providers:
            request = ForecastRequest(
                symbol=symbol, timeframe=timeframe,
                horizon_hours=horizon_hours, candles=candles,
                as_of=moment.isoformat(),
                # The worker's own seed derivation: SHA-256 based, so two replay
                # processes draw identical paths for the same observation. A
                # builtin hash() here would change every run and make a replay
                # impossible to reproduce (probe: 158156885 vs 991776861).
                seed=stable_seed(str(cycle.get("cycle_id")), provider.provider_id),
            )
            try:
                attempt = provider.forecast(request)
            except Exception as exc:  # a provider bug is data, never a crash
                attempt = ForecastAttempt(
                    status="error", reason=f"provider raised {type(exc).__name__}: {exc}",
                )
            row = build_observation(
                provider=provider, request=request, attempt=attempt,
                prediction_created_at=now, decision_moment=moment,
                cycle_id=cycle.get("cycle_id"),
                execution_status=execution_status if isinstance(execution_status, str) else None,
                provenance={
                    "source": "replay_shadow_observations",
                    "replay_note": "produced well after the decision; retrospective by construction",
                },
            )
            row.pop("forecast_id", None)
            rows_out.append(row)
            observed_here = observed_here or row["status"] == STATUS_OK
        cycles_observed += observed_here

    status_counts = Counter(row["status"] for row in rows_out)
    generation_counts = Counter(row["generation_kind"] for row in rows_out)
    group_counts = Counter(row["outcome_group"] for row in rows_out)
    verdicts: dict[str, dict[str, int]] = {}
    reasons: dict[str, Counter[str]] = {}
    for row in rows_out:
        if row["status"] != STATUS_OK:
            first_line = (row.get("reason") or "no reason recorded").splitlines()[0]
            reasons.setdefault(row["provider"], Counter())[first_line] += 1
            continue
        bucket = verdicts.setdefault(row["provider"], {"up": 0, "down": 0, "flat": 0})
        if row.get("direction") in bucket:
            bucket[row["direction"]] += 1
    return {
        "kind": "shadow_observation_replay",
        "generated_at": now.isoformat(),
        "timeframe": timeframe,
        "horizon_hours": horizon_hours,
        "providers_requested": provider_names,
        "providers_available": [provider.provider_id for provider in providers],
        "providers_unavailable": unavailable,
        "cycles_seen": cycles_seen,
        "cycles_with_snapshot": cycles_with_snapshot,
        "cycles_with_a_closed_candle_tail": cycles_with_history,
        "cycles_with_an_ok_observation": cycles_observed,
        # Keyed by tail length: how many cycles got a history of N closed bars.
        # A cycle whose history is short (a gap, or a sparse export) is visible
        # here instead of being averaged into a single misleading number.
        "cycles_by_closed_candle_count": {
            str(k): v for k, v in sorted(history_bars.items())
        },
        "observations": len(rows_out),
        "status_counts": dict(status_counts),
        "generation_counts": dict(generation_counts),
        "outcome_group_counts": dict(group_counts),
        "verdict_counts": verdicts,
        "failure_reasons": {name: dict(counter) for name, counter in reasons.items()},
        "skipped": dict(skipped),
        "limitations": [
            "A provider sees only the most recent CONTIGUOUS run of real closed "
            "candles at each decision moment; a gap ends the history rather than "
            "being bridged, so many recorded cycles carry too few bars.",
            "Every observation here is retrospective: it was created long after "
            "the first predicted bar closed. These are not forward accuracy figures.",
            "Verdicts are COUNTS of one deterministic verdict per observation (or the "
            "majority direction of sampled paths); with a handful of cycles they are "
            "not a rate and carry no confidence interval.",
            "No realized-outcome evaluation is computed: the export carries no candles "
            "after the decision, so exits, band coverage and net R stay unavailable.",
        ],
        "disclaimer": DISCLAIMER,
        "rows": rows_out,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline shadow-observation replay over recorded futures cycles",
    )
    parser.add_argument("--fixture", required=True,
                        help="exported futures_cycles JSON")
    parser.add_argument("--provider", action="append", default=None,
                        help="provider id; repeatable (filter:<name> or kronos)")
    parser.add_argument("--candles", default=None,
                        help="optional exported closed-candle JSON, grouped by symbol")
    parser.add_argument("--timeframe", default="1h", choices=("1h", "4h"))
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--max-bars", type=int, default=128)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--report", default=None, help="write JSON here (default stdout)")
    args = parser.parse_args(argv)

    providers = args.provider or ["filter:donchian_breakout"]
    rows = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    history = load_candles(Path(args.candles) if args.candles else None)
    report = replay(
        rows, providers, timeframe=args.timeframe,
        horizon_hours=args.horizon_hours, history=history,
        max_cycles=args.max_cycles, max_bars=args.max_bars,
    )
    payload = json.dumps(report, indent=1, default=str)
    if args.report:
        Path(args.report).write_text(payload, encoding="utf-8")
        print(f"wrote {args.report} ({report['observations']} observations)")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
