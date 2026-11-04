"""Process-local state for read-only performance history requests."""

from __future__ import annotations

import copy
import hashlib
import hmac
import math
import os
import re
import secrets
import threading
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
from time import monotonic, time as wall_time

from tradingagents.execution.exchange_client import credential_env_names

CacheKey = tuple[str, str, str, tuple[str, ...]]

HISTORY_LOCK = threading.Lock()
_COOLDOWN_LOCK = threading.Lock()
_CACHE_SECRET = secrets.token_bytes(32)
_HISTORY_CACHE: dict[CacheKey, tuple[float, dict]] = {}
_CACHE_TTL = 240.0
_RATE_LIMIT_UNTIL = 0.0
_RATE_LIMIT_FALLBACK = 900.0


def credential_identity(config: dict) -> str | None:
    exchange_id = str(config.get("exec_exchange_id") or "").strip().upper()
    base_url = str(config.get("exec_base_url") or "").strip().lower()
    if not exchange_id:
        return None
    credential_base_url = base_url or (
        "https://demo-api.binance.com" if exchange_id == "BINANCE" else ""
    )
    env_names = credential_env_names(exchange_id, credential_base_url or None)
    values = tuple(os.environ.get(name, "") for name in env_names)
    if not all(values):
        return None
    payload = "\x00".join((exchange_id, credential_base_url, *values)).encode("utf-8", "strict")
    return hmac.new(_CACHE_SECRET, payload, hashlib.sha256).hexdigest()


def cache_key(config: dict, bridge, symbols: list[str]) -> CacheKey | None:
    identity = credential_identity(config)
    if identity is None:
        return None
    base_url = str(config.get("exec_base_url") or "").rstrip("/").lower()
    if not base_url and str(bridge.exchange_id).lower() == "binance":
        base_url = "https://demo-api.binance.com"
    return (
        str(bridge.exchange_id).lower(),
        base_url,
        identity,
        tuple(symbols),
    )


def client_cache_key(config: dict, bridge, client, symbols: list[str]) -> CacheKey | None:
    """Bind the environment identity to the credentials actually used for reads."""
    if str(config.get("exec_exchange_id") or "").strip().lower() != str(bridge.exchange_id).lower():
        return None
    key = cache_key(config, bridge, symbols)
    if key is None:
        return None
    api_key = getattr(client, "apiKey", None)
    secret = getattr(client, "secret", None)
    if not isinstance(api_key, str) or not api_key or not isinstance(secret, str) or not secret:
        return None
    exchange_id = str(config.get("exec_exchange_id") or "").strip().upper()
    base_url = str(config.get("exec_base_url") or "").strip().lower()
    credential_url = base_url or ("https://demo-api.binance.com" if exchange_id == "BINANCE" else "")
    payload = "\x00".join((exchange_id, credential_url, api_key, secret)).encode("utf-8", "strict")
    actual_identity = hmac.new(_CACHE_SECRET, payload, hashlib.sha256).hexdigest()
    return key if hmac.compare_digest(key[2], actual_identity) else None


def cache_response(key: CacheKey | None) -> dict | None:
    now_mono = monotonic()
    if key is None:
        return None
    cached = _HISTORY_CACHE.get(key)
    if cached is None:
        return None
    stored_at, payload = cached
    if now_mono - stored_at >= _CACHE_TTL:
        _HISTORY_CACHE.pop(key, None)
        return None
    return copy.deepcopy(payload)


def store_cache(key: CacheKey | None, payload: dict) -> None:
    now_mono = monotonic()
    if key is not None and payload.get("source") == "venue_fills" and payload.get("status") in {
        "ok", "partial"
    }:
        _HISTORY_CACHE.clear()
        _HISTORY_CACHE[key] = (now_mono, copy.deepcopy(payload))


def _retry_after_seconds(history: dict) -> float | None:
    value = history.get("retry_after_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(0.0, float(value))


def start_cooldown(history: dict | None = None) -> None:
    global _RATE_LIMIT_UNTIL

    retry_after = _retry_after_seconds(history) if isinstance(history, dict) else None
    with _COOLDOWN_LOCK:
        _RATE_LIMIT_UNTIL = max(
            _RATE_LIMIT_UNTIL, monotonic() + max(_RATE_LIMIT_FALLBACK, retry_after or 0.0)
        )


def is_rate_limited() -> bool:
    with _COOLDOWN_LOCK:
        return monotonic() < _RATE_LIMIT_UNTIL


def is_rate_limit_error(exc: Exception) -> bool:
    from ccxt import DDoSProtection, RateLimitExceeded

    if isinstance(exc, (DDoSProtection, RateLimitExceeded)):
        return True
    for source in (exc, getattr(exc, "response", None)):
        for name in ("status", "status_code", "http_status"):
            value = source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)
            if value in (418, 429):
                return True
    return bool(re.search(r"\b(?:HTTP\s*)?(?:418|429)\b", str(exc), re.IGNORECASE))


def _parse_retry_after(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except (TypeError, ValueError, OverflowError):
        seconds = None
    if seconds is not None and math.isfinite(seconds) and seconds >= 0:
        return seconds
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if retry_at.tzinfo is None:
        return None
    return max(0.0, retry_at.timestamp() - wall_time())


def retry_after_from_exception(exc: Exception, client=None) -> float | None:
    durations = []
    for source in (exc, getattr(exc, "response", None), client):
        headers = getattr(source, "headers", None)
        if not isinstance(headers, Mapping) and isinstance(source, Mapping):
            headers = source.get("headers", source)
        if not isinstance(headers, Mapping):
            headers = getattr(source, "last_response_headers", None)
        if not isinstance(headers, Mapping):
            continue
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "retry-after":
                duration = _parse_retry_after(value)
                if duration is not None:
                    durations.append(duration)
    match = re.search(r"\bIP banned until ([0-9]{13})(?![A-Za-z0-9_])", str(exc), re.IGNORECASE)
    if match:
        durations.append(max(0.0, int(match.group(1)) / 1000.0 - wall_time()))
    return max(durations) if durations else None
