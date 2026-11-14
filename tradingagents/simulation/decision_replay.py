"""Point-in-time replay of recorded Futures decision gates."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradingagents.auto_timeframes import timeframe_ms
from tradingagents.dataflows.market_data import Candle
from tradingagents.simulation.data import candle_from_payload

_RECORDED_REJECTION_REASONS = {"direction_mismatch", "doji_candle"}
DISCLAIMER = (
    "Simulation output on replayed closed candles with modeled costs, funding "
    "and fills; it is not evidence of live profitability and not financial advice."
)


def replay_recorded_decisions(
    cycles: list[dict[str, Any]],
    *,
    confirmation_rule: Callable[[dict, str], tuple[bool, str]],
) -> dict[str, Any]:
    """Compare recorded confirmation decisions with the production rule.

    This is a gate-agreement report. It cannot infer post-decision market history,
    execution terms, exits, funding or realized PnL from decision snapshots.
    """
    outcomes: list[dict[str, Any]] = []
    missing_snapshot = 0
    missing_signal = 0
    for row in cycles:
        if not isinstance(row, dict):
            continue
        signal = row.get("signal")
        if not signal:
            missing_signal += 1
            continue
        snapshot = row.get("snapshot")
        symbol = str(row.get("symbol") or "")
        one_hour = candle_from_payload(
            snapshot.get("one_hour") if isinstance(snapshot, dict) else None,
            "1h", symbol,
        )
        four_hour = candle_from_payload(
            snapshot.get("four_hour") if isinstance(snapshot, dict) else None,
            "4h", symbol,
        )
        if one_hour is None or four_hour is None:
            missing_snapshot += 1
            continue

        def direction(candle: Candle) -> str:
            if candle.close > candle.open:
                return "bullish"
            if candle.close < candle.open:
                return "bearish"
            return "doji"

        context = {
            "one_hour": one_hour, "four_hour": four_hour,
            "one_hour_direction": direction(one_hour),
            "four_hour_direction": direction(four_hour),
            "one_hour_open": one_hour.open_time,
            "one_hour_close": one_hour.close,
            "four_hour_open": four_hour.open_time,
            "four_hour_close": four_hour.close,
        }
        selected_identity = snapshot.get("candle_identity") if isinstance(snapshot, dict) else None
        if selected_identity is not None:
            if not isinstance(selected_identity, dict):
                missing_snapshot += 1
                continue
            selected = candle_from_payload(
                snapshot.get("selected_candle"), str(selected_identity.get("timeframe")), symbol,
            )
            try:
                step = timeframe_ms(selected.timeframe) if selected is not None else 0
                cutoff = datetime.fromisoformat(str(snapshot["decision_as_of"]).replace("Z", "+00:00"))
                if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                    raise ValueError("decision time must be timezone-aware")
                cutoff_ms = int(cutoff.timestamp() * 1000)
            except (KeyError, ValueError, TypeError, OverflowError):
                missing_snapshot += 1
                continue
            if (selected is None or selected.symbol != symbol
                    or selected.symbol != selected_identity.get("symbol")
                    or selected.timeframe != selected_identity.get("timeframe")
                    or selected.open_time != selected_identity.get("open_time")
                    or selected.close_time != selected_identity.get("close_time")
                    or selected.open_time < 0 or selected.open_time % step
                    or selected.close_time - selected.open_time != step
                    or selected.close_time != cutoff_ms - cutoff_ms % step):
                missing_snapshot += 1
                continue
            context["selected_candle"] = selected
            context["candle_identity"] = dict(selected_identity)
        confirmed, reason = confirmation_rule(context, signal)
        recorded_confirmed = row.get("confirmation_reason") not in _RECORDED_REJECTION_REASONS
        outcomes.append({
            "cycle_id": row.get("cycle_id"),
            "symbol": symbol,
            "recorded_confirmation_reason": row.get("confirmation_reason"),
            "recorded_confirmed": recorded_confirmed,
            "rule_result": confirmed,
            "rule_reason": reason,
        })
    agreements = sum(
        1 for outcome in outcomes
        if outcome["rule_result"] == outcome["recorded_confirmed"]
    )
    reason_counts = Counter(outcome["rule_reason"] for outcome in outcomes)
    return {
        "kind": "gate_agreement_replay",
        "disclaimer": DISCLAIMER,
        "recorded_cycles": len(cycles),
        "replayed_with_snapshot": len(outcomes),
        "skipped_missing_signal": missing_signal,
        "skipped_missing_snapshot": missing_snapshot,
        "decision_agreements": agreements,
        "decision_disagreements": len(outcomes) - agreements,
        "rule_reason_distribution": dict(sorted(reason_counts.items())),
        "financial_simulation": {
            "status": "unavailable",
            "reason": (
                "the recorded export holds only the two decision candles (1h + 4h) "
                "per cycle: no post-decision candle history, no recorded portfolio "
                "or proposal execution terms, so exits, funding and net PnL cannot "
                "be replayed from this data. Missing data is not zero."
            ),
        },
    }


__all__ = ["replay_recorded_decisions"]
