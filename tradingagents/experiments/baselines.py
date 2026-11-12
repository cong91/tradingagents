"""Baseline variants: every variant runs the SAME simulation (plan §4.1).

One concern: build the ``ProposalSource`` for each registered baseline so all
variants — no-trade, a simple deterministic rule, the recorded bot behavior,
and a single-knob ablation — are evaluated by the identical engine, cost
model, fill rules and funding series. Differences between variants come only
from their proposals, never from a different pipeline.

- ``no_trade``: proposes nothing. Its result is the cash baseline the other
  variants must beat — not zero noise.
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

Every proposal carries explicit stop/target terms, so a missing price or a
Hold signal simply proposes nothing; nothing is inferred.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tradingagents.agents.rating import is_review
from tradingagents.dataflows.market_data import Candle
from tradingagents.simulation.data import candle_from_payload
from tradingagents.simulation.engine import Proposal
from tradingagents.simulation.fills import AMBIGUITY_POLICIES

# The proposal context a recorded snapshot rebuilds (same keys as the engine's
# replay: candle objects plus the derived direction/open/close fields).
ContextBuilder = Callable[[dict], dict | None]

DEFAULT_RULES = ("baseline", "htf_dominance_bounded_pullback")

_KNOWN_ABLATIONS = ("memory_on", "soft_cap_enabled", "quick_model")


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


def _proposal_from_terms(terms: dict[str, Any], symbol: str) -> Proposal | None:
    """One engine Proposal from recorded execution terms; None when incomplete.

    The recorded proposal must carry an explicit stop and target and a price
    the bracket brackets around; a missing field is skipped-with-count, never
    filled with a guess (no invented stop distance, no invented size).
    """
    signal = terms.get("signal")
    price = terms.get("price")
    stop = terms.get("stop_loss")
    target = terms.get("take_profit")
    if not signal or is_review(signal) or signal == "Hold":
        return None
    if signal not in ("Buy", "Overweight", "Sell", "Underweight"):
        return None
    if price is None or stop is None or target is None or price <= 0:
        return None
    try:
        return Proposal(
            signal=str(signal), ticker=str(terms.get("ticker") or symbol),
            ccxt_symbol=str(symbol), stop_loss=float(stop),
            take_profit=float(target), leverage=float(terms.get("leverage") or 1.0),
            risk_fraction=float(terms.get("risk_fraction") or 0.01),
            margin_fraction=float(terms.get("margin_fraction") or 0.25),
            scale_out_enabled=bool(terms.get("scale_out_enabled", True)),
        )
    except (TypeError, ValueError):
        return None


def _confirmed_entry(
    candle: Candle, *, rule: Callable[[dict, str], tuple[bool, str]], signal: str,
    price: float, terms: dict[str, Any], symbol: str,
) -> Proposal | None:
    """A rule-confirmed proposal on this candle, using the recorded terms."""
    context = {
        "one_hour": candle,
        "one_hour_direction": ("bullish" if candle.close > candle.open
                               else "bearish" if candle.close < candle.open else "doji"),
        "one_hour_open": candle.open_time, "one_hour_close": candle.close,
    }
    confirmed, _ = rule(context, signal)
    if not confirmed:
        return None
    return _proposal_from_terms({**terms, "signal": signal, "price": price}, symbol)


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
    required = ("entry_price", "stop_loss", "take_profit")
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
    """First 4h bar whose close can observe the recorded decision timestamp."""
    first_close = snapshot_open_ms + _FOUR_HOUR_MS
    bars_after_snapshot = max(
        0, (available_ms - first_close + _FOUR_HOUR_MS - 1) // _FOUR_HOUR_MS,
    )
    return snapshot_open_ms + bars_after_snapshot * _FOUR_HOUR_MS


def _event_fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    terms = event.get("terms") or {}
    return (
        event.get("symbol"), event.get("timeframe"), event.get("snapshot_open_ms"),
        event.get("available_ms"), event.get("signal"),
        tuple(sorted((str(key), repr(value)) for key, value in terms.items())),
    )


def _deduplicate_events(
    events: dict[tuple[str, str, int], list[dict[str, Any]]],
    skipped: dict[str, int],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Deduplicate exact exports and reject conflicting event identities."""
    accepted: dict[tuple[str, str, int], dict[str, Any]] = {}
    for key, candidates in events.items():
        if len(candidates) == 1:
            accepted[key] = candidates[0]
            continue
        fingerprints = {_event_fingerprint(candidate) for candidate in candidates}
        if len(fingerprints) == 1:
            accepted[key] = candidates[0]
            skipped["duplicate_event"] += len(candidates) - 1
        else:
            skipped["duplicate_event"] += len(candidates)
    return accepted


def rule_source(
    rule: Callable[[dict, str], tuple[bool, str]],
    recorded_cycles: list[dict],
    *,
    signal_by_candle: dict[int, dict[str, Any]] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """Build a rule proposal source from point-in-time recorded evidence.

    This is a counterfactual confirmation-rule baseline, not a claim that the
    rule would have known the answer at the candle open, and not a replay of
    the agent's decisions. Terms: recorded ``payload.agent_proposal`` values
    when the export carries them; otherwise the baseline's own declared
    direction-respecting bracket from the 4h close (a recorded SHORT must get
    stop > entry > target, or ``validate_bracket`` refuses every short). The
    decision is available when the snapshot evidence closed, so activation is
    the snapshot's own 4h bar — the engine fills at the NEXT bar's open, so
    no look-ahead enters the fill. ``recorded_source`` (the bot replay) stays
    strict: it never manufactures terms this baseline is allowed to declare.
    """
    del signal_by_candle  # retained as an API-compatible, intentionally unused hook
    from server.futures_replay import RULES  # the real production rules, not a fork

    if rule not in RULES.values():
        raise ValueError("the rule baseline must use a server.futures_replay.RULES function")

    skipped = defaultdict(int)
    candidates: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in recorded_cycles:
        if not isinstance(row, dict):
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
        four_hour = context.get("four_hour") if context else None
        if context is None or four_hour is None:
            skipped["invalid_snapshot"] += 1
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        recorded_terms = _proposal_terms(payload)
        long_side = str(signal) in ("Buy", "Overweight")
        # Fallback bracket respects the signal's direction; hardcoded long
        # shapes silently inverted every recorded short (see git history).
        terms = {
            "ticker": row.get("symbol") or symbol,
            "stop_loss": (recorded_terms or {}).get(
                "stop_loss", four_hour.close * (0.98 if long_side else 1.02),
            ),
            "take_profit": (recorded_terms or {}).get(
                "take_profit", four_hour.close * (1.04 if long_side else 0.96),
            ),
            "leverage": (recorded_terms or {}).get("leverage", 1.0),
            "risk_fraction": (recorded_terms or {}).get("risk_fraction", 0.01),
        }
        if recorded_terms is None:
            skipped["fallback_terms"] += 1
        # The rule saw closed candles; its verdict is available at that close,
        # so the activation bar is the snapshot's own bar.
        available_ms = max(context["one_hour"].close_time, four_hour.close_time)
        if row.get("confirmation_reason") in ("direction_mismatch", "doji_candle"):
            skipped["recorded_rejection"] += 1
            continue
        activation_open = _activation_open_time(four_hour.open_time, available_ms)
        candidates[(symbol, "4h", four_hour.open_time)].append({
            "signal": str(signal),
            "terms": {**terms, "ticker": row.get("symbol") or symbol},
            "symbol": symbol,
            "timeframe": "4h",
            "context": context,
            "snapshot_open_ms": four_hour.open_time,
            "available_ms": available_ms,
            "activation_open": activation_open,
        })

    accepted = _deduplicate_events(candidates, skipped)
    events_by_activation: dict[tuple[str, str, int], dict[str, Any]] = {}
    for event in accepted.values():
        activation_key = (event["symbol"], event["timeframe"], event["activation_open"])
        if activation_key in events_by_activation:
            skipped["duplicate_event"] += 2
            events_by_activation.pop(activation_key)
            continue
        events_by_activation[activation_key] = event

    def source(candle: Candle) -> Proposal | None:
        if not candle.is_closed or candle.timeframe != "4h":
            return None
        event = events_by_activation.get((candle.symbol, "4h", candle.open_time))
        if event is None or candle.close_time < event["available_ms"]:
            return None
        confirmed, _ = rule(event["context"], event["signal"])
        if not confirmed:
            return None
        return _proposal_from_terms(
            {**event["terms"], "signal": event["signal"], "price": candle.close},
            event["symbol"],
        )

    source.rule_fn = rule  # type: ignore[attr-defined]  # the no-fork pin
    source.skipped = {key: skipped.get(key, 0) for key in (
        "missing_signal_or_snapshot", "unclosed_snapshot", "invalid_snapshot",
        "missing_terms", "fallback_terms", "recorded_rejection", "duplicate_event",
    )}  # type: ignore[attr-defined]
    source.built = len(events_by_activation)  # type: ignore[attr-defined]
    source.counterfactual = True  # type: ignore[attr-defined]
    return source


def recorded_source(
    recorded_cycles: list[dict],
    *,
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
    ablation: dict[str, Any] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """Replay recorded agent decisions through the shared simulation engine.

    A proposal is available only after an explicit ``proposal_validated_at``
    timestamp proves that the recorded signal and terms existed. Missing or
    contradictory lifecycle evidence is skipped and reported, never inferred
    from candle closes or cycle IDs.
    """
    ablation = ablation or {}
    unknown = set(ablation) - set(_KNOWN_ABLATIONS)
    if unknown:
        raise ValueError(f"unknown ablation knob(s): {sorted(unknown)}; the manifest names them")
    memory_on = bool(ablation.get("memory_on", True))

    proposals: dict[tuple[str, str, int], dict[str, Any]] = {}
    skipped: dict[str, int] = defaultdict(int)
    candidates: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    seen_symbol_day: set[tuple[str, str]] = set()
    for row in recorded_cycles:
        if not isinstance(row, dict):
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
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        terms_payload = _proposal_terms(payload)
        if terms_payload is None:
            skipped["missing_terms"] += 1
            continue
        if row.get("confirmation_reason") in ("direction_mismatch", "doji_candle"):
            skipped["recorded_rejection"] += 1
            continue
        available_ms, reason = _decision_availability(
            row, payload, evidence_close_ms=max(one_hour.close_time, four_hour.close_time),
        )
        if available_ms is None:
            skipped[reason or "missing_decision_timestamp"] += 1
            continue
        confirmed, _ = confirmation_rule(context, signal)
        if not confirmed:
            skipped["recorded_rejection"] += 1
            continue
        day = datetime.fromtimestamp(one_hour.open_time / 1000, UTC).date().isoformat()
        activation_open = _activation_open_time(four_hour.open_time, available_ms)
        event_key = (symbol, "4h", four_hour.open_time)
        candidates[event_key].append({
            "proposal": _proposal_from_terms(
                {
                    **terms_payload,
                    "ticker": symbol,
                    "signal": signal,
                    "price": terms_payload["entry_price"],
                },
                symbol,
            ),
            "symbol": symbol,
            "timeframe": "4h",
            "snapshot_open_ms": four_hour.open_time,
            "available_ms": available_ms,
            "activation_open": activation_open,
            "signal": signal,
            "terms": terms_payload,
        })

    accepted = _deduplicate_events(candidates, skipped)
    for event in accepted.values():
        if memory_on:
            day = datetime.fromtimestamp(
                event["snapshot_open_ms"] / 1000, UTC,
            ).date().isoformat()
            key = (event["symbol"], day)
            if key in seen_symbol_day:
                skipped["memory_dedup"] += 1
                continue
            seen_symbol_day.add(key)
        proposal = event.get("proposal")
        if proposal is None:
            skipped["missing_terms"] += 1
            continue
        activation_key = (event["symbol"], event["timeframe"], event["activation_open"])
        if activation_key in proposals:
            skipped["duplicate_event"] += 2
            proposals.pop(activation_key)
            continue
        proposals[activation_key] = {"proposal": proposal}

    def source(candle: Candle) -> Proposal | None:
        if not candle.is_closed or candle.timeframe != "4h":
            return None
        event = proposals.get((candle.symbol, "4h", candle.open_time))
        return event["proposal"] if event is not None else None

    source.skipped = {key: skipped.get(key, 0) for key in (
        "missing_signal_or_snapshot", "unclosed_snapshot", "invalid_snapshot",
        "missing_terms", "missing_decision_timestamp", "invalid_decision_timestamp",
        "timestamp_order", "timestamp_before_evidence", "duplicate_event",
        "recorded_rejection", "memory_dedup",
    )}  # type: ignore[attr-defined]
    source.built = len(proposals)  # type: ignore[attr-defined]
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
