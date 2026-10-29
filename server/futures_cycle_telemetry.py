"""Redacted persistence helpers for futures cycle decision telemetry."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from server import job_store
from server.contract import utc_now_iso
from server.futures_decision_context import effective_futures_leverage_cap

_SENSITIVE_KEY = re.compile(
    r"(?:api[_ -]?key|api[_ -]?secret|secret|password|passwd|passphrase|cookie|"
    r"authorization|credential|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|"
    r"private[_ -]?key|(?:^|[^a-z])token(?:[^a-z]|$))",
    re.IGNORECASE,
)
# Assignment scrub covers the shapes secrets actually travel in: plain
# ``key=value``, YAML/HTTP ``key: value``, and JSON ``"key": "value"`` —
# including separator-less camelCase (apiSecret) and quoted values.
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|api[_ -]?secret|secret(?:[_ -]?key)?|password|"
    r"passwd|passphrase|cookie|authorization|credential|access[_ -]?token|"
    r"refresh[_ -]?token|auth[_ -]?token|private[_ -]?key|token)[\"']?)"
    r"\s*[:=]\s*[\"']?[^\s,;\"'}]+[\"']?"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
# Exception text is the leak channel venue/HTTP errors actually use
# ("HTTPError 401 for key <value>", "venue rejected key: <value>"): no
# assignment separator, just a credential keyword followed by a long
# credential-charset token. The 8-char floor keeps counters ("token 4096")
# and short symbols ("for key BTC-USD") intact; over-redaction here is the
# safe direction.
_KEY_PROSE = re.compile(
    r"(?i)(?P<prefix>\b(?:api[ _-]?key|apikey|keys?|secret|token|auth[ _-]?token|"
    r"private[ _-]?key|passphrase)[\"']?\s*(?:is|:|=)?\s*[\"']?)"
    r"(?P<value>[A-Za-z0-9._~+/=-]{8,})"
)
_KNOWN_SECRET_VALUE = re.compile(
    r"\b(?:sk-(?:proj-)?|xai-|AIza|gh[pousr]_)[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)


def redact_sensitive_text(value: str) -> str:
    # Bearer tokens first: the assignment scrub consumes the "Bearer" keyword
    # and would leave the token body exposed behind it.
    value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1=[REDACTED]", value)
    value = _KEY_PROSE.sub(r"\g<prefix>[REDACTED]", value)
    return _KNOWN_SECRET_VALUE.sub("[REDACTED]", value)


def redact_payload(value: Any) -> Any:
    """Recursively omit credential-shaped fields and scrub recognizable values.

    Also folds the payload into JSON-safe types: venue data may carry Decimal,
    datetime, set or bytes, and ``job_store._json`` would raise TypeError on
    them — a crash here would take the whole cycle claim down.
    """
    if isinstance(value, dict):
        return {
            str(key): redact_payload(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [redact_payload(item) for item in value]
    if isinstance(value, Decimal):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return redact_sensitive_text(value.decode("utf-8", "replace"))
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _candle_open(context: dict, timeframe: str) -> datetime:
    value = context.get(f"{timeframe}_open")
    if value is None:
        value = getattr(context.get(timeframe), "open_time", None)
    if isinstance(value, datetime):
        opened = value
    elif isinstance(value, str):
        opened = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        opened = datetime.fromtimestamp(value / 1000, timezone.utc)
    else:
        raise ValueError(f"futures context is missing the {timeframe} candle open time")
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return opened.astimezone(timezone.utc)


def cycle_identity(context: dict) -> tuple[str, str, str]:
    """Return durable cycle id and normalized candle-open timestamps."""
    symbol = str(context["unified_symbol"])
    one_hour_open = _candle_open(context, "one_hour").isoformat()
    four_hour_open = _candle_open(context, "four_hour").isoformat()
    cycle_id = f"futures:{symbol}:{four_hour_open}:{one_hour_open}:1"
    return cycle_id, four_hour_open, one_hour_open


def effective_caps(
    derivatives: Any, *, auto_cap: float, config: dict | None = None,
) -> dict[str, float]:
    limits = derivatives.risk_guard.limits
    caps = {
        "effective_leverage": effective_futures_leverage_cap(
            risk_guard_cap=float(limits.max_derivatives_leverage),
            auto_cap=auto_cap,
        ),
        "risk_guard_max_leverage": float(limits.max_derivatives_leverage),
        "auto_max_leverage": float(auto_cap),
        "max_derivatives_exposure_pct": float(limits.max_derivatives_exposure_pct),
        "max_position_pct_per_asset": float(limits.max_position_pct_per_asset),
        "max_total_exposure_pct": float(limits.max_total_exposure_pct),
    }
    if config is not None:
        caps["max_risk_fraction"] = float(config.get("risk_derivatives_max_risk_fraction", 0.02))
        caps["max_margin_fraction"] = float(
            config.get("risk_derivatives_max_margin_fraction", 0.5)
        )
    return caps


def redact_proposal(proposal: Any) -> dict:
    """Keep only typed numeric execution terms; discard rationale and free text."""
    if not isinstance(proposal, dict):
        return {}
    allowed = (
        "margin_mode", "leverage", "risk_fraction", "margin_fraction",
        "entry_price", "stop_loss", "take_profit",
    )
    sanitized = {}
    for key in allowed:
        value = proposal.get(key)
        if key == "margin_mode":
            if isinstance(value, str) and value in ("isolated", "cross"):
                sanitized[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(float(value)):
                sanitized[key] = value
        elif key in proposal and value is None:
            # Only null out terms the agent explicitly set; absent terms stay
            # absent instead of becoming null noise.
            sanitized[key] = None
    return sanitized


def decision_telemetry(state: dict, signal: str, rating_parser) -> dict:
    trader = state.get("trader_decision_telemetry")
    pm = state.get("pm_decision_telemetry")
    if not isinstance(trader, dict):
        trader = {}
    if not isinstance(pm, dict):
        pm = {}
    proposal = state.get("futures_execution_proposal")
    final_decision = state.get("final_trade_decision")
    trader_intent = trader.get("futures_execution_intent")
    if trader_intent not in {"enter", "manage_existing", "abstain", "structured_terms_unavailable"}:
        trader_intent = "structured_terms_unavailable"
    pm_intent = state.get("futures_execution_intent")
    if pm_intent not in {"enter", "manage_existing", "abstain", "structured_terms_unavailable"}:
        pm_intent = "structured_terms_unavailable"
    pm_status = state.get("futures_execution_status")
    if pm_status not in {"structured", "fallback_report_only", "not_applicable"}:
        pm_status = "not_applicable"
    rating = pm.get("rating") or rating_parser(str(final_decision or ""))
    if rating not in {"Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"}:
        rating = "REVIEW"
    trader_action = trader.get("action")
    if trader_action not in {"Buy", "Hold", "Sell"}:
        trader_action = None
    pm_proposal_present = pm.get("futures_execution_proposal_present")
    if not isinstance(pm_proposal_present, bool):
        pm_proposal_present = isinstance(proposal, dict) and bool(proposal)
    return {
        "trader_decision": {
            "action": trader_action,
            "futures_execution_intent": trader_intent,
            "futures_execution_proposal_present": bool(
                trader.get("futures_execution_proposal_present")
            ),
            "structured_output": bool(trader.get("structured_output")),
        },
        "pm_decision": {
            "rating": rating,
            "futures_execution_intent": pm_intent,
            "futures_execution_status": pm_status,
            "futures_execution_proposal_present": pm_proposal_present,
            "structured_output": bool(pm.get("structured_output")),
        },
        "agent_proposal": redact_proposal(proposal),
        "signal": signal,
        "confirmation_result": None,
        "confirmation_reason": None,
        "confirmation": {"result": None, "reason": None},
    }


def snapshot_payload(context: dict) -> dict:
    def encode(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {key: encode(getattr(value, key)) for key in value.__dataclass_fields__}
        if isinstance(value, dict):
            return {str(key): encode(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [encode(item) for item in value]
        return value

    return redact_payload(encode(context))


def claim_cycle(
    cycle_id: str,
    *,
    symbol: str,
    four_hour_open: str,
    one_hour_open: str,
    payload: dict,
    strategy_version: str = "1",
) -> bool:
    return job_store.claim_futures_cycle(
        cycle_id,
        symbol=symbol,
        four_hour_open=four_hour_open,
        one_hour_open=one_hour_open,
        strategy_version=strategy_version,
        payload=redact_payload(payload),
    )


def persist_progress(
    cycle_id: str,
    *,
    status: str = "running",
    phase: str,
    payload: dict,
    timestamp: str | None = None,
    terminal: bool = False,
) -> None:
    timestamp = timestamp or utc_now_iso()
    phase_timestamps = dict(payload.get("phase_timestamps") or {})
    phase_timestamps[f"{phase}_updated_at"] = timestamp
    if terminal or status != "running":
        phase_timestamps[f"{phase}_finished_at"] = timestamp
    job_store.update_futures_cycle(
        cycle_id,
        status=status,
        phase=phase,
        payload=redact_payload({**payload, "phase_timestamps": phase_timestamps}),
        terminal=terminal,
    )


def persist_rejection(
    cycle_id: str,
    *,
    symbol: str,
    four_hour_open: str,
    one_hour_open: str,
    phase: str,
    payload: dict,
    status: str = "rejected",
) -> None:
    claim_cycle(
        cycle_id,
        symbol=symbol,
        four_hour_open=four_hour_open,
        one_hour_open=one_hour_open,
        payload=payload,
    )
    timestamp = utc_now_iso()
    phase_timestamps = {
        **dict(payload.get("phase_timestamps") or {}),
        f"{phase}_finished_at": timestamp,
    }
    job_store.update_futures_cycle(
        cycle_id,
        status=status,
        phase=phase,
        payload=redact_payload({**payload, "phase_timestamps": phase_timestamps}),
        terminal=True,
    )


__all__ = [
    "claim_cycle",
    "cycle_identity",
    "decision_telemetry",
    "effective_caps",
    "persist_progress",
    "persist_rejection",
    "redact_payload",
    "redact_proposal",
    "redact_sensitive_text",
    "snapshot_payload",
]
