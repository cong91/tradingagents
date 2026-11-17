"""Fail-closed recovery of uncertain futures entries with missing brackets."""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pytest

from server import db, job_store
from server.auto_modes import AutoMode
from tests import test_futures_entry_recovery as support
from tests.execution_derivatives_support import VERIFIED_IDENTITY, identity_for_api_key
from tradingagents.dataflows.config import set_config
from tradingagents.execution import (
    futures_entry_recovery,
    futures_entry_recovery_snapshot,
    futures_identity,
)
from tradingagents.execution.auto_authorization import capture_authorization
from tradingagents.execution.futures_account import FuturesPosition
from tradingagents.execution.futures_identity import compare_futures_account_identity
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit

OTHER_MARKET = {
    "id": "XYZUSDT", "symbol": "XYZ/USDT:USDT", "contract": True,
    "linear": True, "inverse": False, "quote": "USDT", "settle": "USDT",
}


@pytest.fixture
def missing_bracket(tmp_path, monkeypatch, postgres_schema):
    position = FuturesPosition(
        symbol=support.SYMBOL, side="long", contracts=support.QUANTITY,
        quantity=support.QUANTITY, entry_price=support.ENTRY_PRICE,
        mark_price=support.ENTRY_PRICE,
    )
    exchange, bridge = support._setup(tmp_path, monkeypatch, position=position)
    exchange.markets_by_id = {"XYZUSDT": OTHER_MARKET}
    intent = job_store.load_bracket_recovery(support.INTENT_ID)
    leg = intent["legs"][0]
    leg.update(
        order_id=None,
        original_stop_loss=support.STOP,
        take_profit=support.TARGET,
        entry_provenance={
            "version": 1,
            "kind": "managed_live_entry",
            "intent_id": support.INTENT_ID,
            "client_order_id": support.CLIENT_ID,
            "symbol": support.SYMBOL,
            "side": "buy",
            "venue_key": support.VENUE,
            "account_identity": {
                "exchange": VERIFIED_IDENTITY.exchange,
                "profile": VERIFIED_IDENTITY.profile,
                "type": VERIFIED_IDENTITY.account_type,
                "hash": VERIFIED_IDENTITY.identity_hash,
            },
        },
    )
    job_store.update_bracket_recovery(
        support.INTENT_ID, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error="entry submission ACK lost", now="2026-11-05T10:00:00+00:00",
        legs=[leg],
    )
    exchange.orders[support.CLIENT_ID] = support._entry_order()
    support._insert_audit(support._audit_event(), tmp_path / "execution.jsonl")

    # These lists model both CCXT regular and trigger-order queries. The current
    # entry reconciliation returns before making either query when no bracket exists.
    exchange.open_regular_orders = []
    exchange.open_algo_orders = []

    def fetch_open_orders(symbol, since=None, limit=None, params=None):
        exchange.calls.append(("fetch_open_orders", symbol, since, limit, params))
        orders = exchange.open_algo_orders if (params or {}).get("trigger") else exchange.open_regular_orders
        return list(orders)

    monkeypatch.setattr(exchange, "fetch_open_orders", fetch_open_orders, raising=False)
    for method in ("create_order", "cancel_order", "cancel_all_orders"):
        monkeypatch.setattr(
            exchange, method, Mock(side_effect=AssertionError(f"unexpected {method}")),
            raising=False,
        )
    return exchange, bridge


def _assert_no_venue_mutation(exchange):
    exchange.create_order.assert_not_called()
    exchange.cancel_order.assert_not_called()
    exchange.cancel_all_orders.assert_not_called()


def _assert_no_recovery_mutation(exchange, before):
    assert job_store.load_bracket_recovery(support.INTENT_ID) == before
    assert job_store.load_position_state(f"{support.SYMBOL}:buy:{support.ORDER_ID}") is None
    assert support._control_events("futures_entry_reconciled") == []
    assert not [call for call in exchange.calls if call[0] == "fetch_open_orders"]
    _assert_no_venue_mutation(exchange)


def _seed_bracket(task_id):
    support._seed_task(
        task_id, entry_order_id=support.ORDER_ID,
        legs=[
            support._leg("stop-loss", None, "pending", support.STOP),
            support._leg("take-profit", None, "pending", support.TARGET),
        ],
        placed_ids=[],
    )


@pytest.mark.parametrize("known_venue_id", [False, True], ids=["lost-ack", "known-venue-id"])
def test_open_filled_entry_without_linked_bracket_is_missing(missing_bracket, known_venue_id):
    exchange, bridge = missing_bracket
    if known_venue_id:
        intent = job_store.load_bracket_recovery(support.INTENT_ID)
        intent["legs"][0]["order_id"] = support.ORDER_ID
        job_store.update_bracket_recovery(
            support.INTENT_ID, status=intent["status"], placed_ids=[support.ORDER_ID],
            attempts=0, last_error="entry acknowledged; bracket missing",
            now="2026-11-05T10:00:01+00:00", legs=intent["legs"],
        )
        exchange.orders[support.ORDER_ID] = support._entry_order()

    before = job_store.load_bracket_recovery(support.INTENT_ID)
    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []

    assert support._result(bridge) == "bracket_missing"

    expected_lookup = (
        ("fetch_order", support.ORDER_ID, support.SYMBOL, {})
        if known_venue_id else
        ("fetch_order", support.CLIENT_ID, support.SYMBOL, {"origClientOrderId": support.CLIENT_ID})
    )
    assert expected_lookup in exchange.calls
    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    _assert_no_recovery_mutation(exchange, before)


def test_multiple_linked_brackets_remain_ambiguous(missing_bracket):
    exchange, bridge = missing_bracket
    _seed_bracket(support.BRACKET_ID)
    _seed_bracket(f"legacy-bracket:{support.SYMBOL}:{support.ORDER_ID}")
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    assert len(job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID)) == 2

    assert support._result(bridge) == "bracket_ambiguous"

    assert len(job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID)) == 2
    _assert_no_recovery_mutation(exchange, before)


@pytest.mark.parametrize(
    ("symbol", "entry_order_id"),
    [
        ("OTHER/USDT:USDT", "different-order"),
        ("OTHER/USDT:USDT", support.ORDER_ID),
        (support.SYMBOL, "different-order"),
    ],
    ids=["both-conflict", "symbol-conflict", "entry-conflict"],
)
def test_terminal_canonical_id_with_conflicting_identity_is_ambiguous(
    missing_bracket, symbol, entry_order_id,
):
    exchange, bridge = missing_bracket
    support._seed_task(
        support.BRACKET_ID, symbol=symbol, entry_order_id=entry_order_id, status="completed",
    )
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    conflicting = job_store.load_bracket_recovery(support.BRACKET_ID)
    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    assert support.BRACKET_ID not in {row["task_id"] for row in job_store.active_bracket_recoveries()}

    assert support._result(bridge) == "bracket_ambiguous"

    assert job_store.load_bracket_recovery(support.BRACKET_ID) == conflicting
    _assert_no_recovery_mutation(exchange, before)


@pytest.mark.parametrize(
    "symbol", ["ABCUSDT", "abcusdt", "ABC/USDT", "ABC/USDT:USD", "UNRESOLVABLE", "UNKNOWN/USDT:USDT"],
)
def test_active_raw_or_unresolvable_symbol_remains_ambiguous(missing_bracket, symbol):
    exchange, bridge = missing_bracket
    competing_id = f"entry-intent:{symbol}:other-client"
    support._seed_task(competing_id, symbol=symbol, entry_order_id="other-client")
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    competing = job_store.load_bracket_recovery(competing_id)

    assert support._result(bridge) == "bracket_ambiguous"

    assert job_store.load_bracket_recovery(competing_id) == competing
    _assert_no_recovery_mutation(exchange, before)


@pytest.mark.parametrize("linked", [False, True], ids=["missing", "linked"])
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("status", "completed"),
        ("status", "pending"),
        ("venue_key", "other-venue"),
        ("account_identity_exchange", "different-exchange"),
        ("account_identity_profile", "different-profile"),
        ("account_identity_type", "different-type"),
        ("account_identity_hash", "a" * 64),
        ("quantity", 3.0),
        ("close_side", "buy"),
        ("updated_at", "different-time"),
        ("legs", "[]"),
        ("placed_ids", '["another-order"]'),
    ],
)
def test_intent_changed_after_initial_load_is_stale(
    missing_bracket, monkeypatch, linked, column, value,
):
    exchange, bridge = missing_bracket
    if linked:
        _seed_bracket(support.BRACKET_ID)
    original_fetch = exchange.fetch_order
    changed = []

    def fetch_with_concurrent_change(*args, **kwargs):
        with db.session() as conn:
            # Column comes exclusively from the static parametrization above.
            conn.execute(
                f"UPDATE bracket_recovery_tasks SET {column} = %s WHERE task_id = %s",
                (value, support.INTENT_ID),
            )
        changed.append(job_store.load_bracket_recovery(support.INTENT_ID))
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(exchange, "fetch_order", fetch_with_concurrent_change)

    assert support._result(bridge) == "stale_state"

    assert len(changed) == 1
    _assert_no_recovery_mutation(exchange, changed[0])


@pytest.mark.parametrize("linked", [False, True], ids=["missing", "linked"])
def test_intent_deleted_after_initial_load_is_stale(missing_bracket, monkeypatch, linked):
    exchange, bridge = missing_bracket
    if linked:
        _seed_bracket(support.BRACKET_ID)
    original_fetch = exchange.fetch_order

    def fetch_after_delete(*args, **kwargs):
        with db.session() as conn:
            conn.execute("DELETE FROM bracket_recovery_tasks WHERE task_id = %s", (support.INTENT_ID,))
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(exchange, "fetch_order", fetch_after_delete)

    assert support._result(bridge) == "stale_state"

    _assert_no_recovery_mutation(exchange, None)


@pytest.mark.parametrize("linked", [False, True], ids=["missing", "linked"])
def test_competing_active_task_remains_ambiguous(missing_bracket, linked):
    exchange, bridge = missing_bracket
    if linked:
        _seed_bracket(support.BRACKET_ID)
    competing_id = f"entry-intent:{support.SYMBOL}:other-client"
    support._seed_task(
        competing_id, entry_order_id="other-client", placed_ids=[],
        legs=[{
            "name": "entry-intent", "status": "uncertain", "order_id": None,
            "client_order_id": "other-client", "symbol": support.SYMBOL,
            "side": "buy", "type": "market", "quantity": f"{support.QUANTITY:.3f}",
            "profile": "demo", "venue_key": support.VENUE,
        }],
    )
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    competing_before = job_store.load_bracket_recovery(competing_id)
    assert len(job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID)) == int(linked)
    assert competing_id in {task["task_id"] for task in job_store.active_bracket_recoveries()}

    assert support._result(bridge) == "bracket_ambiguous"

    assert job_store.load_bracket_recovery(competing_id) == competing_before
    _assert_no_recovery_mutation(exchange, before)


def test_disabled_auto_authorization_cannot_cause_bracket_mutation(missing_bracket):
    exchange, bridge = missing_bracket
    set_config({"auto_futures_enabled": False})
    authorization = capture_authorization()
    assert not authorization.allows(AutoMode.FUTURES, live=True)
    before = job_store.load_bracket_recovery(support.INTENT_ID)

    assert support._result(bridge) == "bracket_missing"

    _assert_no_recovery_mutation(exchange, before)


def test_unrelated_active_symbol_does_not_hide_missing_bracket(missing_bracket):
    exchange, bridge = missing_bracket
    other_symbol = "XYZ/USDT:USDT"
    other_id = f"entry-intent:{other_symbol}:other-client"
    support._seed_task(other_id, symbol=other_symbol, entry_order_id="other-client")
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    other_before = job_store.load_bracket_recovery(other_id)

    assert support._result(bridge) == "bracket_missing"

    assert job_store.load_bracket_recovery(other_id) == other_before
    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    _assert_no_recovery_mutation(exchange, before)


def test_missing_bracket_never_authorizes_exit_cleanup(missing_bracket):
    exchange, bridge = missing_bracket
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    bracket = {"task_id": support.BRACKET_ID, "symbol": support.SYMBOL}

    with operation_lock(support.SYMBOL):
        assert futures_entry_recovery.entry_allows_exit_cleanup(bridge, bracket) is False

    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    _assert_no_recovery_mutation(exchange, before)


def test_unreadable_bracket_store_remains_unavailable(missing_bracket, monkeypatch):
    exchange, bridge = missing_bracket
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot",
        Mock(side_effect=OSError("store unavailable")),
    )

    assert support._result(bridge) == "bracket_unavailable"

    _assert_no_recovery_mutation(exchange, before)


@pytest.mark.parametrize(
    ("extra_rows", "expected"),
    [
        (None, "bracket_unavailable"),
        ({}, "bracket_unavailable"),
        ([None], "bracket_ambiguous"),
        ([{}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": None, "task_id": "unknown"}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": 42, "task_id": "unknown"}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": support.SYMBOL}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": support.SYMBOL, "task_id": ["unknown"]}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": support.SYMBOL, "task_id": support.BRACKET_ID}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": "OTHER/USDT:USDT", "task_id": support.BRACKET_ID}], "bracket_ambiguous"),
        ([{"status": "pending", "symbol": "OTHER/USDT:USDT", "task_id": support.INTENT_ID}], "stale_state"),
        ([{"status": "pending", "symbol": support.SYMBOL, "task_id": support.INTENT_ID, "entry_order_id": "other"}],
         "stale_state"),
        ([{"status": "pending", "symbol": "OTHER/USDT:USDT", "task_id": f"bracket:{support.SYMBOL}:other"}],
         "bracket_ambiguous"),
    ],
    ids=[
        "none-list", "dict-list", "none-row", "empty-row", "none-symbol", "numeric-symbol",
        "missing-id", "unhashable-id", "incomplete-linked-id", "linked-id-wrong-symbol",
        "intent-id-wrong-symbol", "intent-id-wrong-entry", "other-id-wrong-symbol",
    ],
)
def test_malformed_snapshot_tasks_never_imply_no_competition(
    missing_bracket, monkeypatch, extra_rows, expected,
):
    exchange, bridge = missing_bracket
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    snapshot = futures_entry_recovery_snapshot._read_snapshot(
        support.SYMBOL, support.ORDER_ID, support.INTENT_ID,
    )
    if isinstance(extra_rows, list):
        if any(isinstance(row, dict) and row.get("task_id") == support.INTENT_ID for row in extra_rows):
            snapshot = [row for row in snapshot if row["task_id"] != support.INTENT_ID]
        extra_rows = [*snapshot, *extra_rows]
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: extra_rows)

    assert support._result(bridge) == expected

    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    _assert_no_recovery_mutation(exchange, before)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"task_id": "bracket:other"}, "bracket_ambiguous"),
        ({"symbol": "OTHER/USDT:USDT"}, "bracket_ambiguous"),
        ({"entry_order_id": "other-order"}, "bracket_ambiguous"),
        ({"venue_key": "other-venue"}, "venue_mismatch"),
        ({"account_identity_hash": None}, "account_identity_unknown"),
        ({"account_identity_hash": "other-account"}, "account_identity_unknown"),
        ({"close_side": "buy"}, "bracket_evidence_incomplete"),
    ],
    ids=["task-id", "symbol", "entry-id", "venue", "missing-identity", "different-identity", "side"],
)
def test_inconsistent_linked_bracket_stays_fail_closed(
    missing_bracket, monkeypatch, changes, expected,
):
    exchange, bridge = missing_bracket
    _seed_bracket(support.BRACKET_ID)
    before = job_store.load_bracket_recovery(support.INTENT_ID)
    stored = job_store.load_bracket_recovery(support.BRACKET_ID)
    row = deepcopy(stored)
    row.update(changes)
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [before, row],
    )

    assert support._result(bridge) == expected

    assert job_store.load_bracket_recovery(support.BRACKET_ID) == stored
    _assert_no_recovery_mutation(exchange, before)


@pytest.fixture
def bracket_lookup(monkeypatch):
    identity = {
        "account_identity_exchange": VERIFIED_IDENTITY.exchange,
        "account_identity_profile": VERIFIED_IDENTITY.profile,
        "account_identity_type": VERIFIED_IDENTITY.account_type,
        "account_identity_hash": VERIFIED_IDENTITY.identity_hash,
    }
    intent = {
        "task_id": support.INTENT_ID, "symbol": support.SYMBOL,
        "entry_order_id": support.CLIENT_ID, "venue_key": support.VENUE,
        "status": "submission_uncertain", "quantity": 2.0, "updated_at": "time",
        "legs": [{"name": "entry-intent", "client_order_id": support.CLIENT_ID}],
        "placed_ids": [], "close_side": "sell", **identity,
    }
    entry = futures_entry_recovery._EntryEvidence(
        support.ORDER_ID, support.CLIENT_ID, Decimal("2"), Decimal("2"),
        Decimal("10"), support.ENTRY_TIME, {},
    )
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [deepcopy(intent)],
    )
    writes = [
        Mock(side_effect=AssertionError(f"unexpected {method}"))
        for method in ("create_bracket_recovery", "update_bracket_recovery", "save_position_state")
    ]
    for method, write in zip(
        ("create_bracket_recovery", "update_bracket_recovery", "save_position_state"),
        writes, strict=True,
    ):
        monkeypatch.setattr(job_store, method, write)
    client = Mock(markets_by_id={"XYZUSDT": OTHER_MARKET})
    return intent, entry, writes, client


def test_missing_lookup_is_read_only_without_database(bracket_lookup):
    intent, entry, writes, client = bracket_lookup

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (
        None, "bracket_missing",
    )
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (None, "bracket_unavailable"),
        ({}, "bracket_unavailable"),
        ([None], "bracket_ambiguous"),
        (["bad"], "bracket_ambiguous"),
        ([{}, {}], "bracket_ambiguous"),
        ([{"symbol": support.SYMBOL, "task_id": ["bad"]}], "bracket_ambiguous"),
    ],
)
def test_malformed_snapshot_is_fail_closed_without_database(
    bracket_lookup, monkeypatch, rows, expected,
):
    intent, entry, writes, client = bracket_lookup
    if isinstance(rows, list):
        rows = [deepcopy(intent), *rows]
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: rows)

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, expected)
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "completed"), ("status", "pending"), ("task_id", "other"),
        ("symbol", "OTHER/USDT:USDT"), ("entry_order_id", "other"),
        ("venue_key", "other"), ("close_side", "buy"), ("quantity", 3.0),
        ("updated_at", "later"), ("legs", []), ("placed_ids", ["other"]),
        ("account_identity_exchange", "other"), ("account_identity_profile", "other"),
        ("account_identity_type", "other"), ("account_identity_hash", "other"),
    ],
)
def test_changed_intent_snapshot_is_stale_without_database(bracket_lookup, monkeypatch, field, value):
    intent, entry, writes, client = bracket_lookup
    current = deepcopy(intent)
    current[field] = value
    rows = [] if field == "task_id" else [current]
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: rows)

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "stale_state")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize(
    "symbol", ["ABCUSDT", "abcusdt", "ABC/USDT", "ABC/USDT:USD", "UNRESOLVABLE", "UNKNOWN/USDT:USDT"],
)
def test_raw_or_unresolved_active_symbol_is_ambiguous_without_database(bracket_lookup, monkeypatch, symbol):
    intent, entry, writes, client = bracket_lookup
    competitor = {
        "task_id": f"entry-intent:{symbol}:other", "symbol": symbol, "status": "pending",
    }
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, competitor],
    )

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"symbol": "OTHER/USDT:USDT"}, {"entry_order_id": "other"},
    {"symbol": "OTHER/USDT:USDT", "entry_order_id": "other"},
])
def test_conflicting_terminal_canonical_task_is_ambiguous_without_database(bracket_lookup, monkeypatch, changes):
    intent, entry, writes, client = bracket_lookup
    canonical = {
        "task_id": support.BRACKET_ID, "symbol": support.SYMBOL,
        "entry_order_id": support.ORDER_ID, "status": "completed", **changes,
    }
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, canonical],
    )

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


def test_unrelated_canonical_symbol_does_not_block_lookup_without_database(bracket_lookup, monkeypatch):
    intent, entry, writes, client = bracket_lookup
    other = {"task_id": "entry-intent:XYZ/USDT:USDT:other", "symbol": "XYZ/USDT:USDT", "status": "pending"}
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, other])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_missing")
    for write in writes:
        write.assert_not_called()


def test_conflicting_canonical_market_indexes_remain_ambiguous(bracket_lookup, monkeypatch):
    intent, entry, writes, client = bracket_lookup
    symbol = "XYZ/USDT:USDT"
    client.markets_by_id = {"XYZUSDT": OTHER_MARKET}
    client.markets = {symbol: {**OTHER_MARKET, "id": "ABCUSDT"}}
    other = {"task_id": f"entry-intent:{symbol}:other", "symbol": symbol, "status": "pending"}
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, other])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize("base", ["ABC", "XYZ"])
def test_verified_unrelated_dated_market_does_not_block_lookup(bracket_lookup, monkeypatch, base):
    intent, entry, writes, client = bracket_lookup
    symbol = f"{base}/USDT:USDT-251226"
    market_id = f"{base}USDT_251226"
    market = {**OTHER_MARKET, "id": market_id, "symbol": symbol, "future": True}
    client.markets = {symbol: market}
    client.markets_by_id = {market_id: [market]}
    other = {"task_id": f"entry-intent:{symbol}:other", "symbol": symbol, "status": "pending"}
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, other])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_missing")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize("fault", [
    "missing", "conflict", "duplicate", "alias", "nonfuture", "same_id",
    "future_index_conflict", "future_reverse_conflict",
])
def test_unverified_dated_market_remains_ambiguous(bracket_lookup, monkeypatch, fault):
    intent, entry, writes, client = bracket_lookup
    symbol = "XYZ/USDT:USDT-251226"
    market_id = "ABCUSDT" if fault == "same_id" else "XYZUSDT_251226"
    market = {**OTHER_MARKET, "id": market_id, "symbol": symbol, "future": True}
    client.markets = {symbol: market}
    client.markets_by_id = {market_id: [market]}
    if fault == "missing":
        client.markets = {}
    elif fault == "conflict":
        client.markets[symbol] = {**market, "settle": "USDC"}
    elif fault == "duplicate":
        client.markets_by_id[market_id].append(market)
    elif fault == "alias":
        client.markets_by_id["OTHER"] = [{**market, "id": "OTHER"}]
    elif fault == "nonfuture":
        market["future"] = False
    elif fault == "future_index_conflict":
        client.markets_by_id[market_id] = [{**market, "future": False}]
    elif fault == "future_reverse_conflict":
        client.markets[symbol] = {**market, "future": False}
    other = {"task_id": f"entry-intent:{symbol}:other", "symbol": symbol, "status": "pending"}
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, other])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize("markets", [
    {},
    {"XYZUSDT": [OTHER_MARKET, OTHER_MARKET]},
    {"XYZUSDT": {**OTHER_MARKET, "symbol": "ABC/USDT:USDT"}},
    {"XYZUSDT": {**OTHER_MARKET, "linear": False}},
    {"XYZUSDT": OTHER_MARKET, "OTHER": {**OTHER_MARKET, "id": "OTHER"}},
])
def test_unresolved_market_metadata_is_ambiguous_without_database(bracket_lookup, monkeypatch, markets):
    intent, entry, writes, client = bracket_lookup
    client.markets_by_id = markets
    other = {"task_id": "entry-intent:XYZ/USDT:USDT:other", "symbol": "XYZ/USDT:USDT", "status": "pending"}
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, other])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize("status", ["submission_uncertain", "completed"])
def test_matching_canonical_bracket_is_returned_without_database(bracket_lookup, monkeypatch, status):
    intent, entry, writes, client = bracket_lookup
    bracket = {
        **intent, "task_id": support.BRACKET_ID, "entry_order_id": support.ORDER_ID,
        "status": status,
    }
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, bracket])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (bracket, None)
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"venue_key": "other"}, "venue_mismatch"),
        ({"account_identity_hash": None}, "account_identity_unknown"),
        ({"account_identity_hash": "other"}, "account_identity_unknown"),
        ({"close_side": "buy"}, "bracket_evidence_incomplete"),
    ],
)
def test_linked_bracket_guards_apply_without_database(bracket_lookup, monkeypatch, changes, expected):
    intent, entry, writes, client = bracket_lookup
    bracket = {
        **intent, "task_id": support.BRACKET_ID, "entry_order_id": support.ORDER_ID,
        "status": "completed", **changes,
    }
    monkeypatch.setattr(futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, bracket])

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, expected)
    for write in writes:
        write.assert_not_called()


def test_multiple_linked_tasks_are_ambiguous_without_database(bracket_lookup, monkeypatch):
    intent, entry, writes, client = bracket_lookup
    bracket = {
        **intent, "task_id": support.BRACKET_ID, "entry_order_id": support.ORDER_ID,
        "status": "completed",
    }
    duplicate = {**bracket, "task_id": f"legacy-bracket:{support.SYMBOL}:{support.ORDER_ID}"}
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", lambda *args: [intent, bracket, duplicate],
    )

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_ambiguous")
    for write in writes:
        write.assert_not_called()


def test_snapshot_failure_is_unavailable_without_database(bracket_lookup, monkeypatch):
    intent, entry, writes, client = bracket_lookup
    monkeypatch.setattr(
        futures_entry_recovery_snapshot, "_read_snapshot", Mock(side_effect=OSError("unavailable")),
    )

    assert futures_entry_recovery._find_bracket(intent, entry, support.VENUE, client) == (None, "bracket_unavailable")
    for write in writes:
        write.assert_not_called()


@pytest.mark.parametrize(
    ("observed_identity", "expected"),
    [
        (identity_for_api_key("different-test-account"), "account_identity_mismatch"),
        (None, "account_identity_unknown"),
    ],
    ids=["different-account", "unknown-account"],
)
def test_account_identity_guard_precedes_entry_lookup(
    missing_bracket, monkeypatch, observed_identity, expected,
):
    exchange, bridge = missing_bracket
    monkeypatch.setattr(futures_identity, "compare_futures_account_identity", compare_futures_account_identity)
    monkeypatch.setattr(futures_identity, "read_futures_account_identity", lambda current: observed_identity)
    before = job_store.load_bracket_recovery(support.INTENT_ID)

    assert support._result(bridge) == expected

    assert not [call for call in exchange.calls if call[0] == "fetch_order"]
    assert job_store.load_bracket_recovery(support.INTENT_ID) == before
    assert job_store.find_bracket_recoveries(support.SYMBOL, support.ORDER_ID) == []
    _assert_no_venue_mutation(exchange)
