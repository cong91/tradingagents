"""Holding cost of an open futures position.

The portfolio response reports what a position costs to keep: when it was
opened, how many hours it has been held, the funding paid or received since
that entry, and an estimated round-trip fee. Nothing here decides anything —
the monitor's time stop is the authority on age (futures_protection.py) — so
this module only derives display figures.

Two rules shape the arithmetic. A missing or malformed input stays ``None``
instead of becoming a guessed number: a null says "unknown", while a zero would
claim "free". And a venue read that fails only nulls the field it fed, leaving
the rest of the portfolio snapshot intact.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from server import job_store

# Position keys are "<ccxt_symbol>:<order side>:<entry order id>" (derivatives.py:577);
# the venue reports the position's own side, so the stored token needs mapping.
_ORDER_SIDE_TO_POSITION_SIDE = {"buy": "long", "sell": "short"}

_FUNDING_HISTORY_LIMIT = 1000

UNAVAILABLE_COST = {
    "opened_at": None,
    "held_hours": None,
    "funding_usdt": None,
    "round_trip_fee_usdt": None,
}


def _finite_number(value) -> float | None:
    """A finite float or None; venue payloads and config values are untrusted."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_timestamp(value) -> datetime | None:
    """Parse a stored ISO-8601 timestamp as UTC; anything malformed is absent."""
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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def position_cost(notional, rate, income_rows, opened_at, now) -> dict:
    """Derive one position's holding cost; pure, so tests need no venue.

    ``income_rows`` are the venue's FUNDING_FEE entries already scoped to this
    position by the caller (``None`` when the read failed or the window is
    unknown), ``opened_at`` the raw stored timestamp and ``rate`` the
    configured per-side fee rate. Anything missing or malformed stays ``None``
    instead of becoming a guessed number: a null says "unknown", a zero would
    claim "free".
    """
    opened = parse_timestamp(opened_at)

    held_hours = None
    if opened is not None and isinstance(now, datetime):
        reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        # Clamped like the monitor's own age (futures_protection.py:170): a clock
        # skew must not render as a negative holding time.
        held_hours = max(
            0.0, (reference.astimezone(timezone.utc) - opened).total_seconds() / 3600.0
        )

    round_trip_fee = None
    size = _finite_number(notional)
    fee_rate = _finite_number(rate)
    if size is not None and fee_rate is not None and fee_rate >= 0:
        round_trip_fee = abs(size) * fee_rate * 2  # one side at entry, one at exit

    funding = None
    if income_rows is not None:
        funding = 0.0
        for row in income_rows:
            if not isinstance(row, dict):
                continue
            amount = _finite_number(row.get("amount"))
            if amount is not None:
                funding += amount

    return {
        # The stored text is echoed back as-is; an unparseable value is not a date.
        "opened_at": opened_at.strip() if opened is not None else None,
        "held_hours": held_hours,
        "funding_usdt": funding,
        "round_trip_fee_usdt": round_trip_fee,
    }


def _active_states() -> list[dict]:
    """Durable position states, or none when the store is unavailable.

    The portfolio endpoint stays useful without a database: an unreadable
    state store only makes the holding time unknown.
    """
    try:
        return job_store.active_position_states()
    except Exception:
        return []


def _load_state(position_key: str) -> dict | None:
    """One durable state; a store hiccup only makes the timestamp unknown."""
    try:
        return job_store.load_position_state(position_key)
    except Exception:
        return None


def _opened_at_for(states: list[dict], symbol: str | None, side: str | None) -> str | None:
    """The recorded entry timestamp for this venue position, newest state first.

    ``position_key`` is ``"<symbol>:<order side>:<entry order id>"``; the venue
    position carries no order id, so the key is matched on symbol + side and
    then re-read through ``load_position_state`` for its current revision. Only
    the newest matching state is consulted: an older position's timestamp must
    never be passed off as this one's.
    """
    if not symbol or not side:
        return None
    for state in reversed(states):
        key = state.get("position_key")
        if not isinstance(key, str):
            continue
        parts = key.rsplit(":", 2)
        if len(parts) != 3 or parts[0] != symbol:
            continue
        snapshot = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
        state_side = str(
            snapshot.get("side") or _ORDER_SIDE_TO_POSITION_SIDE.get(parts[1], parts[1])
        )
        if state_side != side:
            continue
        stored = _load_state(key)
        if isinstance(stored, dict) and isinstance(stored.get("snapshot"), dict):
            snapshot = stored["snapshot"]
        opened = snapshot.get("opened_at")
        return opened.strip() if isinstance(opened, str) and opened.strip() else None
    return None


def _funding_income_rows(bridge, symbol: str, since_ms: int, warnings: list[str]):
    """The symbol's FUNDING_FEE income rows from ``since_ms`` on, or None.

    A venue error only appends a warning: the caller keeps a null funding
    figure and the rest of the snapshot stays intact.
    """
    try:
        fetch = getattr(bridge.client(), "fetch_funding_history", None)
        if not callable(fetch):
            # The venue publishes no income history: nothing to show, nothing failed.
            return None
        rows = fetch(symbol, since_ms, _FUNDING_HISTORY_LIMIT, {})
    except Exception as exc:
        warnings.append(f"funding history unavailable for {symbol}: {type(exc).__name__}")
        return None
    return rows if isinstance(rows, list) else None


def attach_position_costs(
    futures: dict, bridge, config: dict, warnings: list[str],
) -> None:
    """Attach held time, funding and round-trip fee to each futures position."""
    positions = futures.get("positions")
    if not isinstance(positions, list) or not positions:
        return
    now = datetime.now(timezone.utc)
    rate = config.get("risk_derivatives_round_trip_fee_rate", 0.001)
    states = _active_states()
    for position in positions:
        if not isinstance(position, dict):
            continue
        position.update(UNAVAILABLE_COST)
        symbol = position.get("symbol")
        try:
            opened_at = _opened_at_for(states, symbol, position.get("side"))
            opened = parse_timestamp(opened_at)
            # The venue filters the income window itself; without a known entry
            # time there is no defensible window, so funding stays unknown
            # rather than summing an earlier position's rows on the same symbol.
            rows = (
                _funding_income_rows(bridge, symbol, int(opened.timestamp() * 1000), warnings)
                if opened is not None and isinstance(symbol, str) and symbol else None
            )
            position.update(position_cost(
                position.get("notional"), rate, rows, opened_at, now,
            ))
        except Exception as exc:  # display fields must never sink the snapshot
            warnings.append(
                f"position cost unavailable for {symbol or 'unknown'}: {type(exc).__name__}"
            )
            position.update(UNAVAILABLE_COST)
