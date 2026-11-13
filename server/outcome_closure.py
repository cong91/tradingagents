"""Evidence checks for outcome-position closure proofs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

CLOSURE_PROOF_VERSION = 2
_TERMINAL_PARTIAL_ENTRY_STATUSES = {
    "canceled", "cancelled", "expired", "expired_in_match",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ArithmeticError):
        return None
    return number if number.is_finite() else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def fill_history_complete(
    *,
    source_status: str,
    source_certified_complete: bool,
    requested_start: Any,
    requested_end: Any,
    observed_start: Any,
    observed_end: Any,
    source_warnings: list[str],
    malformed_fill_count: int = 0,
    duplicate_fill_count: int = 0,
    unattributed_fill_count: int = 0,
    contradictory_fill_count: int = 0,
) -> bool:
    """Verify fill-read completeness without considering funding uncertainty."""
    requested_from = _timestamp(requested_start)
    requested_until = _timestamp(requested_end)
    observed_from = _timestamp(observed_start)
    observed_until = _timestamp(observed_end)
    fill_warnings = [
        warning for warning in source_warnings
        if not warning.lower().startswith(("funding_", "funding:"))
    ]
    return (
        source_status == "ok"
        and source_certified_complete is True
        and requested_from is not None
        and requested_until is not None
        and requested_from < requested_until
        and observed_from is not None
        and observed_until is not None
        and observed_from <= requested_from
        and observed_until >= requested_until
        and not fill_warnings
        and malformed_fill_count == 0
        and duplicate_fill_count == 0
        and unattributed_fill_count == 0
        and contradictory_fill_count == 0
    )


def _terminal_partial_entry(
    evidence: Any,
    *,
    planned_quantity: Decimal,
    expected_identity: dict[str, Any],
) -> tuple[Decimal, dict[str, Any]] | None:
    if not isinstance(evidence, dict):
        return None
    status = evidence.get("status")
    if not isinstance(status, str) or status.lower() not in _TERMINAL_PARTIAL_ENTRY_STATUSES:
        return None
    for key in ("order_id", "client_order_id", "symbol", "venue_key", "account_id"):
        expected = expected_identity.get(key)
        if not isinstance(expected, str) or not expected or evidence.get(key) != expected:
            return None
    identity = expected_identity.get("account_identity")
    if not isinstance(identity, dict) or evidence.get("account_identity") != identity:
        return None
    original_quantity = _decimal(evidence.get("original_quantity"))
    filled_quantity = _decimal(evidence.get("filled_quantity"))
    if (original_quantity != planned_quantity or filled_quantity is None
            or filled_quantity <= 0 or filled_quantity >= planned_quantity):
        return None
    facts = {
        "status": status.lower(),
        "order_id": evidence["order_id"],
        "client_order_id": evidence["client_order_id"],
        "symbol": evidence["symbol"],
        "venue_key": evidence["venue_key"],
        "account_id": evidence["account_id"],
        "account_identity": identity,
        "original_quantity": str(original_quantity),
        "filled_quantity": str(filled_quantity),
    }
    return filled_quantity, facts


def _fill_signatures(facts: list[dict[str, Any]]) -> list[tuple[Any, ...]] | None:
    signatures = []
    for fact in facts:
        if not isinstance(fact, dict):
            return None
        quantity = _decimal(fact.get("quantity"))
        price = _decimal(fact.get("price"))
        occurred_at = _timestamp(fact.get("occurred_at"))
        if (not isinstance(fact.get("trade_id"), str) or not fact["trade_id"]
                or quantity is None or quantity <= 0 or price is None or price <= 0
                or occurred_at is None):
            return None
        signatures.append((
            fact["trade_id"], fact.get("order_id"), fact.get("symbol"), fact.get("side"),
            fact.get("role"), quantity, price, occurred_at,
        ))
    return sorted(signatures, key=lambda row: row[0])


def verified_closure(
    *,
    fill_history: dict[str, Any],
    planned_quantity: Any,
    entry_quantity: Any,
    exit_quantity: Any,
    opened_at: Any,
    closed_at: Any,
    minimum_balance: Any,
    entry_order_final: Any,
    expected_identity: dict[str, Any],
    fill_facts: list[dict[str, Any]],
    stored_fill_facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return proof facts only when complete history and balanced fills close."""
    if not isinstance(fill_history, dict):
        return None
    if not fill_history_complete(
        source_status=fill_history.get("source_status", ""),
        source_certified_complete=fill_history.get("source_certified_complete") is True,
        requested_start=fill_history.get("requested_start"),
        requested_end=fill_history.get("requested_end"),
        observed_start=fill_history.get("observed_start"),
        observed_end=fill_history.get("observed_end"),
        source_warnings=fill_history.get("source_warnings", []),
        malformed_fill_count=fill_history.get("malformed_fill_count", 0),
        duplicate_fill_count=fill_history.get("duplicate_fill_count", 0),
        unattributed_fill_count=fill_history.get("unattributed_fill_count", 0),
        contradictory_fill_count=fill_history.get("contradictory_fill_count", 0),
    ):
        return None

    planned = _decimal(planned_quantity)
    entries = _decimal(entry_quantity)
    exits = _decimal(exit_quantity)
    balance = _decimal(minimum_balance)
    opened = _timestamp(opened_at)
    closed = _timestamp(closed_at)
    if (planned is None or planned <= 0 or entries is None or entries <= 0
            or exits != entries or balance is None or balance < 0
            or opened is None or closed is None):
        return None

    basis = "planned_quantity"
    entry_facts = None
    if entries != planned:
        partial = _terminal_partial_entry(
            entry_order_final,
            planned_quantity=planned,
            expected_identity=expected_identity,
        )
        if partial is None or partial[0] != entries:
            return None
        basis = "terminal_partial_entry_order"
        entry_facts = partial[1]

    source_signatures = _fill_signatures(fill_facts)
    stored_signatures = _fill_signatures(stored_fill_facts)
    if source_signatures is None or source_signatures != stored_signatures:
        return None

    return {
        "version": CLOSURE_PROOF_VERSION,
        "fill_history": {
            "source_status": fill_history["source_status"],
            "requested_start": _timestamp(fill_history["requested_start"]).isoformat(),
            "requested_end": _timestamp(fill_history["requested_end"]).isoformat(),
            "observed_start": _timestamp(fill_history["observed_start"]).isoformat(),
            "observed_end": _timestamp(fill_history["observed_end"]).isoformat(),
        },
        "entry_quantity_basis": basis,
        "planned_quantity": str(planned),
        "entry_quantity": str(entries),
        "exit_quantity": str(exits),
        "opened_at": opened.isoformat(),
        "closed_at": closed.isoformat(),
        "minimum_balance": str(balance),
        "fill_facts": list(fill_facts),
        "entry_order_final": entry_facts,
    }
