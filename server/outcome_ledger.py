"""Background outcome-ledger worker: attribution of recorded cycles to fills.

A worker of its own, independent of the autoloop (plan §2.6): it reads only
durable state (terminal ``futures_cycles`` rows, the entry journal) and venue
READ history (trade fills and funding income for the cycle's own window). It
never sends an order, never touches risk limits, never blocks the scheduler,
and never adds an observer call into the autoloop. Ingest is off by default
(``outcome_ledger_ingest_enabled``); when off, the loop does nothing.

Restart catch-up: the cursor is the last processed (updated_at, cycle_id)
pair, persisted in outcome_ledger_state, so a restart resumes after it —
cycles completed while the worker was down are covered on the next poll, and
the first run after the flag is enabled starts at "now" (no backfill, no
retro-attribution of history).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from server import outcome_store
from server.outcome_closure import fill_history_complete as verify_fill_history_complete
from server.outcome_poll_lock import PollLockBusyError, PollLockError, poll_lock
from server.outcome_venue_reads import read_venue_window
from tradingagents.dataflows.config import get_config
from tradingagents.execution.futures_entry_journal import (
    record_entry_provenance,
    valid_managed_live_provenance,
)
from tradingagents.outcomes.attribution import (
    CycleOrder,
    attribute_exit_fill,
    attribute_fill,
    attribute_recovered_fill,
    bracket_exit_order_ids,
    recovered_client_order_ids,
)
from tradingagents.outcomes.coverage import build_coverage
from tradingagents.outcomes.currency import CurrencyAmount
from tradingagents.outcomes.netting import (
    REALIZED_PNL_EXCLUDES_FEE,
    classify_exit_leg,
    partition_position_fills,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_BATCH_SIZE = outcome_store.DEFAULT_BATCH_SIZE
# A cycle whose processing raises is retried on the next poll up to this many
# attempts; past the bound the failure is published durably (coverage row,
# status "error") and the cursor moves on — a poison cycle must be visible,
# never silently dropped, and never able to block the pipeline forever.
MAX_CYCLE_ATTEMPTS = 3

FillSource = Callable[[str, str, int, int], dict]

_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def ingest_enabled(config: dict | None = None) -> bool:
    return (config or get_config()).get("outcome_ledger_ingest_enabled") is True


def interval_seconds(config: dict | None = None) -> float:
    value = (config or get_config()).get("outcome_ledger_interval_seconds")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return number if number > 0 else DEFAULT_INTERVAL_SECONDS


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ArithmeticError):
        return None
    return number if number.is_finite() else None


def account_identity_from_marker(marker: dict) -> str | None:
    """The stable logical account id carried by a valid entry marker."""
    identity = marker.get("account_identity")
    if not isinstance(identity, dict):
        return None
    exchange, profile, digest = (
        identity.get("exchange"), identity.get("profile"), identity.get("hash"),
    )
    if not all(isinstance(value, str) and value for value in (exchange, profile, digest)):
        return None
    return f"{exchange}:{profile}:{digest}"


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _closure_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize mutable cycle/journal evidence for the final write fence."""
    quantity = row.get("quantity")
    return {
        "task_id": row.get("task_id"),
        "symbol": row.get("symbol"),
        "entry_order_id": row.get("entry_order_id"),
        "close_side": row.get("close_side"),
        "quantity": str(quantity) if quantity is not None else None,
        "legs": row.get("legs"),
        "placed_ids": sorted(str(item) for item in (row.get("placed_ids") or [])),
        "status": row.get("status"),
        "venue_key": row.get("venue_key"),
        "account_identity_exchange": row.get("account_identity_exchange"),
        "account_identity_profile": row.get("account_identity_profile"),
        "account_identity_type": row.get("account_identity_type"),
        "account_identity_hash": row.get("account_identity_hash"),
    }


def _closure_evidence_snapshot(
    cycle: dict[str, Any], journal: dict[str, Any], bracket: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    return {
        "cycle": {
            "cycle_id": cycle.get("cycle_id"),
            "symbol": cycle.get("symbol"),
            "order_id": payload.get("order_id"),
            "entry_intent_id": payload.get("entry_intent_id"),
            "entry_provenance": payload.get("entry_provenance"),
        },
        "journal": _closure_evidence_row(journal),
        "bracket": _closure_evidence_row(bracket) if bracket is not None else None,
    }


def _process_cycle(
    cycle: dict[str, Any],
    *,
    fill_source: FillSource,
    ingest: bool,
) -> dict[str, Any]:
    """Attribute one terminal executed cycle to venue fills and record it.

    A cycle whose payload does not record a venue-confirmed executed order is
    skipped: nothing to attribute, no position row, no funnel change. Fills
    without evidence stay unattributed and land only in the coverage row.
    """
    payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
    order_id = payload.get("order_id")
    symbol = str(cycle.get("symbol") or "")
    if payload.get("execution_status") not in ("executed", "uncertain") or not symbol:
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "cycle_not_executed")
        return {"cycle_id": cycle.get("cycle_id"), "result": "skipped_not_executed"}

    payload_marker = payload.get("entry_provenance")
    if payload_marker is None:
        # Old cycles have no account/intent binding in their payload. An
        # order ID plus symbol can collide across accounts, even with one
        # matching journal row, so it is not attribution evidence.
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_marker_unavailable")
        return {"cycle_id": cycle.get("cycle_id"), "result": "unattributed_no_marker"}
    if (not valid_managed_live_provenance(payload_marker)
            or payload.get("entry_intent_id") != payload_marker.get("intent_id")
            or payload_marker.get("symbol") != symbol):
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_marker_invalid")
        return {"cycle_id": cycle.get("cycle_id"), "result": "unattributed_no_marker"}
    tasks = outcome_store.find_journal_tasks(symbol, intent_id=payload_marker["intent_id"])
    tasks = [task for task in tasks if record_entry_provenance(task) == payload_marker]
    if len(tasks) != 1:
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_journal_ambiguous")
        return {"cycle_id": cycle.get("cycle_id"), "result": "unattributed_ambiguous_journal"}
    journal = tasks[0]
    marker = record_entry_provenance(journal)
    account_id = account_identity_from_marker(marker) if isinstance(marker, dict) else None
    identity = marker.get("account_identity", {}) if isinstance(marker, dict) else {}
    if (not valid_managed_live_provenance(marker) or account_id is None
            or identity.get("type") not in {"futures", "usds_futures"} or marker.get("symbol") != symbol
            or marker.get("venue_key") != journal.get("venue_key")
            or marker.get("client_order_id") != journal.get("entry_order_id")
            or journal.get("close_side") != ("sell" if marker.get("side") == "buy" else "buy")
            or any(identity.get(key) != journal.get(f"account_identity_{key}")
                   for key in ("exchange", "profile", "type", "hash"))):
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_identity_mismatch")
        return {"cycle_id": cycle.get("cycle_id"), "result": "unattributed_no_marker"}
    placed_ids = journal.get("placed_ids") if isinstance(journal.get("placed_ids"), list) else []
    if order_id is None:
        if len(placed_ids) != 1 or not isinstance(placed_ids[0], str):
            outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_order_unavailable")
            return {"cycle_id": cycle.get("cycle_id"), "result": "pending_order_evidence"}
        order_id = placed_ids[0]
    elif str(order_id) not in placed_ids:
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "entry_order_mismatch")
        return {"cycle_id": cycle.get("cycle_id"), "result": "pending_order_evidence"}
    cycle_order = CycleOrder(
        cycle_id=str(cycle.get("cycle_id")), order_id=str(order_id), symbol=symbol,
        journal_task=journal,
    )

    # Exit evidence: the position's bracket task (keyed by the ENTRY venue
    # order id) holds the stop/target/scale-out order ids the runtime placed,
    # bound to the same account identity — the chain that makes the bot's own
    # exit fills attributable, so realized PnL is never lost to "manual".
    marker_hash = (marker or {}).get("account_identity", {}).get("hash")
    bracket_tasks = outcome_store.find_bracket_tasks(symbol, str(order_id))
    bracket_tasks = [task for task in bracket_tasks if (
        task.get("venue_key") == journal.get("venue_key")
        and task.get("account_identity_exchange") == identity.get("exchange")
        and task.get("account_identity_profile") == identity.get("profile")
        and task.get("account_identity_type") == identity.get("type")
        and task.get("account_identity_hash") == identity.get("hash")
    )]
    if len(bracket_tasks) > 1:
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "exit_evidence_ambiguous")
        return {"cycle_id": cycle_order.cycle_id, "result": "unattributed_ambiguous_bracket"}
    bracket_task = bracket_tasks[0] if bracket_tasks else None
    exit_evidence = bracket_exit_order_ids(
        bracket_task,
        entry_order_id=str(order_id),
        identity_hash=marker_hash,
        symbol=symbol,
    )

    opened_at = _parse_iso(cycle.get("created_at"))
    if opened_at is None:
        outcome_store.revoke_cycle_closure(str(cycle.get("cycle_id")), "cycle_timestamp_unavailable")
        return {"cycle_id": cycle_order.cycle_id, "result": "unavailable_cycle_timestamp"}
    now = datetime.now(UTC)
    requested_start = datetime.fromtimestamp(_ms(opened_at) / 1000, tz=UTC)
    requested_end = datetime.fromtimestamp(_ms(now) / 1000, tz=UTC)
    read_error: Exception | None = None
    try:
        if fill_source is read_venue_window:
            window = read_venue_window(account_id, symbol, _ms(opened_at), _ms(now),
                                       expected_venue=journal["venue_key"])
        else:
            window = fill_source(account_id, symbol, _ms(opened_at), _ms(now))
    except Exception as exc:  # noqa: BLE001 - persist non-proof state before retrying
        read_error = exc
        window = {
            "status": "error", "fills": [], "funding": None,
            "warnings": [f"fill_history_read_error:{type(exc).__name__}"],
        }
    source_status = str(window.get("status") or "unavailable")
    raw_fill_value = window.get("fills")
    raw_fills = raw_fill_value if isinstance(raw_fill_value, list) else []
    source_warnings = [str(item) for item in window.get("warnings") or []]
    warnings = list(source_warnings)
    warnings.extend(str(item) for item in window.get("funding_warnings") or [])
    actual_start = _parse_iso(window.get("start_at"))
    actual_end = _parse_iso(window.get("end_at"))
    source_start = actual_start or opened_at
    source_end = actual_end or now
    if (actual_start is None or actual_end is None or source_start > requested_start
            or source_end < requested_end):
        warnings.append("history_window_bounds_unverified")
    malformed_fills = int(not isinstance(raw_fill_value, list))
    duplicate_fills = unattributed = contradictory_fills = 0
    if malformed_fills:
        warnings.append("fill_list_shape_invalid")
    funding_events = _funding_rows(window.get("funding"), cycle_order.cycle_id, account_id, opened_at)
    # Funding uncertainty is visible in coverage but cannot alter fill completeness.
    warnings.append("funding_position_attribution_unknown")
    if window.get("funding") is None:
        warnings.append("funding_history_unavailable")
    elif not funding_events and isinstance(window.get("funding"), list) and window.get("funding"):
        warnings.append("funding_events_unusable")
    if ingest and funding_events:
        outcome_store.record_funding_observations(
            account_id, journal["venue_key"], identity["type"], symbol, funding_events,
        )

    managed = recovered = 0
    fill_rows: list[dict[str, Any]] = []
    seen_fill_ids: set[str] = set()
    side = "long" if (journal.get("close_side") == "sell") else "short"
    recovery_map = recovered_client_order_ids([journal])
    for raw in raw_fills:
        if not isinstance(raw, dict) or raw.get("symbol") != symbol:
            malformed_fills += 1
            warnings.append("fill_symbol_or_shape_invalid")
            continue
        trade_id = raw.get("trade_id")
        if not isinstance(trade_id, str) or not trade_id:
            malformed_fills += 1
            warnings.append("fill_missing_trade_id")
            continue
        if trade_id in seen_fill_ids:
            duplicate_fills += 1
            warnings.append("duplicate_fill_in_history")
            continue
        seen_fill_ids.add(trade_id)
        verdict = attribute_fill(raw.get("order_id"), symbol, cycle_order)
        if verdict.attribution == "unattributed":
            verdict = attribute_exit_fill(
                raw.get("order_id"), symbol, exit_evidence,
                cycle_order.client_order_id, cycle_order.cycle_id,
            )
        if verdict.attribution == "unattributed":
            verdict = attribute_recovered_fill(
                raw.get("order_id"), symbol, recovery_map, cycle_order.cycle_id
            )
        if verdict.attribution == "unattributed":
            unattributed += 1
            continue
        # Recovered entry orders cannot be exits merely because their side
        # resembles the close side; their order evidence identifies an entry.
        expected_side = marker["side"] if str(raw.get("order_id")) not in exit_evidence else (
            "sell" if marker["side"] == "buy" else "buy"
        )
        if raw.get("side") != expected_side:
            contradictory_fills += 1
            warnings.append("fill_side_contradicts_order_evidence")
            continue
        managed += verdict.attribution == "managed"
        recovered += verdict.attribution == "recovered"
        quantity = _decimal(raw.get("contracts"))
        price = _decimal(raw.get("price"))
        timestamp = raw.get("timestamp")
        if (quantity is None or quantity <= 0 or price is None or price <= 0
                or _parse_iso(timestamp) is None):
            malformed_fills += 1
            warnings.append("attributed_fill_malformed")
            continue
        fill_rows.append({
            "venue_trade_id": trade_id,
            "venue_order_id": raw.get("order_id"),
            "symbol": symbol,
            "side": raw.get("side"),
            "role": "entry",  # partitioned below against the position's side
            "quantity": quantity,
            "price": price,
            "fee": _decimal(raw.get("fee")),
            "fee_currency": raw.get("fee_currency"),
            "realized_pnl": _decimal(raw.get("realized_pnl")),
            "realized_pnl_currency": raw.get("realized_pnl_currency"),
            "realized_pnl_semantics": (
                REALIZED_PNL_EXCLUDES_FEE if identity.get("exchange") == "binance" else None
            ),
            "occurred_at": timestamp,
        })

    planned_quantity = _decimal(journal.get("quantity"))
    entries, exits_fills = partition_position_fills(side, fill_rows)
    filled_quantity = sum((row["quantity"] for row in entries), Decimal(0))
    partial_entry = bool(
        entries and planned_quantity is not None and filled_quantity < planned_quantity
    )
    fill_history = {
        "source_status": source_status,
        "source_certified_complete": window.get("fills_complete") is True,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "observed_start": actual_start.isoformat() if actual_start is not None else None,
        "observed_end": actual_end.isoformat() if actual_end is not None else None,
        "source_warnings": source_warnings,
        "funding_warnings": [
            *[str(item) for item in window.get("funding_warnings") or []],
            "funding_position_attribution_unknown",
        ],
        "malformed_fill_count": malformed_fills,
        "duplicate_fill_count": duplicate_fills,
        "unattributed_fill_count": unattributed,
        "contradictory_fill_count": contradictory_fills,
    }
    history_complete = verify_fill_history_complete(
        source_status=source_status,
        source_certified_complete=fill_history["source_certified_complete"],
        requested_start=requested_start,
        requested_end=requested_end,
        observed_start=actual_start,
        observed_end=actual_end,
        source_warnings=source_warnings,
        malformed_fill_count=malformed_fills,
        duplicate_fill_count=duplicate_fills,
        unattributed_fill_count=unattributed,
        contradictory_fill_count=contradictory_fills,
    )
    if fill_rows:
        for row in fill_rows:
            row["role"] = "entry" if row in entries else "exit"
    entry_legs = [
        leg for leg in journal.get("legs", [])
        if isinstance(leg, dict) and leg.get("name") == "entry-intent"
    ] if isinstance(journal.get("legs"), list) else []
    entry_order_final = (
        entry_legs[0].get("entry_order_final") if len(entry_legs) == 1 else None
    )
    position = {
        "cycle_id": cycle_order.cycle_id,
        "client_order_id": journal.get("entry_order_id"),
        "venue_order_id": str(order_id),
        "symbol": symbol,
        "market_type": "perpetual",
        "side": side,
        "planned_quantity": planned_quantity,
        "filled_quantity": filled_quantity,
        "opened_at": (_first_ocurred(entries) or opened_at).isoformat(),
        "closed_at": None,
        "reconcile": True,
        "history_complete": history_complete,
        "fill_history": fill_history,
        "entry_order_final": entry_order_final,
        "closure_identity": {
            "order_id": str(order_id),
            "client_order_id": journal.get("entry_order_id"),
            "symbol": symbol,
            "venue_key": journal.get("venue_key"),
            "account_id": account_id,
            "account_identity": identity,
        },
        "closure_evidence_snapshot": _closure_evidence_snapshot(cycle, journal, bracket_task),
        "closure_fill_facts": [
            {
                "trade_id": row["venue_trade_id"],
                "order_id": row.get("venue_order_id"),
                "symbol": row["symbol"],
                "side": row["side"],
                "role": "entry" if row in entries else "exit",
                "quantity": str(row["quantity"]),
                "price": str(row["price"]),
                "occurred_at": row["occurred_at"],
            }
            for row in fill_rows
        ],
        "attribution": "recovered" if recovered and not managed else "managed",
        "strategy_version": cycle.get("strategy_version"),
        "provenance": {
            "source": "outcome_ledger_worker",
            "fill_source_status": source_status,
            "journal_task_id": journal.get("task_id"),
            "bracket_task_id": (bracket_task or {}).get("task_id"),
            "venue_key": journal.get("venue_key"),
        },
    }
    exit_rows: list[dict[str, Any]] = []
    if fill_rows:
        for row in fill_rows:
            row["role"] = "entry" if row in entries else "exit"
        exit_rows = _build_exit_rows(
            account_id, cycle_order.cycle_id, position, exits_fills, payload,
        )
    counts = outcome_store.record_outcome(
        account_id=account_id, position=position,
        fills=fill_rows, exits=exit_rows, funding=[],
    )

    # Coverage: the venue's whole realized total in the window against what
    # the ledger actually attributed, with unattributed fills published.
    venue_realized = [
        CurrencyAmount(raw.get("realized_pnl_currency"), _decimal(raw.get("realized_pnl")))
        for raw in raw_fills if isinstance(raw, dict)
    ]
    stored = outcome_store.stored_cycle_fills(account_id, cycle_order.cycle_id) if ingest else []
    current_ids = {row["venue_trade_id"] for row in fill_rows}
    ledger_realized = [
        CurrencyAmount(row["realized_pnl_currency"], row["realized_pnl"])
        for row in stored if row["venue_trade_id"] in current_ids and row["symbol"] == symbol
    ]
    coverage = build_coverage(
        account_id=account_id,
        window_start=source_start.isoformat(),
        window_end=source_end.isoformat(),
        fills_found=len(raw_fills),
        attributed_fill_count=len(fill_rows),
        venue_realized=venue_realized,
        ledger_realized=ledger_realized,
        unattributed_manual_detected=unattributed,
        partial_entry_count=1 if partial_entry else 0,
        source_status=source_status,
        warnings=warnings,
        snapshot={"cycle_id": cycle_order.cycle_id, "managed": managed,
                  "recovered": recovered, "unattributed": unattributed},
    )
    if ingest:
        outcome_store.upsert_coverage(coverage)
    if read_error is not None:
        # Keep the failed observation visible and closure revoked, but let the
        # bounded poll retry policy own the failure lifecycle.
        raise RuntimeError(f"venue history read failed: {type(read_error).__name__}") from read_error
    return {
        "cycle_id": cycle_order.cycle_id,
        "result": "recorded" if fill_rows else "no_fills_in_window",
        "fills_attributed": len(fill_rows),
        "unattributed": unattributed,
        "counts": counts,
    }


def _first_ocurred(fills: list[dict]) -> datetime | None:
    times = [_parse_iso(row.get("occurred_at")) for row in fills]
    return min((moment for moment in times if moment is not None), default=None)


def _build_exit_rows(
    account_id: str,
    cycle_id: str,
    position: dict,
    exit_fills: list[dict],
    payload: dict,
) -> list[dict[str, Any]]:
    """One exit row per exit fill, classified against the recorded proposal."""
    proposal = payload.get("agent_proposal") if isinstance(payload.get("agent_proposal"), dict) else {}
    entry_price = _decimal(proposal.get("entry_price"))
    stop_price = _decimal(proposal.get("stop_loss"))
    target_price = _decimal(proposal.get("take_profit"))
    remaining = position.get("filled_quantity")
    rows: list[dict[str, Any]] = []
    for fill in sorted(exit_fills, key=lambda row: _parse_iso(row.get("occurred_at"))):
        leg = classify_exit_leg(
            exit_price=fill["price"],
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            exit_quantity=fill["quantity"],
            remaining_quantity=remaining or Decimal(0),
        )
        components = [{
            "currency": fill.get("realized_pnl_currency"),
            "amount": None if fill.get("realized_pnl") is None else str(fill["realized_pnl"]),
        }, {
            "currency": fill.get("fee_currency"),
            "amount": None if fill.get("fee") is None else str(-fill["fee"]),
        }]
        rows.append({
            "leg": leg,
            "venue_trade_id": fill["venue_trade_id"],
            "exit_reason": leg,
            "quantity": fill["quantity"],
            "price": fill["price"],
            "net_per_currency": components,
            "r_multiple": None,  # needs the position's initial-risk amount; unknown stays unknown
            "closed_at": fill.get("occurred_at"),
            "provenance": {"cycle_id": cycle_id, "account_id": account_id},
        })
        remaining = (remaining or Decimal(0)) - fill["quantity"]
    return rows


def _funding_rows(
    funding_events: Any,
    cycle_id: str,
    account_id: str,
    opened_at: datetime,
) -> list[dict[str, Any]]:
    """Normalize identified account-level income after the requested start.

    A valid event is still not evidence that a particular position received it.
    Rows without a venue semantics string are skipped.
    """
    if not isinstance(funding_events, list):
        return []
    rows: list[dict[str, Any]] = []
    for event in funding_events:
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp") if event.get("timestamp") is not None else event.get("time")
        if type(timestamp) is int:
            try:
                moment = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue
        else:
            moment = _parse_iso(timestamp)
        if moment is None or moment < opened_at:
            continue
        amount = _decimal(event.get("amount"))
        currency = event.get("currency")
        event_id = event.get("id")
        semantics = event.get("venue_semantics")
        if (amount is None or not isinstance(currency, str) or not currency
                or not isinstance(event_id, str) or not event_id):
            continue
        if not isinstance(semantics, str) or not semantics:
            logger.warning(
                "outcome ledger skipped a funding event without venue semantics "
                "(cycle %s): the amount's meaning is unknown",
                cycle_id,
            )
            continue
        rows.append({
            "id": event_id,
            "funding_at": moment.isoformat(),
            "amount": amount,
            "currency": currency,
            "venue_semantics": semantics,
        })
    return rows


def poll_once(fill_source: FillSource | None = None, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, Any]:
    """One bounded worker poll; independent of the autoloop and the scheduler.

    With ingest disabled this does nothing. On the first run after enabling,
    the cursor initializes to "now": history before the flag was turned on is
    not backfilled and not retro-attributed. The venue read is read-only
    (fills + funding income history), never a send.

    A cycle that raises is retried on the next poll (the cursor does not
    advance past it). Past ``MAX_CYCLE_ATTEMPTS`` the failure is published
    durably as a coverage row with status ``error`` — the cycle is visible
    forever, never silently dropped, and can no longer block the pipeline.
    """
    config = get_config()
    if not ingest_enabled(config):
        return {"status": "disabled"}
    if fill_source is None:
        fill_source = read_venue_window
    try:
        with poll_lock():
            return _poll_owned(fill_source, batch_size=batch_size)
    except PollLockBusyError:
        return {"status": "busy"}


def _poll_owned(fill_source: FillSource, *, batch_size: int) -> dict[str, Any]:
    """Run discovery and reconciliation under the active poll owner."""
    cursor = outcome_store.get_cursor()
    if cursor is None:
        outcome_store.set_cursor(datetime.now(UTC).isoformat())
        return {"status": "initialized", "note": "ingestion starts now; no backfill"}
    cursor_updated_at, _, cursor_cycle_id = cursor.partition("|")
    batch = outcome_store.terminal_cycles_after(
        cursor_updated_at or None, cursor_cycle_id or None, batch_size=batch_size,
    )
    outcome_store.schedule_existing_positions(batch_size=batch_size)
    # Discovery and reconciliation have separate budgets, so a steady stream
    # of new cycles cannot starve old open or recently closed positions.
    revisits = outcome_store.reconciliation_batch(batch_size=batch_size)
    outcomes: list[dict[str, Any]] = []
    processed_ids: set[str] = set()
    blocked_index: int | None = None
    for index, cycle in enumerate(batch):
        cycle_id = str(cycle.get("cycle_id"))
        payload = cycle.get("payload") if isinstance(cycle.get("payload"), dict) else {}
        if payload.get("execution_status") in ("executed", "uncertain"):
            outcome_store.schedule_reconciliation(cycle_id)
        processed_ids.add(cycle_id)
        failure: str | None = None
        try:
            outcomes.append(_process_cycle(cycle, fill_source=fill_source, ingest=True))
            outcome_store.clear_state(f"failed:{cycle_id}")
            continue
        except PollLockError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the poll
            logger.warning("outcome ledger failed to process %s: %s", cycle_id, exc)
            try:
                outcome_store.revoke_cycle_closure(cycle_id, "cycle_processing_error")
            except PollLockError:
                raise
            except Exception:
                logger.exception("outcome ledger could not revoke stale closure %s", cycle_id)
            failure = repr(exc)
            outcomes.append({"cycle_id": cycle_id,
                             "result": f"error:{type(exc).__name__}"})
        attempts = _bump_attempts(cycle_id)
        if attempts < MAX_CYCLE_ATTEMPTS:
            # Stop the batch here: the cursor will not advance past the
            # failing cycle, so the next poll retries it first.
            blocked_index = index
            break
        _record_cycle_error(cycle, failure or "unknown failure")
        outcome_store.clear_state(f"failed:{cycle_id}")
    # Cursor advance: full batch processed -> last row; blocked at index i ->
    # last row BEFORE i (the failing cycle is retried first next poll);
    # blocked at the first row -> cursor stays where it was.
    if batch and blocked_index != 0:
        last = batch[-1 if blocked_index is None else blocked_index - 1]
        outcome_store.set_cursor(f"{last['updated_at']}|{last['cycle_id']}")
    reconciled = []
    for cycle in revisits:
        cycle_id = str(cycle["cycle_id"])
        if cycle_id in processed_ids:
            continue
        try:
            reconciled.append(_process_cycle(cycle, fill_source=fill_source, ingest=True))
            outcome_store.clear_state(f"failed:{cycle_id}")
        except PollLockError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed revisit must not starve others
            logger.warning("outcome ledger failed to reconcile %s: %s", cycle_id, exc)
            try:
                outcome_store.revoke_cycle_closure(cycle_id, "reconciliation_error")
            except PollLockError:
                raise
            except Exception:
                logger.exception("outcome ledger could not revoke stale closure %s", cycle_id)
            reconciled.append({"cycle_id": cycle_id, "result": f"error:{type(exc).__name__}"})
            attempts = _bump_attempts(cycle_id)
            if attempts >= MAX_CYCLE_ATTEMPTS:
                _record_cycle_error(cycle, repr(exc))
                outcome_store.set_state("permanent", key=f"reconcile:{cycle_id}")
                outcome_store.set_state("permanent", key=f"failed:{cycle_id}")
                continue
        outcome_store.set_state(cycle_id, key=f"reconcile:{cycle_id}")
    return {"status": "polled", "processed": len(batch), "outcomes": outcomes,
            "reconciled": reconciled}


def _bump_attempts(cycle_id: str) -> int:
    key = f"failed:{cycle_id}"
    raw = outcome_store.get_state(key)
    try:
        attempts = int(raw) + 1
    except (TypeError, ValueError):
        attempts = 1
    outcome_store.set_state(str(attempts), key=key)
    return attempts


def _record_cycle_error(cycle: dict[str, Any], error: repr) -> None:
    """Publish a permanently failing cycle as an error coverage row."""
    cycle_id = str(cycle.get("cycle_id"))
    coverage = build_coverage(
        account_id=f"cycle-error:{cycle_id}",
        window_start=str(cycle.get("created_at") or ""),
        window_end=datetime.now(UTC).isoformat(),
        fills_found=0,
        attributed_fill_count=0,
        venue_realized=[],
        ledger_realized=[],
        unattributed_manual_detected=0,
        partial_entry_count=0,
        source_status="error",
        warnings=[f"cycle processing failed after {MAX_CYCLE_ATTEMPTS} attempts: {error}"],
        snapshot={"cycle_id": cycle_id, "status": str(cycle.get("status") or "")},
    )
    outcome_store.upsert_coverage(coverage)


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            poll_once()
        except Exception:
            logger.exception("outcome ledger poll failed")
        if _stop_event.wait(interval_seconds()):
            break


def start() -> None:
    """Start the worker thread (composition root wiring); no-op when disabled."""
    global _worker_thread
    if not ingest_enabled():
        logger.info("outcome ledger ingest is disabled; worker not started")
        return
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="server-outcome-ledger", daemon=True,
    )
    _worker_thread.start()


def stop() -> None:
    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=interval_seconds() + 5)
        if not _worker_thread.is_alive():
            _worker_thread = None
