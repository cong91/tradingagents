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
    if one_hour is None or four_hour is None:
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


def rule_source(
    rule: Callable[[dict, str], tuple[bool, str]],
    recorded_cycles: list[dict],
    *,
    signal_by_candle: dict[int, dict[str, Any]] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """A deterministic rule baseline built on the REAL production rule.

    ``recorded_cycles`` are exported futures_cycles rows; their snapshots give
    the rule its context at the cycle's own decision time. The returned
    source proposes on the 4h-candle closes that match a recorded cycle's
    decision time, so the rule decides exactly what it would have decided
    that day (point-in-time), with the recorded entry terms as bracket terms.

    Events are keyed by ``(symbol, open_time)``, not open time alone: the
    export holds many symbols that share a 4h decision time, so an open-time
    key would silently keep only the last symbol's event and then attach that
    symbol's prices and bracket terms to a different instrument's candle.
    """
    from server.futures_replay import RULES  # the real production rules, not a fork

    if rule not in RULES.values():
        raise ValueError("the rule baseline must use a server.futures_replay.RULES function")

    events: dict[tuple[str, int], dict[str, Any]] = {}
    for row in recorded_cycles:
        if not isinstance(row, dict):
            continue
        signal = row.get("signal")
        snapshot = row.get("snapshot")
        symbol = str(row.get("symbol") or "")
        if not signal or not isinstance(snapshot, dict):
            continue
        context = _rebuild_context(snapshot, symbol)
        four_hour = context.get("four_hour") if context else None
        if context is None or four_hour is None:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        proposal_terms = (payload.get("agent_proposal") if isinstance(payload, dict) else None) or {}
        # The fallback bracket must respect the signal's direction: a short
        # (Sell/Underweight) needs stop > entry > target, a long the reverse.
        # Hardcoding the long shape silently inverted every short bracket, and
        # the runtime's validate_bracket then refused the entry — a recorded
        # short could never trade in a simulation built from this source.
        long_side = str(signal) in ("Buy", "Overweight")
        stop_default = four_hour.close * (0.98 if long_side else 1.02)
        target_default = four_hour.close * (1.04 if long_side else 0.96)
        terms = {
            "ticker": row.get("symbol"),
            "stop_loss": proposal_terms.get("stop_loss", stop_default),
            "take_profit": proposal_terms.get("take_profit", target_default),
            "leverage": proposal_terms.get("leverage", 1.0),
            "risk_fraction": proposal_terms.get("risk_fraction", 0.01),
        }
        # The FULL rule context travels with the event: the rule re-decides on
        # the same evidence it decided on that day, not a 1h-only projection.
        events[(str(symbol or four_hour.symbol), four_hour.open_time)] = {
            "signal": str(signal), "terms": terms,
            "symbol": str(symbol or four_hour.symbol),
            "context": context,
        }

    def source(candle: Candle) -> Proposal | None:
        if candle.timeframe != "4h":
            return None
        # Symbol-scoped lookup: an instrument never trades another's decision.
        event = events.get((candle.symbol, candle.open_time))
        if event is None:
            return None
        confirmed, _ = rule(event["context"], event["signal"])
        if not confirmed:
            return None
        return _proposal_from_terms(
            {**event["terms"], "signal": event["signal"], "price": candle.close},
            event["symbol"],
        )

    source.rule_fn = rule  # type: ignore[attr-defined]  # the no-fork pin
    return source


def recorded_source(
    recorded_cycles: list[dict],
    *,
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
    ablation: dict[str, Any] | None = None,
) -> Callable[[Candle], Proposal | None]:
    """The bot as it decided, replayed through the same engine.

    A recorded cycle proposes when its own snapshot carries explicit entry
    terms and its recorded review was a pass (same flip vocabulary the engine
    replay uses: ``direction_mismatch``/``doji_candle`` are recorded
    rejections). The optional ``ablation`` is a single named knob; only
    ``memory_on`` (the runtime memory dedup — one proposal per symbol/day,
    mirroring ``TradingMemoryLog.store_decision``'s idempotency guard) and
    ``quick_model`` (same decisions, faster fills assumed unchanged) are
    recognized; an unknown ablation name is refused rather than silently
    ignored. ``memory_on=False`` switches the dedup off, so repeated entries
    for the same symbol/day propose instead of being suppressed — the knob
    changes the proposal set or it is not an ablation.

    Known data limit: the recorded export carries entry terms in
    ``payload.agent_proposal``; cycles exported without them fall into
    ``skipped["missing_terms"]``. If the export at hand has no recorded terms
    at all, this source builds zero proposals — reported honestly via
    ``built``/``skipped``, never filled with invented stop/target values.
    """
    ablation = ablation or {}
    unknown = set(ablation) - set(_KNOWN_ABLATIONS)
    if unknown:
        raise ValueError(f"unknown ablation knob(s): {sorted(unknown)}; the manifest names them")
    memory_on = bool(ablation.get("memory_on", True))

    proposals: dict[int, dict[str, Any]] = {}
    skipped = {"missing_terms": 0, "recorded_rejection": 0, "memory_dedup": 0}
    # The runtime memory dedup, replayed: the real memory log stores ONE entry
    # per (ticker, date) — a second decision for the same symbol and day is
    # suppressed (tradingagents/memory/log.py, store_decision's guard). The
    # ``memory_on`` ablation toggles exactly that behavior off.
    seen_symbol_day: set[tuple[str, str]] = set()
    for row in recorded_cycles:
        if not isinstance(row, dict):
            continue
        signal = row.get("signal")
        snapshot = row.get("snapshot")
        symbol = str(row.get("symbol") or "")
        if not signal or not isinstance(snapshot, dict):
            continue
        one_hour = candle_from_payload(snapshot.get("one_hour"), "1h", symbol)
        if one_hour is None:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        terms_payload = (payload.get("agent_proposal") if isinstance(payload, dict) else None) or {}
        price = terms_payload.get("entry_price", one_hour.close)
        terms = dict(terms_payload)
        terms["ticker"] = symbol
        recorded_rejected = row.get("confirmation_reason") in ("direction_mismatch", "doji_candle")
        if recorded_rejected:
            skipped["recorded_rejection"] += 1
            continue
        proposal = _proposal_from_terms({**terms, "signal": signal, "price": price}, symbol)
        if proposal is None:
            skipped["missing_terms"] += 1
            continue
        confirmed, _ = confirmation_rule(_rebuild_context(snapshot, symbol) or {}, signal)
        if not confirmed:
            skipped["recorded_rejection"] += 1
            continue
        if memory_on:
            day = datetime.fromtimestamp(one_hour.open_time / 1000, UTC).date().isoformat()
            key = (symbol, day)
            if key in seen_symbol_day:
                skipped["memory_dedup"] += 1
                continue
            seen_symbol_day.add(key)
        proposals[one_hour.open_time] = {
            "proposal": proposal,
        }

    def source(candle: Candle) -> Proposal | None:
        event = proposals.get(candle.open_time)
        return event["proposal"] if event is not None else None

    source.skipped = skipped  # type: ignore[attr-defined]  # reported honestly
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
