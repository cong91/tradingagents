"""Expert re-evaluation of a stale futures position at its time-stop deadline.

The monitor's time stop used to close mechanically at
``risk_derivatives_max_hold_hours``. With the analyzed time stop enabled, the
monitor persists a pending request and calls :func:`request_review`; this
module runs ONE structured LLM call over the position's evidence — position
math in initial-risk units, funding history and schedule, recent candles, and
the original entry thesis — and persists a HOLD verdict (the deadline extends
by the reviewed interval) or a CLOSE verdict (the monitor's next tick closes
through the proven mechanical path). The worker never touches the venue: the
monitor stays the sole mutation owner, and a failed or missing review falls
back to the mechanical close after a grace period.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from server import job_store
from server.autoloop import _futures_derivative_metrics
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured,
)
from tradingagents.dataflows.market_data import fetch_closed_candles
from tradingagents.execution.futures_funding import funding_schedule
from tradingagents.execution.futures_protection import _grace_hours, _positive_or_none
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.llm_clients import create_tier_client

logger = logging.getLogger(__name__)

# The verdict may pick its own next review horizon, inside hard bounds so a
# runaway "review again in 500h" can never park a position indefinitely.
_MIN_REVIEW_HOURS = 4.0
_MAX_REVIEW_HOURS = 72.0
_ENTRY_NOTE_LIMIT = 1500
_REASONING_LIMIT = 600
# CAS retries against the monitor's own snapshot writes; the monitor is the
# only other writer and it ticks every 30s, so contention is rare.
_SAVE_ATTEMPTS = 3
_CAS_BACKOFF_SECONDS = 0.2

_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()


class PositionHoldReview(BaseModel):
    """The structured verdict of one position-hold review."""

    verdict: Literal["hold", "close"]
    reasoning: str = Field(min_length=1)
    review_after_hours: float | None = None

    @field_validator("reasoning", mode="before")
    @classmethod
    def _stringify_reasoning(cls, value):
        if value is None:
            return ""
        return value if isinstance(value, str) else str(value)

    @field_validator("review_after_hours", mode="before")
    @classmethod
    def _coerce_hours(cls, value):
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def request_review(
    position_key: str,
    payload: dict,
    *,
    bridge_factory: Callable[[], Any],
    config: dict,
) -> bool:
    """Spawn the background review for one position; False when one is running.

    ``bridge_factory`` builds a fresh venue bridge inside the worker thread so
    the review never shares a ccxt client with the monitor that is still
    polling.
    """
    with _IN_FLIGHT_LOCK:
        if position_key in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(position_key)
    threading.Thread(
        target=_run_review,
        name=f"position-review-{position_key}",
        daemon=True,
        args=(position_key, dict(payload), bridge_factory, config),
    ).start()
    return True


def _run_review(
    position_key: str,
    payload: dict,
    bridge_factory: Callable[[], Any],
    config: dict,
) -> None:
    try:
        bridge = bridge_factory()
        _review_once(position_key, payload, bridge, config)
    except Exception:
        logger.exception("position review failed for %s", position_key)
        try:
            _persist_unavailable(position_key, {}, None, config, "review crashed")
        except Exception:
            logger.exception("position review fallback persist failed for %s", position_key)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(position_key)


def _review_once(position_key: str, payload: dict, bridge: Any, config: dict) -> None:
    prompt = _build_prompt(payload, bridge)
    llm = create_tier_client(config, "deep").get_llm()
    structured = bind_structured(llm, PositionHoldReview, "position_review")
    verdict = invoke_structured(structured, prompt, "position_review")
    if verdict is None:
        # Prose fallback is never parsed into a mutation decision; the grace
        # window then falls back to the mechanical close (fail-closed).
        _persist_unavailable(
            position_key, payload, bridge, config, "structured review unavailable",
        )
        return
    _apply_verdict(position_key, payload, verdict, bridge, config)


def _apply_verdict(
    position_key: str,
    payload: dict,
    verdict: PositionHoldReview,
    bridge: Any,
    config: dict,
) -> None:
    now = datetime.now(UTC)
    reasoning = str(verdict.reasoning)[:_REASONING_LIMIT]
    record = {
        "at": now.isoformat(),
        "verdict": verdict.verdict,
        "reasoning": reasoning,
        "favorable_r": payload.get("favorable_r"),
        "funding_r": payload.get("funding_r"),
        "fee_r": payload.get("fee_r"),
    }
    if verdict.verdict == "close":
        saved = _merge_review_snapshot(position_key, lambda snapshot: {
            **snapshot,
            "observed_at": now.isoformat(),
            "time_stop_review_status": "close",
            "time_stop_review_last": record,
        })
        if not saved:
            logger.error("position review verdict could not be persisted for %s", position_key)
            return
        # No audit here: the monitor's close itself records time_stop_closed
        # with the review context in its reason.
        return
    hours = _clamp_review_hours(verdict.review_after_hours, config)
    record["review_after_hours"] = hours
    saved = _merge_review_snapshot(position_key, lambda snapshot: {
        **snapshot,
        "observed_at": now.isoformat(),
        "phase": "time_stop_review_held",
        "time_stop_review_status": "hold",
        "time_stop_review_until": (now + timedelta(hours=hours)).isoformat(),
        "time_stop_review_count": int(snapshot.get("time_stop_review_count") or 0) + 1,
        "time_stop_review_last": record,
    })
    if not saved:
        logger.error("position review verdict could not be persisted for %s", position_key)
        return
    _audit_review(
        bridge, payload, "time_stop_review_hold",
        f"review says hold: {reasoning}; next review in {hours:.0f}h",
    )


def _merge_review_snapshot(position_key: str, apply: Callable[[dict], dict]) -> bool:
    for _ in range(_SAVE_ATTEMPTS):
        state = job_store.load_position_state(position_key)
        if state is None:
            # The position closed or was cleaned while the review ran.
            return False
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        merged = {**state, "snapshot": apply(snapshot)}
        if job_store.save_position_state(position_key, state["revision"], merged):
            return True
        time.sleep(_CAS_BACKOFF_SECONDS)
    return False


def _persist_unavailable(
    position_key: str,
    payload: dict,
    bridge: Any,
    config: dict,
    detail: str,
) -> None:
    now = datetime.now(UTC)
    grace = _grace_hours(config)
    saved = _merge_review_snapshot(position_key, lambda snapshot: {
        **snapshot,
        "observed_at": now.isoformat(),
        "phase": "time_stop_review_unavailable",
        "time_stop_review_status": "unavailable",
        "time_stop_review_until": (now + timedelta(hours=grace)).isoformat(),
        "time_stop_review_last": {
            "at": now.isoformat(),
            "verdict": "unavailable",
            "reasoning": str(detail)[:_REASONING_LIMIT],
        },
    })
    if saved:
        _audit_review(bridge, payload, "time_stop_review_unavailable",
                      f"position review unavailable ({detail}); grace {grace:.1f}h")


def _clamp_review_hours(hours: float | None, config: dict) -> float:
    if hours is None or not math.isfinite(hours):
        default = _positive_or_none(
            config.get("risk_derivatives_time_stop_review_interval_hours", 24.0),
        )
        hours = default if default is not None else 24.0
    return min(max(hours, _MIN_REVIEW_HOURS), _MAX_REVIEW_HOURS)


def _audit_review(bridge: Any, payload: dict, action: str, reason: str) -> None:
    audit = getattr(bridge, "audit_plan", None)
    if not callable(audit):
        return
    symbol = str(payload.get("symbol") or "")
    if not symbol:
        return
    audit(
        PlannedOrder(
            ticker=symbol,
            ccxt_symbol=symbol,
            signal="time_stop",
            side=str(payload.get("close_side") or (
                "sell" if payload.get("side") == "long" else "buy"
            )),
            quantity=float(payload.get("contracts") or 0.0),
            estimated_price=float(payload.get("mark") or 0.0),
            market="derivatives",
        ),
        action=action,
        confirmed=True,
        order_id=payload.get("bracket_task_id"),
        reason=reason,
        phase="execute",
    )


# ---------------------------------------------------------------------------
# Evidence packet and prompt
# ---------------------------------------------------------------------------


def _build_prompt(payload: dict, bridge: Any) -> str:
    client = _safe_client(bridge)
    symbol = str(payload.get("symbol") or "")
    now = datetime.now(UTC)
    sections = [
        "You are the risk reviewer for one open leveraged futures position whose "
        "time limit has expired. Decide whether the position should keep being "
        "held or be closed now.",
        _position_section(payload),
        _funding_section(payload, client, symbol, now),
        _market_section(client, symbol, now),
        _thesis_section(symbol),
        _decision_rules(payload),
        NO_EXTERNAL_TOOLS,
    ]
    return "\n\n".join(section for section in sections if section)


def _safe_client(bridge: Any) -> Any | None:
    try:
        return bridge.client()
    except Exception:
        return None


def _position_section(payload: dict) -> str:
    side = str(payload.get("side") or "unknown")
    return "\n".join([
        "POSITION",
        f"- symbol: {payload.get('symbol')} ({side})",
        f"- entry price: {_num(payload.get('entry'))}, current mark: {_num(payload.get('mark'))}",
        f"- held hours: {_num(payload.get('held_hours'))} of a {_num(payload.get('max_hold_hours'))} h deadline",
        f"- price PnL: {_num(payload.get('favorable_r'))} R of initial risk",
        "- cumulative funding: "
        f"{_num(payload.get('funding_r'))} R (positive = received), "
        f"round-trip fee: {_num(payload.get('fee_r'))} R",
        f"- size: {_num(payload.get('contracts'))} contracts; "
        f"completed scale-out tranches: {len(payload.get('completed_targets') or [])}",
    ])


def _funding_section(payload: dict, client: Any, symbol: str, now: datetime) -> str:
    lines = ["FUNDING"]
    schedule = funding_schedule(client, symbol, now) if client is not None else None
    rate = schedule.get("rate") if schedule else None
    next_at = schedule.get("next_funding_at") if schedule else None
    side = str(payload.get("side") or "")
    if rate is None:
        lines.append("- current funding rate: unavailable")
    else:
        paying = (side == "long" and rate > 0) or (side == "short" and rate < 0)
        lines.append(
            f"- current funding rate: {rate:+.6f} — this {side or 'position'} is "
            f"{'PAYING' if paying else 'RECEIVING'} funding at the next settlement"
        )
    lines.append(
        f"- next settlement: {next_at.isoformat() if next_at else 'unavailable'}"
    )
    note = payload.get("funding_note")
    if note:
        lines.append(f"- note: {note}")
    return "\n".join(lines)


def _market_section(client: Any, symbol: str, now: datetime) -> str:
    lines = ["MARKET"]
    if client is None or not symbol:
        lines.append("- market data unavailable")
        return "\n".join(lines)
    for timeframe, window in (("1h", 24), ("4h", 12)):
        summary = _candle_summary(client, symbol, timeframe, now, window)
        lines.append(f"- {timeframe} closed candles (last {window}): {summary}")
    metrics = _futures_derivative_metrics(client, symbol)
    lines.append(
        f"- venue metrics: funding rate {_num(metrics.get('funding_rate'))}, "
        f"mark {_num(metrics.get('mark_price'))}, "
        f"open interest {_num(metrics.get('open_interest'))}"
    )
    return "\n".join(lines)


def _candle_summary(client: Any, symbol: str, timeframe: str, now: datetime, window: int) -> str:
    try:
        candles = fetch_closed_candles(client, symbol, timeframe, as_of=now, limit=window)
    except Exception:
        return "unavailable"
    if not candles or not candles[0].close:
        return "unavailable"
    first, last = candles[0], candles[-1]
    change = (last.close - first.close) / first.close * 100.0
    ups = sum(1 for candle in candles if candle.close > candle.open)
    return (
        f"close {last.close:g}, net {change:+.1f}% over the window, "
        f"{ups}/{len(candles)} candles closed up"
    )


def _thesis_section(symbol: str) -> str:
    lines = [
        "ORIGINAL ENTRY THESIS (the portfolio manager's note when this position was opened)",
    ]
    note = _entry_thesis(symbol)
    lines.append(note if note else "- unavailable")
    return "\n".join(lines)


def _entry_thesis(symbol: str) -> str:
    if not symbol:
        return ""
    try:
        rows = job_store.list_futures_cycles(limit=20, symbol=symbol)
    except Exception:
        return ""
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if payload.get("execution_status") != "executed":
            continue
        note = str(payload.get("agent_decision_note") or "").strip()
        if note:
            return note[:_ENTRY_NOTE_LIMIT]
    return ""


def _decision_rules(payload: dict) -> str:
    return "\n".join([
        "DECISION RULES",
        "- The protective stop-loss and take-profit stay active either way; you "
        "are only deciding the time-based exit of a position that never reached "
        f"its minimum R ({_num(payload.get('time_stop_min_r'))}) within "
        f"{_num(payload.get('max_hold_hours'))} h.",
        "- HOLD when the original thesis still stands, recent market structure "
        "still supports the position's direction, and the carrying cost (funding "
        "the position PAYS) does not erode the remaining risk budget. Set "
        "review_after_hours to when it should be re-examined (4-72 hours).",
        "- CLOSE when the thesis is broken, the position stagnates against its "
        "direction while paying funding, or the remaining risk no longer "
        "justifies the potential upside. Receiving funding is carry income, "
        "never a substitute for a working thesis.",
        "- Answer with verdict (\"hold\" or \"close\"), a concise factual "
        "reasoning, and review_after_hours (hours until the next review; omit "
        "for CLOSE).",
    ])


def _num(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "unavailable"
    return f"{number:+.3f}" if math.isfinite(number) else "unavailable"
