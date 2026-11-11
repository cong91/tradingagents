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
from tradingagents.dataflows.config import get_config
from tradingagents.execution.futures_entry_journal import record_entry_provenance
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
    VENUE_SEMANTICS_BINANCE,
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
    if payload.get("execution_status") != "executed" or not order_id or not symbol:
        return {"cycle_id": cycle.get("cycle_id"), "result": "skipped_not_executed"}

    tasks = outcome_store.find_journal_tasks(symbol, venue_order_id=str(order_id))
    journal = tasks[0] if tasks else None
    cycle_order = CycleOrder(
        cycle_id=str(cycle.get("cycle_id")),
        order_id=str(order_id),
        symbol=symbol,
        journal_task=journal,
    )
    marker = record_entry_provenance(journal) if journal is not None else None
    account_id = account_identity_from_marker(marker) if isinstance(marker, dict) else None
    if cycle_order.client_order_id is None or account_id is None:
        # No valid marker: the fills cannot be proven managed, so nothing is
        # written and the cycle is reported for coverage, never attributed.
        return {"cycle_id": cycle_order.cycle_id, "result": "unattributed_no_marker"}

    # Exit evidence: the position's bracket task (keyed by the ENTRY venue
    # order id) holds the stop/target/scale-out order ids the runtime placed,
    # bound to the same account identity — the chain that makes the bot's own
    # exit fills attributable, so realized PnL is never lost to "manual".
    marker_hash = (marker or {}).get("account_identity", {}).get("hash")
    bracket_task = outcome_store.find_bracket_task(symbol, str(order_id))
    exit_evidence = bracket_exit_order_ids(
        bracket_task,
        entry_order_id=str(order_id),
        identity_hash=marker_hash,
        symbol=symbol,
    )

    opened_at = _parse_iso(cycle.get("created_at"))
    if opened_at is None:
        opened_at = datetime.now(UTC)
    window = fill_source(account_id, symbol, _ms(opened_at), _ms(datetime.now(UTC)))
    source_status = str(window.get("status") or "unavailable")
    raw_fills = window.get("fills") if isinstance(window.get("fills"), list) else []
    warnings = [str(item) for item in window.get("warnings") or []]

    managed = recovered = unattributed = 0
    fill_rows: list[dict[str, Any]] = []
    side = "long" if (journal.get("close_side") == "sell") else "short"
    recovery_map = recovered_client_order_ids([journal])
    for raw in raw_fills:
        if not isinstance(raw, dict) or raw.get("symbol") != symbol:
            continue
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
        managed += verdict.attribution == "managed"
        recovered += verdict.attribution == "recovered"
        fill_rows.append({
            "venue_trade_id": str(raw["trade_id"]),
            "venue_order_id": raw.get("order_id"),
            "symbol": symbol,
            "side": raw.get("side"),
            "role": "entry",  # partitioned below against the position's side
            "quantity": _decimal(raw.get("contracts")),
            "price": _decimal(raw.get("price")),
            "fee": _decimal(raw.get("fee")),
            "fee_currency": raw.get("fee_currency"),
            "realized_pnl": _decimal(raw.get("realized_pnl")),
            "realized_pnl_currency": raw.get("realized_pnl_currency"),
            "realized_pnl_semantics": REALIZED_PNL_EXCLUDES_FEE,
            "occurred_at": raw.get("timestamp"),
        })
    complete_fills = [row for row in fill_rows if None not in (
        row["quantity"], row["price"], row["occurred_at"],
    ) and row["side"] in ("buy", "sell")]
    planned_quantity = _decimal(journal.get("quantity"))
    entries, exits_fills = partition_position_fills(side, complete_fills)
    filled_quantity = sum((row["quantity"] for row in entries), Decimal(0))
    partial_entry = bool(
        complete_fills and planned_quantity is not None
        and filled_quantity < planned_quantity
    )
    if ingest and complete_fills:
        for row in complete_fills:
            row["role"] = "entry" if row in entries else "exit"
        opened_at = _first_ocurred(entries) or opened_at
        # closed_at is the last EXIT fill; with no exit fill yet the position
        # is still open — the ledger must not declare it closed by omission.
        closed_at = _last_occurred(exits_fills)
        position = {
            "cycle_id": cycle_order.cycle_id,
            "client_order_id": journal.get("entry_order_id"),
            "venue_order_id": str(order_id),
            "symbol": symbol,
            "market_type": "perpetual",
            "side": side,
            "planned_quantity": planned_quantity,
            "filled_quantity": filled_quantity,
            "opened_at": opened_at.isoformat(),
            "closed_at": closed_at.isoformat() if closed_at else None,
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
        exit_rows = _build_exit_rows(
            account_id, cycle_order.cycle_id, position, exits_fills, payload,
        )
        funding_rows = _funding_rows(
            window.get("funding"), cycle_order.cycle_id, account_id, opened_at,
        )
        counts = outcome_store.record_outcome(
            account_id=account_id, position=position,
            fills=complete_fills, exits=exit_rows, funding=funding_rows,
        )
    else:
        counts = {"positions_new": 0, "fills_new": 0, "fills_duplicate": 0,
                  "exits_new": 0, "funding_new": 0}

    # Coverage: the venue's whole realized total in the window against what
    # the ledger actually attributed, with unattributed fills published.
    venue_realized = [
        CurrencyAmount(raw.get("realized_pnl_currency"), _decimal(raw.get("realized_pnl")))
        for raw in raw_fills if isinstance(raw, dict)
    ]
    ledger_realized = [
        CurrencyAmount(row["realized_pnl_currency"], row["realized_pnl"])
        for row in complete_fills
    ]
    coverage = build_coverage(
        account_id=account_id,
        window_start=opened_at.isoformat(),
        window_end=datetime.now(UTC).isoformat(),
        fills_found=len(raw_fills),
        attributed_fill_count=len(complete_fills),
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
    return {
        "cycle_id": cycle_order.cycle_id,
        "result": "recorded" if complete_fills else "no_fills_in_window",
        "fills_attributed": len(complete_fills),
        "unattributed": unattributed,
        "counts": counts,
    }


def _first_ocurred(fills: list[dict]) -> Any:
    times = sorted(str(row["occurred_at"]) for row in fills if row.get("occurred_at"))
    return _parse_iso(times[0]) if times else None


def _last_occurred(fills: list[dict]) -> Any:
    times = sorted(str(row["occurred_at"]) for row in fills if row.get("occurred_at"))
    return _parse_iso(times[-1]) if times else None


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
    for fill in sorted(exit_fills, key=lambda row: str(row.get("occurred_at"))):
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
    """Venue funding income rows inside the position window, sign verbatim.

    A row without an explicit ``venue_semantics`` is SKIPPED, never defaulted:
    guessing Binance's sign convention for a future venue would fabricate the
    meaning of the amounts. The Binance reader sets the semantics explicitly.
    """
    if not isinstance(funding_events, list):
        return []
    rows: list[dict[str, Any]] = []
    for event in funding_events:
        if not isinstance(event, dict):
            continue
        moment = _parse_iso(event.get("timestamp") or event.get("time"))
        if moment is None or moment < opened_at:
            continue
        amount = _decimal(event.get("amount"))
        currency = event.get("currency")
        semantics = event.get("venue_semantics")
        if amount is None or not isinstance(currency, str) or not currency:
            continue
        if not isinstance(semantics, str) or not semantics:
            logger.warning(
                "outcome ledger skipped a funding event without venue semantics "
                "(cycle %s): the amount's meaning is unknown",
                cycle_id,
            )
            continue
        rows.append({
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
        fill_source = _venue_fill_source
    cursor = outcome_store.get_cursor()
    if cursor is None:
        outcome_store.set_cursor(datetime.now(UTC).isoformat())
        return {"status": "initialized", "note": "ingestion starts now; no backfill"}
    cursor_updated_at, _, cursor_cycle_id = cursor.partition("|")
    batch = outcome_store.terminal_cycles_after(
        cursor_updated_at or None, cursor_cycle_id or None, batch_size=batch_size,
    )
    outcomes: list[dict[str, Any]] = []
    blocked_index: int | None = None
    for index, cycle in enumerate(batch):
        cycle_id = str(cycle.get("cycle_id"))
        failure: str | None = None
        try:
            outcomes.append(_process_cycle(cycle, fill_source=fill_source, ingest=True))
            outcome_store.clear_state(f"failed:{cycle_id}")
            continue
        except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the poll
            logger.warning("outcome ledger failed to process %s: %s", cycle_id, exc)
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
    return {"status": "polled", "processed": len(batch), "outcomes": outcomes}


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


def _venue_fill_source(account_id: str, symbol: str, since_ms: int, until_ms: int) -> dict:
    """Read-only venue history for the window, via the executor's own readers.

    Builds the derivatives executor the same way bracket_monitor does. Fills
    come from the bounded trade-history reader; funding income from the
    venue income reader. Both are reads; nothing is sent.
    """
    del account_id  # the venue read is scoped by the configured account, not by the label
    from server.futures_trade_history import read_futures_trade_history
    from tradingagents.execution.derivatives import DerivativesExecutor

    config = get_config()
    executor = DerivativesExecutor(config=config)
    bridge = executor._bridge  # the bridge is the executor's venue reader (not a public API)
    ccxt_symbol = symbol  # cycle symbols are already unified ccxt symbols
    history = read_futures_trade_history(bridge, [ccxt_symbol], since_ms, until_ms)
    funding: list[dict] | None = None
    if history["status"] in ("ok", "partial"):
        from tradingagents.execution.futures_funding import fetch_funding_income

        client = bridge.client()
        rows = fetch_funding_income(client, ccxt_symbol, since_ms)
        if rows:
            funding = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                currency = (row.get("info") or {}).get("incomeAsset")
                # A row without a currency label is not guessed into USDT:
                # an unknown currency cannot be bucketed honestly.
                if not isinstance(currency, str) or not currency:
                    continue
                amount = (row.get("amount")
                          if isinstance(row.get("amount"), (int, float))
                          else (row.get("info") or {}).get("income"))
                if amount is None:
                    continue
                funding.append({
                    "timestamp": row.get("timestamp"),
                    "amount": amount,
                    "currency": currency,
                    "venue_semantics": VENUE_SEMANTICS_BINANCE,
                })
    return {
        "status": history["status"],
        "fills": list(history["fills"]),
        "warnings": list(history["warnings"]),
        "funding": funding,
    }


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
