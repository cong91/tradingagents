"""Fill-to-position attribution for the outcome ledger.

Evidence-only matching. A venue fill belongs to the bot only when a durable
chain holds: the cycle payload's ``order_id`` matches the fill's venue order id
AND a valid ``managed_live_entry`` provenance marker exists on the entry journal
(futures_entry_journal). Fills recovered through the submission-unknown path are
the same intent when they share the journal's ``client_order_id``.

No time/price/symbol heuristics ever attribute a fill: a fill that only happens
to share symbol, size and timing with a cycle stays unattributed, and a manual
trade is never folded into bot PnL. There is no retro-assignment: the decision
uses only the journal rows recorded at order time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tradingagents.execution.futures_entry_journal import (
    record_entry_provenance,
    valid_managed_live_provenance,
)

Attribution = Literal["managed", "recovered", "unattributed"]


@dataclass(frozen=True)
class AttributionDecision:
    """One fill's verdict plus the keys the position row needs."""

    attribution: Attribution
    client_order_id: str | None
    cycle_id: str | None
    reason: str


@dataclass(frozen=True)
class CycleOrder:
    """The attribution evidence one terminal cycle carries."""

    cycle_id: str
    order_id: str | None
    symbol: str
    journal_task: dict | None

    @property
    def client_order_id(self) -> str | None:
        marker = self.marker()
        if marker is None:
            return None
        value = marker.get("client_order_id")
        return value if isinstance(value, str) and value else None

    def marker(self) -> dict | None:
        """The provenance marker recorded on the entry journal, or None."""
        if not isinstance(self.journal_task, dict):
            return None
        return record_entry_provenance(self.journal_task)


def attribute_fill(
    fill_order_id: str | None,
    fill_symbol: str,
    cycle: CycleOrder,
) -> AttributionDecision:
    """Decide one venue fill's attribution, or refuse to attribute it.

    Requires the fill's venue order id to equal the cycle's recorded
    ``order_id`` and the journal marker to be a valid ``managed_live_entry``
    marker for the same symbol. Everything else stays unattributed — including
    a fill with no order id, a cycle without journal evidence, and a marker
    whose copies disagree (``record_entry_provenance`` returns None then).
    """
    marker = cycle.marker()
    if cycle.order_id is None or not isinstance(fill_order_id, str) or not fill_order_id:
        return AttributionDecision(
            "unattributed", None, None, "missing_order_id_evidence"
        )
    if not isinstance(marker, dict) or not valid_managed_live_provenance(marker):
        return AttributionDecision(
            "unattributed", None, None, "managed_live_entry_marker_absent"
        )
    if fill_order_id != cycle.order_id:
        return AttributionDecision(
            "unattributed", None, None, "order_id_mismatch"
        )
    if marker.get("symbol") != fill_symbol:
        return AttributionDecision(
            "unattributed", None, None, "symbol_mismatch"
        )
    return AttributionDecision(
        "managed",
        str(marker.get("client_order_id")),
        cycle.cycle_id,
        "cycle_order_id_and_marker_match",
    )


def recovered_client_order_ids(tasks: list[dict]) -> dict[str, str]:
    """venue order id -> client_order_id for recovery-found orders.

    Recovery tasks record the venue order ids they eventually found in
    ``placed_ids``; any of them is the same entry intent as the task's
    ``entry_order_id`` (the client order id), so a fill matched here merges
    into one position instead of opening a second one. Markers must be valid;
    an invalid marker yields no recovered identity.
    """
    recovered: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        marker = record_entry_provenance(task)
        if not isinstance(marker, dict) or not valid_managed_live_provenance(marker):
            continue
        client_order_id = marker.get("client_order_id")
        if not isinstance(client_order_id, str) or not client_order_id:
            continue
        placed = task.get("placed_ids")
        if not isinstance(placed, list):
            continue
        for venue_order_id in placed:
            if isinstance(venue_order_id, str) and venue_order_id:
                recovered.setdefault(venue_order_id, client_order_id)
    return recovered


def attribute_recovered_fill(
    fill_order_id: str | None,
    fill_symbol: str,
    recovered: dict[str, str],
    cycle_id: str,
) -> AttributionDecision:
    """Attribute a fill through the recovery path, merging by client order id.

    A fill whose venue order id appears in a recovery task's ``placed_ids``
    belongs to the same entry intent as the original attempt. It never creates
    a second position: the caller keys the position on the returned
    ``client_order_id``.
    """
    if not isinstance(fill_order_id, str) or not fill_order_id:
        return AttributionDecision(
            "unattributed", None, None, "missing_order_id_evidence"
        )
    client_order_id = recovered.get(fill_order_id)
    if client_order_id is None:
        return AttributionDecision(
            "unattributed", None, None, "no_recovery_evidence"
        )
    return AttributionDecision(
        "recovered",
        client_order_id,
        cycle_id,
        "recovered_by_client_order_id",
    )


def bracket_exit_order_ids(
    bracket_task: dict | None,
    *,
    entry_order_id: str | None,
    identity_hash: str | None,
    symbol: str,
) -> dict[str, str]:
    """venue exit order id -> reason label, from the position's bracket task.

    The bracket task is the durable exit evidence: it is created keyed by the
    ENTRY venue order id (``bracket:{symbol}:{entry_order_id}``), and its
    ``placed_ids`` (and per-leg ``order_id`` values) hold the stop, target and
    scale-out tranche order ids as they are placed (brackets._save /
    scale_out_apply). A fill whose venue order id appears here is the bot's
    own exit for that position.

    Evidence linkage is required, never assumed: the task must carry the same
    symbol AND the same account identity hash as the entry marker. A task with
    a missing or different identity contributes nothing — the fills stay
    unattributed rather than being claimed.
    """
    evidence: dict[str, str] = {}
    if not isinstance(bracket_task, dict):
        return evidence
    if symbol and bracket_task.get("symbol") != symbol:
        return evidence
    if identity_hash and bracket_task.get("account_identity_hash") != identity_hash:
        return evidence
    if entry_order_id is not None and bracket_task.get("entry_order_id") != entry_order_id:
        return evidence
    legs = bracket_task.get("legs")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            order_id = leg.get("order_id")
            if isinstance(order_id, str) and order_id:
                evidence.setdefault(order_id, f"bracket_leg_{leg.get('name')}")
    placed = bracket_task.get("placed_ids")
    if isinstance(placed, list):
        for order_id in placed:
            if isinstance(order_id, str) and order_id:
                evidence.setdefault(order_id, "bracket_placed_id")
    return evidence


def attribute_exit_fill(
    fill_order_id: str | None,
    fill_symbol: str,
    exit_evidence: dict[str, str],
    client_order_id: str,
    cycle_id: str,
) -> AttributionDecision:
    """Attribute one exit fill through the position's bracket evidence.

    The venue reports ``realized_pnl`` on the fill that REDUCES the position,
    so exit fills carry the net result; without this path the bot's own exits
    would be miscounted as manual trades. The verdict is ``managed`` — same
    intent, same client_order_id — never a second position.
    """
    if not isinstance(fill_order_id, str) or not fill_order_id:
        return AttributionDecision(
            "unattributed", None, None, "missing_order_id_evidence"
        )
    reason = exit_evidence.get(fill_order_id)
    if reason is None:
        return AttributionDecision(
            "unattributed", None, None, "no_bracket_exit_evidence"
        )
    return AttributionDecision(
        "managed",
        client_order_id,
        cycle_id,
        reason,
    )
