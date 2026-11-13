"""Baseline variants: every variant runs the SAME simulation (plan §4.1).

One concern: build the ``ProposalSource`` for each registered baseline so all
variants — no-trade, a simple deterministic rule, the recorded bot behavior,
and a single-knob ablation — are evaluated by the identical engine, cost
model, fill rules and funding series. Differences between variants come only
from their proposals, never from a different pipeline.

- ``no_trade``: proposes nothing. Its result is a cash baseline only when the
  recorded source and financial inputs have sufficient coverage.
- ``rule``: a real confirmation rule from ``server.futures_replay.RULES`` over
  a recorded cycle context, turned into an engine ``Proposal``. The rule is
  imported, never forked: a drift in the production rule shows up here too.
- ``recorded``: the bot as it actually decided, replayed through the engine —
  every recorded proposal that passed its recorded review becomes a proposal;
  rejections and holds propose nothing. The confirmation rule for
  re-confirmation is also a real ``futures_replay`` rule.
- ``ablation``: the recorded behavior with ONE knob overridden. ``memory_on``
  toggles the runtime memory dedup the bot ran under
  (``tradingagents/memory/log.py``: one decision entry per ticker+date — a
  repeat for the same symbol and day is suppressed): with ``memory_on=True``
  (the recorded default) only the first proposal per symbol/day survives; with
  ``memory_on=False`` the dedup is off and every confirmed recorded entry
  proposes. The knob changes the proposal set by construction, and a test pins
  the difference.

Every proposal requires complete recorded entry, bracket, risk, margin and
leverage terms. Missing timestamps, intent, or validation evidence are skipped
with source diagnostics; nothing is inferred.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tradingagents.agents.rating import is_review
from tradingagents.dataflows.market_data import Candle
from tradingagents.execution.futures_safety import validate_bracket
from tradingagents.simulation.data import candle_from_payload
from tradingagents.simulation.engine import Proposal
from tradingagents.simulation.fills import AMBIGUITY_POLICIES

# The proposal context a recorded snapshot rebuilds (same keys as the engine's
# replay: candle objects plus the derived direction/open/close fields).
ContextBuilder = Callable[[dict], dict | None]

DEFAULT_RULES = ("baseline", "htf_dominance_bounded_pullback")

_KNOWN_ABLATIONS = ("memory_on", "soft_cap_enabled", "quick_model")
_INCOMPLETE_EVIDENCE_REASONS = frozenset({
    "invalid_row", "missing_signal_or_snapshot", "unclosed_snapshot", "invalid_snapshot",
    "missing_intent", "missing_validation", "missing_terms", "invalid_terms",
    "missing_decision_timestamp", "invalid_decision_timestamp", "timestamp_before_evidence",
    "timestamp_order", "duplicate_conflict", "activation_collision",
})
_SOURCE_SKIP_REASONS = frozenset({
    *_INCOMPLETE_EVIDENCE_REASONS,
    "non_entry_intent", "validation_rejected", "recorded_rejection",
    "rule_rejection", "duplicate_event", "memory_dedup",
})


def _rebuild_context(snapshot: dict | None, symbol: str) -> dict | None:
    """The recorded snapshot as a rule context; None when unusable."""
    one_hour = candle_from_payload(snapshot.get("one_hour") if isinstance(snapshot, dict) else None,
                                   "1h", symbol)
    four_hour = candle_from_payload(snapshot.get("four_hour") if isinstance(snapshot, dict) else None,
                                    "4h", symbol)
    if (one_hour is None or four_hour is None or not symbol
            or one_hour.symbol != symbol or four_hour.symbol != symbol
            or one_hour.timeframe != "1h" or four_hour.timeframe != "4h"
            or one_hour.close_time - one_hour.open_time != 3_600_000
            or four_hour.close_time - four_hour.open_time != 14_400_000):
        return None

    def direction(candle: Candle) -> str:
        if candle.close > candle.open:
            return "bullish"
        if candle.close < candle.open:
            return "bearish"
        return "doji"

    return {
        **snapshot,
        "one_hour": one_hour, "four_hour": four_hour,
        "one_hour_direction": direction(one_hour),
        "four_hour_direction": direction(four_hour),
        "one_hour_open": one_hour.open_time,
        "one_hour_close": one_hour.close,
        "four_hour_open": four_hour.open_time,
        "four_hour_close": four_hour.close,
    }


def no_trade_source(candle: Candle) -> Proposal | None:
    """The cash baseline: nothing is proposed, nothing is filled."""
    return None


no_trade_source.mode = "no_trade"  # type: ignore[attr-defined]
no_trade_source.built = 0  # type: ignore[attr-defined]
no_trade_source.input_count = 0  # type: ignore[attr-defined]
no_trade_source.evaluated = 0  # type: ignore[attr-defined]
no_trade_source.skipped = {}  # type: ignore[attr-defined]
no_trade_source.coverage_status = "not_applicable"  # type: ignore[attr-defined]


def _proposal_from_terms(terms: dict[str, Any], symbol: str) -> Proposal | None:
    """One engine Proposal from recorded execution terms; None when incomplete.

    The recorded proposal must carry an explicit stop and target and a price
    the bracket brackets around; a missing field is skipped-with-count, never
    filled with a guess (no invented stop distance, no invented size).
    """
    signal = terms.get("signal")
    if not signal or is_review(signal) or signal not in ("Buy", "Overweight", "Sell", "Underweight"):
        return None
    names = ("price", "stop_loss", "take_profit", "leverage", "risk_fraction", "margin_fraction")
    try:
        if any(isinstance(terms.get(name), bool) for name in names):
            return None
        price, stop, target, leverage, risk, margin = (float(terms[name]) for name in names)
        if not all(math.isfinite(value) and value > 0 for value in (
            price, stop, target, leverage, risk, margin,
        )) or not leverage.is_integer() or leverage < 1 or risk > 1 or margin > 1:
            return None
        margin_mode = terms.get("margin_mode")
        if margin_mode not in ("isolated", "cross") or not isinstance(
            terms.get("scale_out_enabled"), bool
        ):
            return None
        validate_bracket(
            side="buy" if signal in ("Buy", "Overweight") else "sell",
            entry=price, stop_loss=stop, take_profit=target,
        )
        return Proposal(
            signal=signal, ticker=str(terms.get("ticker") or symbol),
            ccxt_symbol=symbol, stop_loss=stop, take_profit=target,
            leverage=leverage, risk_fraction=risk, margin_fraction=margin,
            margin_mode=margin_mode, scale_out_enabled=terms["scale_out_enabled"],
        )
    except (KeyError, TypeError, ValueError):
        return None


_FOUR_HOUR_MS = 14_400_000


def _snapshot_is_explicitly_closed(snapshot: dict) -> bool:
    return all(
        isinstance(snapshot.get(key), dict)
        and snapshot[key].get("is_closed") is True
        for key in ("one_hour", "four_hour")
    )


def _proposal_terms(payload: dict) -> dict[str, Any] | None:
    terms = payload.get("agent_proposal")
    if not isinstance(terms, dict):
        return None
    # These values must have been emitted by the agent; the replay may not
    # manufacture a bracket or entry estimate from a candle.
    required = (
        "entry_price", "stop_loss", "take_profit", "leverage", "risk_fraction",
        "margin_fraction", "margin_mode", "scale_out_enabled",
    )
    if any(terms.get(key) is None for key in required):
        return None
    return dict(terms)


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if number != number or abs(number) == float("inf"):
            return None
        if abs(number) < 100_000_000_000:
            number *= 1000
        return int(number)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _phase_timestamp(row: dict, payload: dict, name: str) -> Any:
    phase_timestamps = payload.get("phase_timestamps")
    if isinstance(phase_timestamps, dict) and phase_timestamps.get(name) is not None:
        return phase_timestamps[name]
    if payload.get(name) is not None:
        return payload[name]
    return row.get(name)


def _decision_availability(
    row: dict, payload: dict, *, evidence_close_ms: int,
) -> tuple[int | None, str | None]:
    """Return when signal and validated agent terms are durably available.

    ``proposal_validated_at`` is the only timestamp accepted for a proposal
    replay: ``analysis_finished_at`` proves the signal analysis finished, but
    cannot prove that the proposal terms were present and validated. Both
    timestamps are checked when present so malformed or reversed lifecycle
    data fails closed.
    """
    analysis_raw = _phase_timestamp(row, payload, "analysis_finished_at")
    proposal_raw = _phase_timestamp(row, payload, "proposal_validated_at")
    analysis_ms = _timestamp_ms(analysis_raw) if analysis_raw is not None else None
    proposal_ms = _timestamp_ms(proposal_raw) if proposal_raw is not None else None
    if analysis_raw is None or proposal_raw is None:
        return None, "missing_decision_timestamp"
    if analysis_ms is None or proposal_ms is None:
        return None, "invalid_decision_timestamp"
    if analysis_ms <= evidence_close_ms or proposal_ms <= evidence_close_ms:
        return None, "timestamp_before_evidence"
    if proposal_ms < analysis_ms:
        return None, "timestamp_order"
    return proposal_ms, None


def _activation_open_time(snapshot_open_ms: int, available_ms: int) -> int:
    """First bar whose decision at close is strictly after availability."""
    first_close = snapshot_open_ms + _FOUR_HOUR_MS
    bars_after_snapshot = max(0, (available_ms - first_close) // _FOUR_HOUR_MS + 1)
    return snapshot_open_ms + bars_after_snapshot * _FOUR_HOUR_MS


def _event_fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    terms = event.get("terms") or {}
    return (
        event.get("symbol"), event.get("timeframe"), event.get("snapshot_open_ms"),
        event.get("available_ms"), event.get("signal"),
        json.dumps(event.get("context"), sort_keys=True, default=repr),
        json.dumps(event.get("payload"), sort_keys=True, default=repr),
        event.get("phase"), event.get("confirmation_reason"), event.get("signal_reason"),
        event.get("analysis_finished_at"), event.get("proposal_validated_at"),
        tuple(sorted((str(key), repr(value)) for key, value in terms.items())),
    )


def _deduplicate_events(
    events: dict[tuple[str, str, int, int], list[dict[str, Any]]],
    skipped: dict[str, int],
) -> dict[tuple[str, str, int, int], dict[str, Any]]:
    """Deduplicate exact exports and reject conflicting event identities."""
    accepted: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for key, candidates in events.items():
        if len(candidates) == 1:
            accepted[key] = candidates[0]
            continue
        fingerprints = {_event_fingerprint(candidate) for candidate in candidates}
        if len(fingerprints) == 1:
            accepted[key] = candidates[0]
            skipped["duplicate_event"] += len(candidates) - 1
        else:
            skipped["duplicate_conflict"] += len(candidates)
    return accepted


def _independent_gate(row: dict, payload: dict, *, recorded: bool) -> str | None:
    """Require recorded entry intent and reject independent validation failures."""
    pm = payload.get("pm_decision")
    intent = pm.get("futures_execution_intent") if isinstance(pm, dict) else None
    if intent in ("abstain", "manage_existing"):
        return "non_entry_intent"
    if intent != "enter":
        return "missing_intent"
    confirmation_reasons = ("direction_mismatch", "doji_candle")
    if recorded and row.get("confirmation_reason") in confirmation_reasons:
        return "recorded_rejection"
    validation = payload.get("proposal_validation")
    if not isinstance(validation, dict) or not isinstance(validation.get("valid"), bool):
        return "missing_validation"
    if validation["valid"] is False:
        reason = str(validation.get("reason") or "")
        if (reason not in confirmation_reasons or row.get("phase") != "confirmation"
                or row.get("confirmation_reason") != reason):
            return "validation_rejected"
        if recorded:
            return "recorded_rejection"
    reason = str(row.get("signal_reason") or payload.get("signal_reason") or "")
    if reason and reason != "tradeable_signal":
        return "validation_rejected"
    return None


def _activation_groups(
    events: list[dict[str, Any]], skipped: dict[str, int],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        groups[(event["symbol"], event["timeframe"], event["activation_open"])].append(event)
    accepted = {}
    for key, group in groups.items():
        if len(group) > 1:
            skipped["activation_collision"] += len(group)
        else:
            accepted[key] = group[0]
    return accepted


def _replay_events(
    recorded_cycles: list[dict],
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
    *, recorded: bool, memory_on: bool = False,
    ) -> tuple[dict[tuple[str, str, int], dict[str, Any]], dict[str, int], int]:

    skipped: dict[str, int] = defaultdict(int)
    candidates: dict[tuple[str, str, int, int], list[dict[str, Any]]] = defaultdict(list)
    evaluated = 0
    for row in recorded_cycles:
        if not isinstance(row, dict):
            skipped["invalid_row"] += 1
            continue
        signal = row.get("signal")
        snapshot = row.get("snapshot")
        symbol = str(row.get("symbol") or "")
        if not signal or not isinstance(snapshot, dict):
            skipped["missing_signal_or_snapshot"] += 1
            continue
        if not _snapshot_is_explicitly_closed(snapshot):
            skipped["unclosed_snapshot"] += 1
            continue
        context = _rebuild_context(snapshot, symbol)
        if context is None:
            skipped["invalid_snapshot"] += 1
            continue
        one_hour = context["one_hour"]
        four_hour = context["four_hour"]
        candidates[(symbol, "4h", four_hour.open_time, one_hour.open_time)].append({
            "symbol": symbol, "timeframe": "4h", "snapshot_open_ms": four_hour.open_time,
            "signal": signal, "context": context, "payload": row.get("payload"),
            "phase": row.get("phase"), "confirmation_reason": row.get("confirmation_reason"),
            "signal_reason": row.get("signal_reason"),
            "analysis_finished_at": row.get("analysis_finished_at"),
            "proposal_validated_at": row.get("proposal_validated_at"), "row": row,
        })

    accepted = _deduplicate_events(candidates, skipped)
    confirmed_events = []
    for candidate in accepted.values():
        row = candidate["row"]
        signal, symbol, context = candidate["signal"], candidate["symbol"], candidate["context"]
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        gate_reason = _independent_gate(row, payload, recorded=recorded)
        if gate_reason:
            skipped[gate_reason] += 1
            continue
        terms = _proposal_terms(payload)
        if terms is None:
            skipped["missing_terms"] += 1
            continue
        proposal = _proposal_from_terms({
            **terms, "ticker": symbol, "signal": signal, "price": terms["entry_price"],
        }, symbol)
        if proposal is None:
            skipped["invalid_terms"] += 1
            continue
        one_hour, four_hour = context["one_hour"], context["four_hour"]
        available_ms, reason = _decision_availability(
            row, payload, evidence_close_ms=max(one_hour.close_time, four_hour.close_time),
        )
        if available_ms is None:
            skipped[reason or "missing_decision_timestamp"] += 1
            continue
        evaluated += 1
        confirmed, _ = confirmation_rule(context, signal)
        if confirmed:
            confirmed_events.append({
                "proposal": proposal, "symbol": symbol, "timeframe": "4h",
                "snapshot_open_ms": four_hour.open_time, "available_ms": available_ms,
            "activation_open": _activation_open_time(four_hour.open_time, available_ms),
            "one_hour_open_ms": one_hour.open_time,
            "signal": signal, "terms": terms, "context": context,

            })
        else:
            skipped["recorded_rejection" if recorded else "rule_rejection"] += 1
    # Resolve collisions before memory suppression; a third event cannot revive
    # an ambiguous activation, and the outcome never depends on export order.
    events = _activation_groups(confirmed_events, skipped)
    if memory_on:
        seen: set[tuple[str, str]] = set()
        for key, event in sorted(events.items(), key=lambda item: (
            item[1]["available_ms"], item[1]["snapshot_open_ms"], item[0],
        )):
            day = datetime.fromtimestamp(event["available_ms"] / 1000, UTC).date().isoformat()
            identity = (event["symbol"], day)
            if identity in seen:
                skipped["memory_dedup"] += 1
                del events[key]
            else:
                seen.add(identity)
    return events, defaultdict(int, sorted(skipped.items())), evaluated


def _source_status(skipped: dict[str, int], evaluated: int, built: int) -> str:
    if evaluated == 0 or built == 0 or any(
        skipped.get(reason, 0) for reason in _INCOMPLETE_EVIDENCE_REASONS
    ):
        return "insufficient_evidence"
    return "sufficient_evidence"


def _source_diagnostics(skipped: dict[str, int]) -> dict[str, int]:
    """Stable source diagnostics with zeroes for all known skip classes."""
    return {reason: skipped.get(reason, 0) for reason in sorted(_SOURCE_SKIP_REASONS)}


def _holdout_coverage(
    recorded_cycles: list[dict],
    rule: Callable[[dict, str], tuple[bool, str]],
    events: dict[tuple[str, str, int], dict[str, Any]],
    candles: list[Candle],
    *, recorded: bool, memory_on: bool = False,
) -> dict[str, Any]:
    holdout = {
        (c.symbol, c.timeframe, c.open_time): c
        for c in candles if c.is_closed
    }
    selected = []
    identities: set[tuple[str, int]] = set()
    for row in recorded_cycles:
        if not isinstance(row, dict):
            selected.append(row)
            continue
        symbol = str(row.get("symbol") or "")
        relevant = [c for (s, timeframe, _), c in holdout.items()
                    if (not symbol or s == symbol) and timeframe == "4h"]
        if not relevant:
            continue
        context = _rebuild_context(row.get("snapshot"), symbol)
        if context is None:
            selected.append(row)
            continue
        one_hour, four_hour = context["one_hour"], context["four_hour"]
        evidence_close = max(one_hour.close_time, four_hour.close_time)
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        available_ms, _ = _decision_availability(row, payload, evidence_close_ms=evidence_close)
        if available_ms is None:
            # Without a valid availability time, any later matching candle could
            # be affected; do not discard incomplete evidence as out-of-window.
            earliest = _activation_open_time(four_hour.open_time, evidence_close)
            if any(c.open_time >= earliest for c in relevant):
                selected.append(row)
                identities.add((symbol, four_hour.open_time))
        elif any(
            c.open_time == _activation_open_time(four_hour.open_time, available_ms)
            and c.close_time > available_ms for c in relevant
        ):
            selected.append(row)
            identities.add((symbol, four_hour.open_time))
    selected_ids = {id(row) for row in selected}
    for row in recorded_cycles:
        if not isinstance(row, dict) or id(row) in selected_ids:
            continue
        symbol = str(row.get("symbol") or "")
        context = _rebuild_context(row.get("snapshot"), symbol)
        if context is not None and (symbol, context["four_hour"].open_time) in identities:
            selected.append(row)
    _, skipped, evaluated = _replay_events(selected, rule, recorded=recorded, memory_on=memory_on)
    built = sum(
        1 for key, event in events.items()
        if key in holdout and holdout[key].close_time > event["available_ms"]
    )
    return {
        "input_count": len(selected), "evaluated": evaluated, "built": built,
        "status": _source_status(skipped, evaluated, built),
        "skipped": _source_diagnostics(skipped),
    }


def rule_source(
    rule: Callable[[dict, str], tuple[bool, str]],
    recorded_cycles: list[dict],
    *,
    signal_by_candle: dict[int, dict[str, Any]] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """Replay a counterfactual rule with recorded intent, terms and timestamps."""
    del signal_by_candle
    from server.futures_replay import RULES

    if rule not in RULES.values():
        raise ValueError("the rule baseline must use a server.futures_replay.RULES function")
    events, skipped, evaluated = _replay_events(recorded_cycles, rule, recorded=False)

    def source(candle: Candle) -> Proposal | None:
        if not candle.is_closed or candle.timeframe != "4h":
            return None
        event = events.get((candle.symbol, candle.timeframe, candle.open_time))
        if event is None or candle.close_time <= event["available_ms"]:
            return None
        return event["proposal"]

    source.rule_fn = rule  # type: ignore[attr-defined]
    source.coverage_for = lambda candles: _holdout_coverage(  # type: ignore[attr-defined]
        recorded_cycles, rule, events, candles, recorded=False,
    )
    source.skipped = _source_diagnostics(skipped)  # type: ignore[attr-defined]
    source.built = len(events)  # type: ignore[attr-defined]
    source.event_keys = tuple(events)  # type: ignore[attr-defined]
    source.input_count = len(recorded_cycles)  # type: ignore[attr-defined]
    source.evaluated = evaluated  # type: ignore[attr-defined]
    source.coverage_status = _source_status(skipped, evaluated, len(events))  # type: ignore[attr-defined]
    source.mode = "counterfactual_rule"  # type: ignore[attr-defined]
    source.counterfactual = True  # type: ignore[attr-defined]
    return source


def recorded_source(
    recorded_cycles: list[dict],
    *,
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
    ablation: dict[str, Any] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """Replay recorded decisions only when entry intent and terms are proven."""
    ablation = ablation or {}
    unknown = set(ablation) - set(_KNOWN_ABLATIONS)
    if unknown:
        raise ValueError(f"unknown ablation knob(s): {sorted(unknown)}; the manifest names them")
    events, skipped, evaluated = _replay_events(
        recorded_cycles, confirmation_rule, recorded=True,
        memory_on=bool(ablation.get("memory_on", True)),
    )

    def source(candle: Candle) -> Proposal | None:
        if not candle.is_closed or candle.timeframe != "4h":
            return None
        event = events.get((candle.symbol, candle.timeframe, candle.open_time))
        if event is None or candle.close_time <= event["available_ms"]:
            return None
        return event["proposal"]

    source.coverage_for = lambda candles: _holdout_coverage(  # type: ignore[attr-defined]
        recorded_cycles, confirmation_rule, events, candles, recorded=True,
        memory_on=bool(ablation.get("memory_on", True)),
    )
    source.skipped = _source_diagnostics(skipped)  # type: ignore[attr-defined]
    source.built = len(events)  # type: ignore[attr-defined]
    source.event_keys = tuple(events)  # type: ignore[attr-defined]
    source.input_count = len(recorded_cycles)  # type: ignore[attr-defined]
    source.evaluated = evaluated  # type: ignore[attr-defined]
    source.coverage_status = _source_status(skipped, evaluated, len(events))  # type: ignore[attr-defined]
    source.mode = "recorded_ablation" if ablation else "recorded_decisions"  # type: ignore[attr-defined]
    return source


def ambiguity_policy_for(name: str) -> str:
    """Guard the policy vocabulary so a typo cannot silently change matching."""
    if name not in AMBIGUITY_POLICIES:
        raise ValueError(f"unknown ambiguity policy {name!r}")
    return name


__all__ = [
    "no_trade_source",
    "rule_source",
    "recorded_source",
    "ambiguity_policy_for",
    "DEFAULT_RULES",
]
