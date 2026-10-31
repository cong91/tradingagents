"""Venue funding reads and the pure settlement-postpone decision.

The monitor's time stop (futures_protection.py) consults this module only at
the deadline: once to convert accumulated funding into risk units, and once to
decide whether an imminent settlement that would PAY the position justifies
holding it past the time stop. Every venue read fails closed — a failed read
is ``None``, never a guessed number, and the postpone verdict defaults to
"close" whenever the data behind it is missing.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

_FUNDING_HISTORY_LIMIT = 1000

# No venue settles less often than every two days; a "next settlement"
# further out than that is a malformed timestamp, not a live schedule.
_MAX_SCHEDULE_HORIZON_HOURS = 48.0

# A postpone window that spans a whole funding cycle would defer the close
# forever, one settlement at a time. Venues settle at least daily; when the
# payload publishes no interval, that loosest common cycle bounds the window.
_MAX_ASSUMED_INTERVAL_HOURS = 24.0


def funding_schedule(client: Any, symbol: str, now: datetime) -> dict | None:
    """The venue's next funding settlement for ``symbol``, or ``None``.

    Returns ``{"next_funding_at": datetime | None, "rate": float | None,
    "interval_hours": float | None}`` parsed from the client's funding-rate
    payload (Binance reports the upcoming settlement in ``fundingTimestamp``
    / ``info.nextFundingTime``, other venues in ``nextFunding*``). A client
    error or a non-dict payload reads as ``None``; a field that fails to
    parse reads as absent, and a settlement timestamp further than
    ``_MAX_SCHEDULE_HORIZON_HOURS`` out voids the whole schedule — fail-closed.
    """
    try:
        fetch = getattr(client, "fetch_funding_rate", None)
        if not callable(fetch):
            return None
        payload = fetch(symbol, params={"subType": "linear"})
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    next_funding_at = _next_funding_at(payload)
    if next_funding_at is not None:
        reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if (next_funding_at - reference.astimezone(UTC)).total_seconds() > (
            _MAX_SCHEDULE_HORIZON_HOURS * 3600.0
        ):
            return None
    return {
        "next_funding_at": next_funding_at,
        "rate": _finite_number(payload.get("fundingRate")),
        "interval_hours": _interval_hours(payload),
    }


def evaluate_time_stop_postpone(
    *,
    side: str,
    rate: float | None,
    next_funding_at: datetime | None,
    now: datetime,
    postpone_minutes: float,
    interval_hours: float | None = None,
) -> Literal["close", "postpone"]:
    """Pure time-stop verdict at the settlement boundary.

    ``rate`` follows the venue's sign convention: positive means longs pay
    shorts, so a short receives it. Only a position that would be paid at a
    settlement inside the postpone window is held past the time stop; anything
    unknown, paying, already settled, or outside the window closes. A
    non-positive window disables the postpone entirely, and a window spanning
    a whole funding cycle is refused: it would defer the close one settlement
    at a time, forever. Without a published interval the loosest common cycle
    (``_MAX_ASSUMED_INTERVAL_HOURS``) bounds the window.
    """
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    window = _finite_number(postpone_minutes)
    if window is None or window <= 0:
        return "close"
    interval = _finite_number(interval_hours) or _MAX_ASSUMED_INTERVAL_HOURS
    if window >= interval * 60.0:
        return "close"
    funding_rate = _finite_number(rate)
    if funding_rate is None or next_funding_at is None:
        return "close"
    paying = (side == "long" and funding_rate > 0) or (
        side == "short" and funding_rate < 0
    )
    if paying:
        return "close"
    settlement = (
        next_funding_at
        if next_funding_at.tzinfo is not None
        else next_funding_at.replace(tzinfo=UTC)
    ).astimezone(UTC)
    reference = (now if now.tzinfo is not None else now.replace(tzinfo=UTC)).astimezone(UTC)
    minutes_to = (settlement - reference).total_seconds() / 60.0
    if minutes_to <= 0 or minutes_to > window:
        return "close"
    return "postpone"


def fetch_funding_income(client: Any, symbol: str, since_ms: int) -> list | None:
    """The symbol's FUNDING_FEE income rows from ``since_ms`` on.

    ``[]`` means the venue publishes no income history (nothing to read,
    nothing failed); ``None`` means the read failed or came back in an
    unusable shape. The row amounts already carry their sign — a short that
    received funding reports positive amounts.
    """
    try:
        fetch = getattr(client, "fetch_funding_history", None)
        if not callable(fetch):
            return []
        rows = fetch(symbol, since_ms, _FUNDING_HISTORY_LIMIT, {})
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def _next_funding_at(payload: dict) -> datetime | None:
    """Parse the upcoming settlement, preferring explicit next-* fields.

    Binance maps the upcoming settlement onto ``fundingTimestamp`` /
    ``fundingDatetime`` while leaving ``nextFunding*`` null; on other venues
    ``funding*`` may hold the previous settlement, so the ``next*`` fields
    must win in both encodings before those fallbacks are tried.
    """
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    for value, parse in (
        (payload.get("nextFundingTimestamp"), _datetime_from_ms),
        (payload.get("nextFundingDatetime"), _parse_iso_datetime),
        (payload.get("fundingTimestamp"), _datetime_from_ms),
        (payload.get("fundingDatetime"), _parse_iso_datetime),
        (info.get("nextFundingTime"), _datetime_from_ms),
    ):
        parsed = parse(value)
        if parsed is not None:
            return parsed
    return None


def _datetime_from_ms(value) -> datetime | None:
    milliseconds = _timestamp_ms(value)
    if milliseconds is None:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        # Out-of-range garbage (a millisecond count millennia long) must read
        # as absent, never escape the fail-closed parse.
        return None


def _timestamp_ms(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def _parse_iso_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        # Python 3.10's fromisoformat does not accept the RFC 3339 suffix.
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _interval_hours(payload: dict) -> float | None:
    interval = payload.get("interval")
    if isinstance(interval, str) and interval.strip().lower().endswith("h"):
        try:
            hours = float(interval.strip().lower()[:-1])
        except ValueError:
            hours = None
        if hours is not None and math.isfinite(hours) and hours > 0:
            return hours
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    return _finite_number(info.get("fundingIntervalHours"))


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
