"""Operator-authorized legacy futures takeover: bind-only, fail-closed."""

import json
from datetime import UTC, datetime

import pytest

from server import db, job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _recovery_db as _recovery_db,
    bind_identity,
)
from tradingagents.execution import futures_takeover
from tradingagents.execution.exchange_client import venue_key

pytestmark = pytest.mark.unit

SYMBOL = "BTC/USDT:USDT"
TASK_ID = "bracket:BTC/USDT:USDT:entry-1"
POSITION_KEY = "BTC/USDT:USDT:buy:entry-1"
NOW = datetime.now(UTC).isoformat()


class _TakeoverBridge:
    """Minimal binance demo bridge: cached client, no venue mutation surface."""

    exchange_id = "binance"

    def __init__(self, exchange):
        self._exchange = exchange
        self.config = {
            "exec_base_url": "https://demo-fapi.binance.com",
            "exec_sandbox": False,
        }

    def client(self):
        return self._exchange


class _MalformedRiskExchange(FakeDerivExchange):
    """Position-risk row the venue mapper refuses (margin asset not the settle)."""

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.calls.append(("fapiPrivateV3GetPositionRisk", params))
        return [{
            "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.005000",
            "entryPrice": "50000.00", "markPrice": "50000.00", "marginAsset": "BTC",
        }]


def _algo_order(order_id, *, algo_status="NEW", name="stop-loss", trigger, quantity=0.005):
    order_type = "STOP_MARKET" if name == "stop-loss" else "TAKE_PROFIT_MARKET"
    return {
        "id": order_id, "symbol": SYMBOL, "side": "sell", "amount": quantity,
        "type": "market", "status": "open",
        "info": {
            "algoId": order_id, "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": algo_status, "orderType": order_type,
            "triggerPrice": str(trigger), "quantity": str(quantity),
            "reduceOnly": True, "workingType": "MARK_PRICE",
        },
    }


def _leg(name, order_id, *, status="placed", trigger):
    param = "stopLossPrice" if name == "stop-loss" else "takeProfitPrice"
    return {"name": name, "params": {param: trigger}, "status": status, "order_id": order_id}


def _seed_state(position_key, *, task_id=None, entry=50000.0, quantity=0.005,
                side="long", entry_order_id="entry-x"):
    assert job_store.persist_futures_entry_state(
        position_key, side=side, entry=entry, quantity=quantity,
        stop=48000.0, targets=[55000.0], mark=50000.0, order_id=entry_order_id,
    )
    state = job_store.load_position_state(position_key)
    payload = dict(state)
    if task_id is not None:
        payload["bracket_task_id"] = task_id
    assert job_store.save_position_state(position_key, state["revision"], payload)


def _seed_task(task_id, *, entry_order_id="entry-x", quantity=0.005, legs=None,
               venue_key_value=None, identity=None):
    assert job_store.create_bracket_recovery(
        task_id, symbol=SYMBOL, entry_order_id=entry_order_id, close_side="sell",
        quantity=quantity, legs=legs if legs is not None else [], now=NOW,
        venue_key=venue_key_value, identity=identity,
    )


def _task_identity(task_id):
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM bracket_recovery_tasks WHERE task_id = %s", (task_id,)
        ).fetchone()
    return (row["account_identity_exchange"], row["account_identity_profile"],
            row["account_identity_type"], row["account_identity_hash"])


def _state_identity(position_key):
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM position_states WHERE position_key = %s", (position_key,)
        ).fetchone()
    return (row["account_identity_exchange"], row["account_identity_profile"],
            row["account_identity_type"], row["account_identity_hash"])


def _risk_calls(exchange):
    return [call for call in exchange.calls if call[0] == "fapiPrivateV3GetPositionRisk"]


def _bound_identity():
    return (VERIFIED_IDENTITY.exchange, VERIFIED_IDENTITY.profile,
            VERIFIED_IDENTITY.account_type, VERIFIED_IDENTITY.identity_hash)


# --- happy path ---------------------------------------------------------------


def test_takeover_binds_both_rows_and_writes_the_audit_event(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    exchange.orders_by_id["leg-1"] = _algo_order("leg-1", name="stop-loss", trigger=48000.0)
    exchange.orders_by_id["leg-2"] = _algo_order("leg-2", name="take-profit", trigger=55000.0)
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[
        _leg("stop-loss", "leg-1", trigger=48000.0),
        _leg("take-profit", "leg-2", status="uncertain", trigger=55000.0),
    ], venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    config = {"exec_log_path": str(tmp_path / "audit.jsonl")}

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config=config,
    )

    assert reports == [{
        "position_key": POSITION_KEY, "status": "bound", "symbol": SYMBOL,
        "side": "long", "contracts": 0.005, "entry": 50000.0,
        "legs_verified": 2, "task_id": TASK_ID,
    }]
    assert _task_identity(TASK_ID) == _bound_identity()
    assert _state_identity(POSITION_KEY) == _bound_identity()
    assert ("fetch_order", "leg-1", SYMBOL, {"trigger": True}) in exchange.calls
    assert ("fetch_order", "leg-2", SYMBOL, {"trigger": True}) in exchange.calls
    events = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(events) == 1
    assert set(events[0]) == {"event", "timestamp", "symbol", "task_id", "legs_verified"}
    assert events[0]["event"] == "legacy_takeover_bound"
    assert events[0]["symbol"] == SYMBOL
    assert events[0]["task_id"] == TASK_ID
    assert events[0]["legs_verified"] == 2


def test_partial_venue_position_still_binds(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.003
    exchange.orders_by_id["leg-1"] = _algo_order("leg-1", trigger=48000.0)
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[_leg("stop-loss", "leg-1", trigger=48000.0)],
               venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    config = {"exec_log_path": str(tmp_path / "audit.jsonl")}

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config=config,
    )

    assert [report["status"] for report in reports] == ["bound"]
    assert reports[0]["contracts"] == 0.003
    assert _task_identity(TASK_ID)[3] == VERIFIED_IDENTITY.identity_hash
    assert _state_identity(POSITION_KEY)[3] == VERIFIED_IDENTITY.identity_hash


# --- global gate and once-per-run refuses -------------------------------------


def test_mutation_gate_reports_gated_without_any_venue_or_identity_read(monkeypatch):
    exchange = FakeDerivExchange()
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID)
    bound_key = "BTC/USDT:USDT:buy:entry-bound"
    job_store.persist_futures_entry_state(
        bound_key, side="long", entry=50000.0, quantity=0.005,
        stop=48000.0, targets=[55000.0], mark=50000.0, order_id="entry-bound",
        identity=VERIFIED_IDENTITY,
    )
    _seed_task("bracket:BTC/USDT:USDT:entry-bound", entry_order_id="entry-bound",
               identity=VERIFIED_IDENTITY)
    identity_reads = []

    def counting_reader(bridge_arg):
        identity_reads.append(bridge_arg)
        return VERIFIED_IDENTITY

    bind_identity(monkeypatch, counting_reader)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=False, config={},
    )

    assert _risk_calls(exchange) == []
    assert not [call for call in exchange.calls if call[0] == "fetch_order"]
    assert identity_reads == []
    statuses = {report["position_key"]: report["status"] for report in reports}
    assert statuses[POSITION_KEY] == "mutation_gated"
    assert statuses[bound_key] == "already_bound"
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_identity_unavailable_refuses_every_state_with_zero_writes(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID)
    bind_identity(monkeypatch, None)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "identity_unavailable"}]
    assert _risk_calls(exchange) == []
    assert _task_identity(TASK_ID) == (None, None, None, None)
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_malformed_venue_row_refuses_every_state(monkeypatch):
    exchange = _MalformedRiskExchange()
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID)
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "scope_incomplete"}]
    assert _risk_calls(exchange)
    assert _task_identity(TASK_ID) == (None, None, None, None)
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_venue_read_failure_refuses_every_state(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = None  # the fake raises ExchangeError on the risk read
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID)
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "venue_read_failed"}]
    assert _risk_calls(exchange)
    assert _task_identity(TASK_ID) == (None, None, None, None)
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_already_bound_rows_are_skipped_without_venue_reads(monkeypatch):
    exchange = FakeDerivExchange()
    bridge = _TakeoverBridge(exchange)
    job_store.persist_futures_entry_state(
        POSITION_KEY, side="long", entry=50000.0, quantity=0.005,
        stop=48000.0, targets=[55000.0], mark=50000.0, order_id="entry-1",
        identity=VERIFIED_IDENTITY,
    )
    _seed_task(TASK_ID, identity=VERIFIED_IDENTITY)
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "already_bound"}]
    assert _risk_calls(exchange) == []


# --- per-state refuses ---------------------------------------------------------


def test_entry_price_mismatch_refuses_the_bind(monkeypatch):
    exchange = FakeDerivExchange(price=49500.0)
    exchange.contracts = 0.005
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "position_unmatched"}]
    assert _task_identity(TASK_ID) == (None, None, None, None)
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_venue_contracts_larger_than_snapshot_refuse_the_bind(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.01
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "position_unmatched"}]
    assert _task_identity(TASK_ID) == (None, None, None, None)


def test_side_mismatch_refuses_the_bind(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005  # venue shows the long only
    bridge = _TakeoverBridge(exchange)
    short_key = "BTC/USDT:USDT:sell:entry-s"
    _seed_state(short_key, task_id=TASK_ID, side="short")
    _seed_task(TASK_ID, entry_order_id="entry-s", venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": short_key, "status": "position_unmatched"}]


def test_missing_bracket_task_refuses_the_bind(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    bridge = _TakeoverBridge(exchange)
    unlinked_key = "BTC/USDT:USDT:buy:entry-nolink"
    ghost_key = "BTC/USDT:USDT:buy:entry-ghost"
    _seed_state(unlinked_key)
    _seed_state(ghost_key, task_id="bracket:BTC/USDT:USDT:ghost")
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    statuses = {report["position_key"]: report["status"] for report in reports}
    assert statuses[unlinked_key] == "missing_bracket_task"
    assert statuses[ghost_key] == "missing_bracket_task"


def test_venue_mismatch_refuses_the_bind(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, venue_key_value="binance|https://fapi.binance.com|False")
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "venue_mismatch"}]
    assert _task_identity(TASK_ID) == (None, None, None, None)


def test_leg_order_not_found_refuses_the_bind(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[_leg("stop-loss", "leg-gone", trigger=48000.0)],
               venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{"position_key": POSITION_KEY, "status": "leg_unverified"}]
    assert ("fetch_order", "leg-gone", SYMBOL, {"trigger": True}) in exchange.calls
    assert _task_identity(TASK_ID) == (None, None, None, None)
    assert _state_identity(POSITION_KEY) == (None, None, None, None)


def test_leg_no_longer_placed_refuses_with_its_state(monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    exchange.orders_by_id["leg-1"] = _algo_order("leg-1", algo_status="EXPIRED", trigger=48000.0)
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[_leg("stop-loss", "leg-1", trigger=48000.0)],
               venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config={},
    )

    assert reports == [{
        "position_key": POSITION_KEY, "status": "leg_unverified",
        "leg_state": "cancelled",
    }]
    assert _task_identity(TASK_ID) == (None, None, None, None)


def test_terminal_legs_are_skipped_when_everything_else_matches(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    exchange.orders_by_id["leg-1"] = _algo_order("leg-1", trigger=48000.0)
    bridge = _TakeoverBridge(exchange)
    _seed_state(POSITION_KEY, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[
        _leg("stop-loss", "leg-1", trigger=48000.0),
        _leg("take-profit", "leg-filled", status="filled", trigger=55000.0),
    ], venue_key_value=venue_key(bridge))
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    config = {"exec_log_path": str(tmp_path / "audit.jsonl")}

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config=config,
    )

    assert [report["status"] for report in reports] == ["bound"]
    assert reports[0]["legs_verified"] == 1
    assert not [call for call in exchange.calls if call[1] == "leg-filled"]


# --- isolation -----------------------------------------------------------------


def test_one_corrupt_state_does_not_abort_the_others(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    exchange.contracts = 0.005
    exchange.orders_by_id["leg-1"] = _algo_order("leg-1", trigger=48000.0)
    bridge = _TakeoverBridge(exchange)
    good_key = POSITION_KEY
    corrupt_key = "BTC/USDT:USDT:buy:entry-corrupt"
    unparseable_key = "weird-key"
    _seed_state(good_key, task_id=TASK_ID)
    _seed_task(TASK_ID, legs=[_leg("stop-loss", "leg-1", trigger=48000.0)],
               venue_key_value=venue_key(bridge))
    _seed_state(corrupt_key)
    state = job_store.load_position_state(corrupt_key)
    state["snapshot"]["entry"] = None
    assert job_store.save_position_state(corrupt_key, state["revision"], state)
    _seed_state(unparseable_key)
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    config = {"exec_log_path": str(tmp_path / "audit.jsonl")}

    reports = futures_takeover.take_over_legacy_positions(
        bridge, allow_mutation=True, config=config,
    )

    statuses = {report["position_key"]: report["status"] for report in reports}
    assert statuses[good_key] == "bound"
    assert statuses[corrupt_key] == "position_unmatched"
    assert statuses[unparseable_key] == "error:ValueError"
    assert _state_identity(good_key)[3] == VERIFIED_IDENTITY.identity_hash
