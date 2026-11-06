"""Regression coverage for terminal partial futures entry recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import job_store
from tests import test_futures_entry_recovery as recovery_tests
from tests.execution_derivatives_support import VERIFIED_IDENTITY
from tradingagents.execution import futures_entry_recovery
from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.execution.futures_entry_recovery import reconcile_entry
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit

SYMBOL = recovery_tests.SYMBOL
ORDER_ID = recovery_tests.ORDER_ID
CLIENT_ID = recovery_tests.CLIENT_ID
INTENT_ID = recovery_tests.INTENT_ID
BRACKET_ID = recovery_tests.BRACKET_ID
VENUE = recovery_tests.VENUE
ORIGINAL_QUANTITY = recovery_tests.QUANTITY
FILLED_QUANTITY = 0.5
ENTRY_PRICE = recovery_tests.ENTRY_PRICE
STOP = recovery_tests.STOP
TARGET = recovery_tests.TARGET


@pytest.fixture(autouse=True)
def _partial_recovery_db(postgres_schema):
    return postgres_schema


def _terminal_order(raw_status: str, *, filled: float = FILLED_QUANTITY):
    unified_status = {
        "CANCELED": "canceled",
        "EXPIRED": "expired",
        "EXPIRED_IN_MATCH": "expired",
        "FILLED": "closed",
    }[raw_status]
    item = recovery_tests._entry_order(
        status=raw_status, quantity=ORIGINAL_QUANTITY, filled=filled,
    )
    item["status"] = unified_status
    return item


def _partial_position(quantity: float = FILLED_QUANTITY):
    return FuturesPosition(
        symbol=SYMBOL, side="long", contracts=quantity, quantity=quantity,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )


def _set_intent_terms():
    intent = job_store.load_bracket_recovery(INTENT_ID)
    intent_leg = intent["legs"][0]
    intent_leg.update(original_stop_loss=STOP, take_profit=TARGET, venue_key=VENUE, profile="demo")
    job_store.update_bracket_recovery(
        INTENT_ID, status="submission_uncertain", placed_ids=[ORDER_ID], attempts=0,
        last_error="entry residual requires reconciliation", now="2026-11-05T10:00:00+00:00",
        legs=[intent_leg],
    )


def _seed_open_bracket(exchange, *, quantity: float = FILLED_QUANTITY):
    recovery_tests._seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        quantity=quantity,
        legs=[
            recovery_tests._leg("stop-loss", "stop-algo", "placed", STOP),
            recovery_tests._leg("take-profit", "target-algo", "placed", TARGET),
        ],
    )
    exchange.orders["stop-algo"] = recovery_tests._conditional(
        "stop-algo", "stop-loss", quantity=quantity,
    )
    exchange.orders["target-algo"] = recovery_tests._conditional(
        "target-algo", "take-profit", quantity=quantity,
    )


def _seed_flat_bracket(exchange, *, quantity: float = FILLED_QUANTITY):
    recovery_tests._seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        quantity=quantity,
        status="reconciled_stop_loss",
        legs=[
            recovery_tests._leg("stop-loss", "stop-algo", "filled", STOP),
            recovery_tests._leg("take-profit", "target-algo", "cancelled", TARGET),
        ],
    )
    exchange.orders["stop-algo"] = recovery_tests._conditional(
        "stop-algo", "stop-loss", "FINISHED", quantity=quantity,
        child_id="stop-child", child_quantity=quantity,
    )
    exchange.orders["stop-child"] = recovery_tests._child_order(
        quantity=quantity, filled=quantity,
    )
    exchange.orders["target-algo"] = recovery_tests._conditional(
        "target-algo", "take-profit", "CANCELED", quantity=quantity,
    )


def _audit_rows(bridge, *, quantity: float = FILLED_QUANTITY):
    authoritative = recovery_tests._audit_event(action="buy", client_id=None)
    authoritative.update(qty=quantity, price_est=ENTRY_PRICE)
    diagnostic = dict(authoritative, action="entry_partial_or_unknown")
    unrelated = dict(authoritative, order_id="unrelated-order", action="sell")
    return authoritative, diagnostic, unrelated


def _write_audit(path: Path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _reconcile(bridge):
    with operation_lock(SYMBOL):
        return reconcile_entry(bridge, INTENT_ID)


@pytest.mark.parametrize(("raw_status", "unified_status"), [
    ("CANCELED", "canceled"),
    ("EXPIRED", "expired"),
    ("EXPIRED_IN_MATCH", "expired"),
])
def test_terminal_partial_entry_is_accepted_only_with_matching_status_pair(
    tmp_path, monkeypatch, raw_status, unified_status,
):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    item = _terminal_order(raw_status)
    item["status"] = unified_status

    with operation_lock(SYMBOL):
        evidence = futures_entry_recovery._validate_entry_order(
            exchange, job_store.load_bracket_recovery(INTENT_ID), item, ORDER_ID, CLIENT_ID,
        )

    assert evidence is not None
    assert evidence.quantity == ORIGINAL_QUANTITY
    assert evidence.filled == FILLED_QUANTITY


@pytest.mark.parametrize(("raw_status", "unified_status"), [
    ("CANCELED", "expired"),
    ("EXPIRED", "canceled"),
    ("EXPIRED_IN_MATCH", "expired_in_match"),
    ("PARTIALLY_FILLED", "open"),
    ("NEW", "open"),
    ("FILLED", "closed"),
])
def test_partial_entry_validation_rejects_conflicts_and_nonterminal_orders(
    tmp_path, monkeypatch, raw_status, unified_status,
):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    item = recovery_tests._entry_order(
        status=raw_status, quantity=ORIGINAL_QUANTITY, filled=FILLED_QUANTITY,
    )
    item["status"] = unified_status

    with operation_lock(SYMBOL):
        evidence = futures_entry_recovery._validate_entry_order(
            exchange, job_store.load_bracket_recovery(INTENT_ID), item, ORDER_ID, CLIENT_ID,
        )

    assert evidence is None


@pytest.mark.parametrize("missing", ["status", "origQty", "executedQty", "cumQuote", "time", "updateTime"])
def test_terminal_partial_entry_requires_complete_raw_fields(tmp_path, monkeypatch, missing):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    item = _terminal_order("CANCELED")
    item["info"].pop(missing)

    with operation_lock(SYMBOL):
        evidence = futures_entry_recovery._validate_entry_order(
            exchange, job_store.load_bracket_recovery(INTENT_ID), item, ORDER_ID, CLIENT_ID,
        )

    assert evidence is None


@pytest.mark.parametrize("changes", [
    {"quantity": 3.0, "amount": 3.0},
    {"filled": 0.0},
    {"filled": 2.1},
    {"order_id": "other-order"},
    {"client_id": "other-client"},
    {"side": "SELL"},
])
def test_terminal_partial_entry_rejects_invalid_size_or_identity(tmp_path, monkeypatch, changes):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    item = _terminal_order("CANCELED")
    raw = item["info"]
    if "quantity" in changes:
        raw["origQty"] = str(changes["quantity"])
        item["amount"] = changes["amount"]
    if "filled" in changes:
        raw["executedQty"] = str(changes["filled"])
        raw["cumQuote"] = str(changes["filled"] * ENTRY_PRICE)
        item["filled"] = changes["filled"]
    if "order_id" in changes:
        item["id"] = changes["order_id"]
        raw["orderId"] = changes["order_id"]
    if "client_id" in changes:
        item["clientOrderId"] = changes["client_id"]
        raw["clientOrderId"] = changes["client_id"]
    if "side" in changes:
        item["side"] = changes["side"].lower()
        raw["side"] = changes["side"]

    with operation_lock(SYMBOL):
        evidence = futures_entry_recovery._validate_entry_order(
            exchange, job_store.load_bracket_recovery(INTENT_ID), item, ORDER_ID, CLIENT_ID,
        )

    assert evidence is None


@pytest.mark.parametrize("raw_status", ["CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"])
def test_terminal_partial_entry_reconciles_open_position_and_diagnostic_audit(
    tmp_path, monkeypatch, raw_status,
):
    exchange, bridge = recovery_tests._setup(
        tmp_path, monkeypatch, position=_partial_position(),
    )
    exchange.orders[ORDER_ID] = _terminal_order(raw_status)
    exchange.trades[0]["qty"] = str(FILLED_QUANTITY)
    _set_intent_terms()
    _seed_open_bracket(exchange)
    events = _audit_rows(bridge)
    _write_audit(Path(bridge.config["exec_log_path"]), events)

    assert _reconcile(bridge) == "completed"
    state = job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}")
    assert state["snapshot"]["quantity"] == FILLED_QUANTITY
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "completed"
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]


def test_terminal_partial_entry_reconciles_flat_position_after_verified_exit(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _terminal_order("EXPIRED_IN_MATCH")
    exchange.trades[0]["qty"] = str(FILLED_QUANTITY)
    _set_intent_terms()
    _seed_flat_bracket(exchange)

    assert _reconcile(bridge) == "completed"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "completed"
    assert job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}") is None
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]


def test_terminal_partial_entry_requires_bracket_quantity_to_equal_actual_fill(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(
        tmp_path, monkeypatch, position=_partial_position(),
    )
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    exchange.trades[0]["qty"] = str(FILLED_QUANTITY)
    _set_intent_terms()
    _seed_open_bracket(exchange, quantity=FILLED_QUANTITY - 0.1)
    _write_audit(
        Path(bridge.config["exec_log_path"]), [_audit_rows(bridge)[0]],
    )

    assert _reconcile(bridge) == "bracket_evidence_incomplete"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_open_partial_provenance_rejects_two_authoritative_entry_events(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(
        tmp_path, monkeypatch, position=_partial_position(),
    )
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    exchange.trades[0]["qty"] = str(FILLED_QUANTITY)
    _set_intent_terms()
    _seed_open_bracket(exchange)
    authoritative, _, _ = _audit_rows(bridge)
    conflicting = dict(authoritative, stop_loss=STOP - 0.5)
    _write_audit(Path(bridge.config["exec_log_path"]), [authoritative, conflicting])

    assert _reconcile(bridge) == "open_position_provenance_ambiguous"
    assert job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}") is None


def _cleanup_allowed(bridge, bracket):
    from tradingagents.execution import futures_entry_recovery

    with operation_lock(SYMBOL):
        return futures_entry_recovery.entry_allows_exit_cleanup(bridge, bracket)


def _seed_cleanup_bracket(*, quantity=FILLED_QUANTITY, identity=VERIFIED_IDENTITY):
    assert job_store.create_bracket_recovery(
        BRACKET_ID,
        symbol=SYMBOL,
        entry_order_id=ORDER_ID,
        close_side="sell",
        quantity=quantity,
        legs=[
            recovery_tests._leg("stop-loss", "stop-algo", "placed", STOP),
            recovery_tests._leg("take-profit", "target-algo", "placed", TARGET),
        ],
        now="2026-11-05T10:00:00+00:00",
        venue_key=VENUE,
        identity=identity,
    )


def test_cleanup_guard_accepts_only_verified_terminal_fill_matching_bracket(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    _seed_cleanup_bracket()
    bracket = job_store.load_bracket_recovery(BRACKET_ID)
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")

    assert _cleanup_allowed(bridge, bracket) is True


def test_cleanup_guard_accepts_full_fill_and_rejects_active_entry(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    _seed_cleanup_bracket(quantity=ORIGINAL_QUANTITY)
    bracket = job_store.load_bracket_recovery(BRACKET_ID)
    exchange.orders[ORDER_ID] = _terminal_order("FILLED", filled=ORIGINAL_QUANTITY)
    assert _cleanup_allowed(bridge, bracket) is True

    exchange.orders[ORDER_ID] = recovery_tests._entry_order(
        status="PARTIALLY_FILLED", quantity=ORIGINAL_QUANTITY, filled=FILLED_QUANTITY,
    )
    assert _cleanup_allowed(bridge, bracket) is False


@pytest.mark.parametrize("invalid", ["wrong_bracket_quantity", "wrong_original_quantity", "wrong_order_id", "wrong_identity"])
def test_cleanup_guard_fails_closed_on_mismatched_entry_or_bracket(
    tmp_path, monkeypatch, invalid,
):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    quantity = FILLED_QUANTITY - 0.1 if invalid == "wrong_bracket_quantity" else FILLED_QUANTITY
    identity = VERIFIED_IDENTITY
    if invalid == "wrong_identity":
        from tests.execution_derivatives_support import identity_for_api_key

        identity = identity_for_api_key("other-account")
    _seed_cleanup_bracket(quantity=quantity, identity=identity)
    bracket = job_store.load_bracket_recovery(BRACKET_ID)
    item = _terminal_order("CANCELED")
    if invalid == "wrong_original_quantity":
        item["info"]["origQty"] = "3"
        item["amount"] = 3.0
    elif invalid == "wrong_order_id":
        item["id"] = "other-order"
        item["info"]["orderId"] = "other-order"
    exchange.orders[ORDER_ID] = item

    assert _cleanup_allowed(bridge, bracket) is False


def test_cleanup_guard_rejects_nonterminal_and_missing_entry_evidence(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    _seed_cleanup_bracket()
    bracket = job_store.load_bracket_recovery(BRACKET_ID)
    exchange.orders[ORDER_ID] = recovery_tests._entry_order(
        status="PARTIALLY_FILLED", quantity=ORIGINAL_QUANTITY, filled=FILLED_QUANTITY,
    )
    assert _cleanup_allowed(bridge, bracket) is False

    exchange.orders.pop(ORDER_ID)
    assert _cleanup_allowed(bridge, bracket) is False


def test_cleanup_guard_requires_operation_lock(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    _seed_cleanup_bracket()
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    bracket = job_store.load_bracket_recovery(BRACKET_ID)

    from tradingagents.execution import futures_entry_recovery

    assert futures_entry_recovery.entry_allows_exit_cleanup(bridge, bracket) is False


def test_terminal_partial_exit_cleanup_and_intent_reconcile_complete_in_order(tmp_path, monkeypatch):
    from tradingagents.execution.brackets import BracketManager

    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    _seed_flat_bracket(exchange)
    task = job_store.load_bracket_recovery(BRACKET_ID)
    job_store.update_bracket_recovery(
        BRACKET_ID, status="monitoring", placed_ids=task["placed_ids"], attempts=0,
        last_error=None, now=task["updated_at"], legs=task["legs"],
    )
    monkeypatch.setattr(exchange, "fetch_open_orders", lambda symbol, params=None: [], raising=False)
    monkeypatch.setattr(bridge, "_redact", lambda value: value, raising=False)

    assert _reconcile(bridge) == "bracket_evidence_incomplete"
    assert BracketManager(bridge).reconcile(
        BRACKET_ID, repair_missing=False, allow_sibling_cancel=True,
    ) == "reconciled_stop_loss"
    assert _reconcile(bridge) == "completed"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "completed"


def test_cleanup_guard_retains_protection_for_open_or_unknown_position(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(tmp_path, monkeypatch, position=_partial_position())
    _seed_cleanup_bracket()
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    bracket = job_store.load_bracket_recovery(BRACKET_ID)

    assert _cleanup_allowed(bridge, bracket) is False


def test_partial_reconciliation_is_idempotent_after_restart(tmp_path, monkeypatch):
    exchange, bridge = recovery_tests._setup(
        tmp_path, monkeypatch, position=_partial_position(),
    )
    exchange.orders[ORDER_ID] = _terminal_order("CANCELED")
    exchange.trades[0]["qty"] = str(FILLED_QUANTITY)
    _set_intent_terms()
    _seed_open_bracket(exchange)
    _write_audit(Path(bridge.config["exec_log_path"]), [_audit_rows(bridge)[0]])

    assert _reconcile(bridge) == "completed"
    first_calls = list(exchange.calls)
    assert _reconcile(bridge) == "already_reconciled"
    assert exchange.calls == first_calls
