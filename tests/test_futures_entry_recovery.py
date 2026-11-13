"""Offline evidence and persistence checks for futures entry recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from server import control_store, job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    _recovery_db as _recovery_db,
)
from tradingagents.execution import futures_identity
from tradingagents.execution.futures_account import (
    FuturesPosition,
    FuturesPositionsObservation,
)
from tradingagents.execution.futures_entry_recovery import reconcile_entry
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit

SYMBOL = "ABC/USDT:USDT"
ORDER_ID = "entry-venue-1"
CLIENT_ID = "entry-client-1"
INTENT_ID = f"entry-intent:{SYMBOL}:{CLIENT_ID}"
BRACKET_ID = f"bracket:{SYMBOL}:{ORDER_ID}"
VENUE = "binance|https://demo-api.binance.com|False"
QUANTITY = 2.0
ENTRY_PRICE = 10.0
STOP = 9.0
TARGET = 12.0
ENTRY_TIME = 1_793_914_952_211


class FakeExchange:
    def __init__(self):
        self.apiKey = "test-api-key"
        self.secret = "test-secret"
        self.urls = {"api": {"fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3"}}
        self.options = {"enableDemoTrading": True, "sandboxMode": False}
        self.verify = True
        self.validateServerSsl = True
        self.verbose = False
        self.calls = []
        self.orders = {}
        self.fetch_error = None
        self.trades = [{
            "id": 1,
            "orderId": ORDER_ID,
            "symbol": "ABCUSDT",
            "side": "BUY",
            "positionSide": "BOTH",
            "qty": str(QUANTITY),
            "price": str(ENTRY_PRICE),
            "time": ENTRY_TIME,
        }]

    def fapiPrivateGetUserTrades(self, params):
        self.calls.append(("fapiPrivateGetUserTrades", params))
        return list(self.trades)

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.3f}"

    def price_to_precision(self, symbol, price):
        return f"{float(price):.3f}"

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("fetch_order", order_id, symbol, params))
        if self.fetch_error is not None:
            raise self.fetch_error
        if order_id not in self.orders:
            raise LookupError("order unavailable")
        return self.orders[order_id]


class FakeBridge:
    exchange_id = "binance"

    def __init__(self, exchange, audit_path):
        self.config = {
            "exec_base_url": "https://demo-api.binance.com",
            "exec_sandbox": False,
            "exec_log_path": str(audit_path),
        }
        self._exchange = exchange

    def client(self):
        return self._exchange


def _entry_order(*, order_id=ORDER_ID, client_id=CLIENT_ID, status="FILLED",
                 side="BUY", quantity=QUANTITY, filled=QUANTITY, price=ENTRY_PRICE):
    raw = {
        "orderId": order_id,
        "clientOrderId": client_id,
        "symbol": "ABCUSDT",
        "status": status,
        "type": "MARKET",
        "side": side,
        "positionSide": "BOTH",
        "reduceOnly": False,
        "origQty": str(quantity),
        "executedQty": str(filled),
        "avgPrice": "" if price is None else str(price),
        "cumQuote": "" if price is None else str(float(filled) * float(price)),
        "time": ENTRY_TIME,
        "updateTime": ENTRY_TIME,
    }
    return {
        "id": order_id,
        "clientOrderId": client_id,
        "symbol": SYMBOL,
        "status": "closed" if status == "FILLED" else status.lower(),
        "type": "market",
        "side": side.lower(),
        "amount": quantity,
        "filled": filled,
        "average": price,
        "price": price,
        "info": raw,
    }


def _conditional(order_id, name, status="NEW", *, quantity=QUANTITY, trigger=None,
                 child_id=None, child_quantity=None):
    stop = name == "stop-loss"
    order_type = "STOP_MARKET" if stop else "TAKE_PROFIT_MARKET"
    trigger = trigger if trigger is not None else STOP if stop else TARGET
    raw = {
        "algoId": order_id,
        "symbol": "ABCUSDT",
        "side": "SELL",
        "algoStatus": status,
        "orderType": order_type,
        "triggerPrice": str(trigger),
        "quantity": str(quantity),
        "reduceOnly": True,
        "workingType": "MARK_PRICE",
        "createTime": ENTRY_TIME + 100,
        "updateTime": ENTRY_TIME + 100,
    }
    if child_id is not None:
        raw.update(actualOrderId=child_id, actualQty=str(child_quantity or quantity))
    return {
        "id": order_id,
        "symbol": SYMBOL,
        "status": "closed" if status == "FINISHED" else "canceled" if status == "CANCELED" else "open",
        "type": "market",
        "side": "sell",
        "amount": quantity,
        "triggerPrice": trigger,
        "reduceOnly": True,
        "info": raw,
    }


def _child_order(order_id="stop-child", *, quantity=QUANTITY, filled=QUANTITY, status="FILLED"):
    raw = {
        "orderId": order_id,
        "symbol": "ABCUSDT",
        "status": status,
        "type": "MARKET",
        "side": "SELL",
            "positionSide": "BOTH",
            "reduceOnly": True,
            "origQty": str(quantity),

        "executedQty": str(filled),
        "avgPrice": "9.0",
        "clientOrderId": "",
        "time": ENTRY_TIME + 200,
        "updateTime": ENTRY_TIME + 200,
    }
    return {
        "id": order_id,
        "symbol": SYMBOL,
        "status": "closed" if status == "FILLED" else status.lower(),
        "type": "market",
        "side": "sell",
        "amount": quantity,
        "filled": filled,
        "average": 9.0,
        "info": raw,
    }


def _leg(name, order_id, status, trigger):
    param = "stopLossPrice" if name == "stop-loss" else "takeProfitPrice"
    return {
        "name": name,
        "params": {param: trigger},
        "status": status,
        "order_id": order_id,
    }


def _seed_task(task_id, *, symbol=SYMBOL, entry_order_id=CLIENT_ID, close_side="sell",
               status="submission_uncertain", quantity=QUANTITY, legs=None, placed_ids=None):
    assert job_store.create_bracket_recovery(
        task_id,
        symbol=symbol,
        entry_order_id=entry_order_id,
        close_side=close_side,
        quantity=quantity,
        legs=legs or [{"name": "entry-intent", "status": "uncertain",
                       "order_id": ORDER_ID, "client_order_id": CLIENT_ID,
                       "symbol": symbol, "side": "buy", "type": "market",
                       "quantity": f"{quantity:.3f}", "profile": "demo",
                       "venue_key": VENUE}],
        now=datetime.now(UTC).isoformat(),
        venue_key=VENUE,
        identity=VERIFIED_IDENTITY,
    )
    job_store.update_bracket_recovery(
        task_id,
        status=status,
        placed_ids=[ORDER_ID] if placed_ids is None else placed_ids,
        attempts=0,
        last_error=None,
        now=datetime.now(UTC).isoformat(),
    )


def _audit_event(*, order_id=ORDER_ID, venue=VENUE, symbol=SYMBOL, stop=STOP,
                 target=TARGET, action="entry_uncertain", client_id=CLIENT_ID):
    event = {
        "timestamp": "2026-11-05T10:00:00+00:00",
        "action": action,
        "phase": "execute",
        "confirmed": True,
        "market": "derivatives",
        "mode": "live",
        "signal": "Buy",
        "order_id": order_id,
        "ccxt_symbol": symbol,
        "venue_key": venue,
        "stop_loss": stop,
        "take_profit": target,
        "qty": QUANTITY,
        "price_est": ENTRY_PRICE,
    }
    if client_id is not None:
        event["client_order_id"] = client_id
    return event


def _setup(tmp_path, monkeypatch, *, position=None, scope_complete=True):
    exchange = FakeExchange()
    bridge = FakeBridge(exchange, tmp_path / "execution.jsonl")
    _seed_task(INTENT_ID)
    monkeypatch.setattr(
        futures_identity,
        "_configured_profile_and_client",
        lambda current: ("demo", exchange),
    )
    monkeypatch.setattr(futures_identity, "_matches_profile_route", lambda client, profile: True)
    monkeypatch.setattr(
        "server.binance_futures_positions.validate_client",
        lambda client, profile, base_url: None,
    )
    monkeypatch.setattr(
        futures_identity,
        "compare_futures_account_identity",
        lambda current, expected: futures_identity.IdentityComparison.MATCH,
    )
    positions = () if position is None else (position,)
    monkeypatch.setattr(
        "tradingagents.execution.futures_account.observe_futures_positions",
        lambda current: FuturesPositionsObservation(positions, frozenset(), False, scope_complete),
    )
    return exchange, bridge


def _add_terminal_bracket(exchange, *, quantity=QUANTITY):
    stop = _leg("stop-loss", "stop-algo", "filled", STOP)
    target = _leg("take-profit", "target-algo", "cancelled", TARGET)
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        status="reconciled_stop_loss",
        quantity=quantity,
        legs=[stop, target],
    )
    exchange.orders["stop-algo"] = _conditional(
        "stop-algo", "stop-loss", "FINISHED", quantity=quantity,
        child_id="stop-child", child_quantity=quantity,
    )
    exchange.orders["stop-child"] = _child_order(
        quantity=quantity, filled=quantity,
    )
    exchange.orders["target-algo"] = _conditional(
        "target-algo", "take-profit", "CANCELED", quantity=quantity,
    )


def _insert_audit(event, path):
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _result(bridge, task_id=INTENT_ID):
    with operation_lock(SYMBOL):
        return reconcile_entry(bridge, task_id)


def _control_events(action):
    return [event for event in control_store.load_server_audit() if event.get("action") == action]


def test_reconciles_filled_entry_after_verified_stop_and_flat_position(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "completed"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "completed"
    assert len(_control_events("futures_entry_reconciled")) == 1
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]


def test_terminal_partial_entry_persists_identity_bound_order_evidence(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    partial_quantity = QUANTITY / 2
    exchange.orders[ORDER_ID] = _entry_order(
        status="CANCELED", filled=partial_quantity,
    )
    _add_terminal_bracket(exchange, quantity=partial_quantity)

    assert _result(bridge) == "completed"
    recovered = job_store.load_bracket_recovery(INTENT_ID)
    entry_leg = next(leg for leg in recovered["legs"] if leg["name"] == "entry-intent")
    assert entry_leg["entry_order_final"] == {
        "status": "cancelled",
        "order_id": ORDER_ID,
        "client_order_id": CLIENT_ID,
        "symbol": SYMBOL,
        "venue_key": VENUE,
        "account_id": (
            f"binance:demo:{VERIFIED_IDENTITY.identity_hash}"
        ),
        "account_identity": {
            "exchange": "binance", "profile": "demo", "type": "usds_futures",
            "hash": VERIFIED_IDENTITY.identity_hash,
        },
        "original_quantity": str(QUANTITY),
        "filled_quantity": str(partial_quantity),
    }
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]


def test_filled_entry_does_not_write_terminal_partial_evidence(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "completed"
    recovered = job_store.load_bracket_recovery(INTENT_ID)
    entry_leg = next(leg for leg in recovered["legs"] if leg["name"] == "entry-intent")
    assert "entry_order_final" not in entry_leg


def test_reconciliation_is_idempotent_and_does_not_duplicate_event(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "completed"
    first_calls = list(exchange.calls)
    assert _result(bridge) == "already_reconciled"
    assert exchange.calls == first_calls
    assert len(_control_events("futures_entry_reconciled")) == 1


def test_recovery_persists_verified_venue_id_for_no_ack_intent(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    intent_leg = {
        "name": "entry-intent", "status": "uncertain", "order_id": None,
        "client_order_id": CLIENT_ID, "symbol": SYMBOL, "side": "buy",
        "type": "market", "quantity": f"{QUANTITY:.3f}", "profile": "demo",
        "venue_key": VENUE, "entry_provenance": {
            "version": 1, "kind": "managed_live_entry", "intent_id": INTENT_ID,
            "client_order_id": CLIENT_ID, "symbol": SYMBOL, "side": "buy",
            "venue_key": VENUE, "account_identity": {
                "exchange": "binance", "profile": "demo", "type": "usds_futures",
                "hash": VERIFIED_IDENTITY.identity_hash,
            },
        },
    }
    job_store.update_bracket_recovery(
        INTENT_ID, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error="no-ACK submission; venue lookup required",
        now=datetime.now(UTC).isoformat(), legs=[intent_leg],
    )
    exchange.orders[CLIENT_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "completed"
    recovered = job_store.load_bracket_recovery(INTENT_ID)
    assert recovered["status"] == "completed"
    assert recovered["entry_order_id"] == CLIENT_ID
    assert recovered["placed_ids"] == [ORDER_ID]
    recovered_leg = next(leg for leg in recovered["legs"] if leg["name"] == "entry-intent")
    assert recovered_leg["order_id"] == ORDER_ID
    assert recovered_leg["client_order_id"] == CLIENT_ID
    assert recovered_leg["entry_provenance"] == intent_leg["entry_provenance"]
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]




@pytest.mark.parametrize("mutation", ["identity", "ambiguous", "missing"])
def test_unverified_or_ambiguous_no_ack_never_persists_a_venue_id(
    tmp_path, monkeypatch, mutation,
):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    intent_leg = {
        "name": "entry-intent", "status": "uncertain", "order_id": None,
        "client_order_id": CLIENT_ID, "symbol": SYMBOL, "side": "buy",
        "type": "market", "quantity": f"{QUANTITY:.3f}", "profile": "demo",
        "venue_key": VENUE,
    }
    job_store.update_bracket_recovery(
        INTENT_ID, status="submission_uncertain",
        placed_ids=[] if mutation in {"identity", "missing"} else ["venue-a", "venue-b"],
        attempts=0, last_error="no-ACK submission; venue lookup required",
        now=datetime.now(UTC).isoformat(), legs=[intent_leg],
    )
    if mutation != "missing":
        exchange.orders[ORDER_ID] = _entry_order()
    if mutation == "identity":
        monkeypatch.setattr(
            futures_identity, "compare_futures_account_identity",
            lambda current, stored: futures_identity.IdentityComparison.MISMATCH,
        )

    before = job_store.load_bracket_recovery(INTENT_ID)
    expected = {
        "identity": "account_identity_mismatch",
        "ambiguous": "entry_ids_ambiguous",
        "missing": "entry_order_unavailable",
    }[mutation]
    assert _result(bridge) == expected
    after = job_store.load_bracket_recovery(INTENT_ID)
    assert after["status"] == before["status"] == "submission_uncertain"
    assert after["placed_ids"] == before["placed_ids"]
    assert after["legs"] == before["legs"]
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]


def test_competing_intent_update_cannot_commit_recovered_id(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    intent = job_store.load_bracket_recovery(INTENT_ID)
    intent_leg = intent["legs"][0]
    intent_leg["order_id"] = None
    job_store.update_bracket_recovery(
        INTENT_ID, status="submission_uncertain", placed_ids=[], attempts=0,
        last_error="no-ACK submission; venue lookup required",
        now=datetime.now(UTC).isoformat(), legs=[intent_leg],
    )
    exchange.orders[CLIENT_ID] = _entry_order()
    _add_terminal_bracket(exchange)
    comparisons = 0

    def compare_with_competing_update(current, expected):
        nonlocal comparisons
        comparisons += 1
        if comparisons == 2:
            current = job_store.load_bracket_recovery(INTENT_ID)
            job_store.update_bracket_recovery(
                INTENT_ID, status=current["status"], placed_ids=current["placed_ids"],
                attempts=current["attempts"], last_error="competing update",
                now=datetime.now(UTC).isoformat(), legs=current["legs"],
            )
        return futures_identity.IdentityComparison.MATCH

    monkeypatch.setattr(
        futures_identity, "compare_futures_account_identity", compare_with_competing_update,
    )

    assert _result(bridge) == "stale_state"
    recovered = job_store.load_bracket_recovery(INTENT_ID)
    assert recovered["status"] == "submission_uncertain"
    assert recovered["placed_ids"] == []
    assert recovered["legs"][0]["order_id"] is None


@pytest.mark.parametrize("identity_result, expected", [
    (futures_identity.IdentityComparison.MISMATCH, "account_identity_mismatch"),
    (futures_identity.IdentityComparison.UNKNOWN, "account_identity_unknown"),
])
def test_identity_mismatch_or_unknown_never_completes_intent(
    tmp_path, monkeypatch, identity_result, expected,
):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)
    monkeypatch.setattr(
        futures_identity, "compare_futures_account_identity",
        lambda current, stored: identity_result,
    )

    assert _result(bridge) == expected
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"
    assert _control_events("futures_entry_reconciled") == []


@pytest.mark.parametrize("changes", [
    {"order_id": "other-order"},
    {"client_id": "other-client"},
    {"side": "SELL"},
    {"status": "NEW", "filled": 0.0},
    {"quantity": 3.0},
    {"filled": 1.0},
    {"price": None},
])
def test_inconsistent_or_incomplete_entry_order_is_deferred(tmp_path, monkeypatch, changes):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order(**changes)
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "entry_order_unverified"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_unavailable_entry_order_and_pending_submission_are_never_completed(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.fetch_error = OSError("offline")

    assert _result(bridge) == "entry_order_unavailable"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"
    assert _control_events("futures_entry_reconciled") == []


def test_incomplete_position_scope_never_proves_flat(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch, scope_complete=False)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert _result(bridge) == "position_scope_unknown"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


@pytest.mark.parametrize("child", [None, _child_order(filled=0.0), _child_order(status="NEW")])
def test_terminal_bracket_requires_confirmed_child_fill(tmp_path, monkeypatch, child):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)
    if child is None:
        exchange.orders.pop("stop-child")
    else:
        exchange.orders["stop-child"] = child

    assert _result(bridge) == "bracket_evidence_incomplete"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_unknown_or_uncanceled_sibling_prevents_terminal_reconciliation(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)
    exchange.orders["target-algo"] = _conditional("target-algo", "take-profit", "NEW")

    assert _result(bridge) == "bracket_evidence_incomplete"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_open_position_reconciles_normal_ack_audit_and_preserves_existing_state(tmp_path, monkeypatch):
    from datetime import timedelta

    position = FuturesPosition(
        symbol=SYMBOL, side="long", contracts=QUANTITY, quantity=QUANTITY,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )
    exchange, bridge = _setup(tmp_path, monkeypatch, position=position)
    exchange.orders[ORDER_ID] = _entry_order()
    intent = job_store.load_bracket_recovery(INTENT_ID)
    intent_leg = intent["legs"][0]
    intent_leg.update(
        original_stop_loss=STOP, take_profit=TARGET, venue_key=VENUE, profile="demo",
    )
    job_store.update_bracket_recovery(
        INTENT_ID, status="submission_uncertain", placed_ids=[ORDER_ID], attempts=0,
        last_error="bracket resting status unverified", now="2026-11-05T10:00:00+00:00", legs=[intent_leg],
    )
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        legs=[_leg("stop-loss", "stop-algo", "placed", STOP),
              _leg("take-profit", "target-algo", "placed", TARGET)],
    )
    exchange.orders["stop-algo"] = _conditional("stop-algo", "stop-loss")
    exchange.orders["target-algo"] = _conditional("target-algo", "take-profit")
    _insert_audit(
        _audit_event(action="buy", client_id=None), Path(bridge.config["exec_log_path"]),
    )
    position_key = f"{SYMBOL}:buy:{ORDER_ID}"
    original_opened_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    assert job_store.persist_futures_entry_state(
        position_key, side="long", entry=ENTRY_PRICE, quantity=QUANTITY, stop=STOP,
        targets=[TARGET], mark=ENTRY_PRICE, order_id=ORDER_ID, identity=VERIFIED_IDENTITY,
    )
    prior = job_store.load_position_state(position_key)
    prior["snapshot"]["opened_at"] = original_opened_at
    prior["snapshot"]["bracket_task_id"] = BRACKET_ID
    prior["snapshot"]["venue_key"] = VENUE
    prior["bracket_task_id"] = BRACKET_ID
    prior["venue_key"] = VENUE
    assert job_store.save_position_state(position_key, prior["revision"], prior)
    prior = job_store.load_position_state(position_key)
    revision = prior["revision"]

    assert _result(bridge) == "completed"
    recovered = job_store.load_position_state(position_key)
    assert recovered["revision"] == revision
    assert recovered["snapshot"]["opened_at"] == original_opened_at
    assert recovered["bracket_task_id"] == BRACKET_ID
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "completed"


def test_open_position_requires_unique_correlated_audit_and_resting_bracket(tmp_path, monkeypatch):
    position = FuturesPosition(
        symbol=SYMBOL, side="long", contracts=QUANTITY, quantity=QUANTITY,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )
    exchange, bridge = _setup(tmp_path, monkeypatch, position=position)
    exchange.orders[ORDER_ID] = _entry_order()
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        legs=[_leg("stop-loss", "stop-algo", "placed", STOP),
              _leg("take-profit", "target-algo", "placed", TARGET)],
    )
    exchange.orders["stop-algo"] = _conditional("stop-algo", "stop-loss")
    exchange.orders["target-algo"] = _conditional("target-algo", "take-profit")
    _insert_audit(_audit_event(), Path(bridge.config["exec_log_path"]))

    assert _result(bridge) == "completed"
    position_key = f"{SYMBOL}:buy:{ORDER_ID}"
    state = job_store.load_position_state(position_key)
    assert state["snapshot"]["entry"] == ENTRY_PRICE
    assert state["snapshot"]["quantity"] == QUANTITY
    assert state["snapshot"]["initial_stop"] == STOP
    assert state["snapshot"]["targets"] == [TARGET]
    assert state["snapshot"]["opened_at"] == datetime.fromtimestamp(
        ENTRY_TIME / 1000, UTC,
    ).isoformat()
    assert len(_control_events("futures_entry_reconciled")) == 1


def test_open_position_defers_unknown_price_or_missing_correlated_audit(tmp_path, monkeypatch):
    position = FuturesPosition(
        symbol=SYMBOL, side="long", contracts=QUANTITY, quantity=QUANTITY,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )
    exchange, bridge = _setup(tmp_path, monkeypatch, position=position)
    exchange.orders[ORDER_ID] = _entry_order(price=None)
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        legs=[_leg("stop-loss", "stop-algo", "placed", STOP),
              _leg("take-profit", "target-algo", "placed", TARGET)],
    )
    exchange.orders["stop-algo"] = _conditional("stop-algo", "stop-loss")
    exchange.orders["target-algo"] = _conditional("target-algo", "take-profit")

    assert _result(bridge) == "entry_order_unverified"
    assert job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}") is None
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_open_position_rejects_duplicate_or_conflicting_original_audit(tmp_path, monkeypatch):
    position = FuturesPosition(
        symbol=SYMBOL, side="long", contracts=QUANTITY, quantity=QUANTITY,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )
    exchange, bridge = _setup(tmp_path, monkeypatch, position=position)
    exchange.orders[ORDER_ID] = _entry_order()
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        legs=[_leg("stop-loss", "stop-algo", "placed", STOP),
              _leg("take-profit", "target-algo", "placed", TARGET)],
    )
    exchange.orders["stop-algo"] = _conditional("stop-algo", "stop-loss")
    exchange.orders["target-algo"] = _conditional("target-algo", "take-profit")
    path = Path(bridge.config["exec_log_path"])
    path.write_text(
        json.dumps(_audit_event()) + "\n"
        + json.dumps(_audit_event(stop=8.5)) + "\n",
        encoding="utf-8",
    )

    assert _result(bridge) == "open_position_provenance_ambiguous"
    assert job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}") is None
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_malformed_audit_fails_closed_for_open_position(tmp_path, monkeypatch):
    position = FuturesPosition(
        symbol=SYMBOL, side="long", contracts=QUANTITY, quantity=QUANTITY,
        entry_price=ENTRY_PRICE, mark_price=ENTRY_PRICE,
    )
    exchange, bridge = _setup(tmp_path, monkeypatch, position=position)
    exchange.orders[ORDER_ID] = _entry_order()
    _seed_task(
        BRACKET_ID,
        entry_order_id=ORDER_ID,
        legs=[_leg("stop-loss", "stop-algo", "placed", STOP),
              _leg("take-profit", "target-algo", "placed", TARGET)],
    )
    Path(bridge.config["exec_log_path"]).write_text("not-json\n", encoding="utf-8")

    assert _result(bridge) == "open_position_provenance_ambiguous"
    assert job_store.load_position_state(f"{SYMBOL}:buy:{ORDER_ID}") is None


def test_no_lock_or_foreign_venue_cannot_change_intent(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)

    assert reconcile_entry(bridge, INTENT_ID) == "lock_required"
    task = job_store.load_bracket_recovery(INTENT_ID)
    task["venue_key"] = "binance|https://fapi.binance.com|False"
    monkeypatch.setattr(job_store, "load_bracket_recovery", lambda task_id: task)
    assert _result(bridge) == "venue_mismatch"
    assert task["status"] == "submission_uncertain"


def test_client_identity_change_during_read_prevents_commit(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    exchange.orders[ORDER_ID] = _entry_order()
    _add_terminal_bracket(exchange)
    calls = 0

    def identity_changes(current, expected):
        nonlocal calls
        calls += 1
        return futures_identity.IdentityComparison.MATCH if calls == 1 else futures_identity.IdentityComparison.MISMATCH

    monkeypatch.setattr(futures_identity, "compare_futures_account_identity", identity_changes)

    assert _result(bridge) == "account_identity_mismatch"
    assert job_store.load_bracket_recovery(INTENT_ID)["status"] == "submission_uncertain"


def test_unknown_entry_task_is_not_inferred_from_symbol(tmp_path, monkeypatch):
    exchange, bridge = _setup(tmp_path, monkeypatch)
    assert _result(bridge, "entry-intent:unknown-symbol:client") == "task_not_found"
    assert exchange.calls == []
