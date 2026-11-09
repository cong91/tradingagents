from datetime import UTC, datetime, timedelta

import pytest

from server import job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    bind_identity,
)
from tradingagents.execution.futures_protection import manage_active_positions


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch, postgres_schema):
    return postgres_schema
def test_position_state_can_be_linked_to_bracket_and_listed_as_active():
    position_key = "BTC/USDT:USDT:buy:entry-1"
    assert job_store.persist_futures_entry_state(
        position_key,
        side="long",
        entry=100.0,
        quantity=1.0,
        stop=90.0,
        targets=[130.0],
        mark=100.0,
        order_id="entry-1",
    )

    state = job_store.load_position_state(position_key)
    state["bracket_task_id"] = "bracket:BTC/USDT:USDT:entry-1"
    state["venue_key"] = "binance|demo|true"
    assert job_store.save_position_state(position_key, state["revision"], state)

    active = job_store.active_position_states()
    assert active == [{
        "position_key": position_key,
        **job_store.load_position_state(position_key),
    }]


def test_persisted_entry_state_records_opened_at():
    position_key = "BTC/USDT:USDT:buy:entry-3"
    before = datetime.now(UTC)
    assert job_store.persist_futures_entry_state(
        position_key,
        side="long",
        entry=100.0,
        quantity=1.0,
        stop=90.0,
        targets=[130.0],
        mark=100.0,
        order_id="entry-3",
    )

    opened_at = job_store.load_position_state(position_key)["snapshot"]["opened_at"]
    parsed = datetime.fromisoformat(opened_at)
    assert parsed.tzinfo is not None
    assert before <= parsed <= datetime.now(UTC)


def test_position_state_compare_and_swap_rejects_stale_revision():
    position_key = "BTC/USDT:USDT:buy:entry-2"
    assert job_store.persist_futures_entry_state(
        position_key,
        side="long",
        entry=100.0,
        quantity=1.0,
        stop=90.0,
        targets=[130.0],
        mark=100.0,
        order_id="entry-2",
    )

    state = job_store.load_position_state(position_key)
    state["snapshot"]["stop"] = 95.0
    assert job_store.save_position_state(position_key, state["revision"], state)
    assert not job_store.save_position_state(position_key, state["revision"], state)


class _ProtectionExchange:
    def __init__(self, *, positions, mark_price=None, position_mode=False):
        self.positions = positions
        self.mark_price = mark_price
        self.position_mode = position_mode
        self.calls = []
        self.orders = {}
        self.cancel_error = None
        # The account-scope surface the raw V3 position read validates.
        self.urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        self.options = {"enableDemoTrading": True, "sandboxMode": False}
        self.verbose = False
        self.markets_by_id = {"TIAUSDT": {
            "id": "TIAUSDT", "symbol": "TIA/USDT:USDT", "contract": True,
            "linear": True, "inverse": False, "quote": "USDT", "settle": "USDT",
            "settleId": "USDT",
        }}

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.calls.append(("raw_positions", params))
        rows = []
        for position in self.positions:
            if not isinstance(position, dict):
                rows.append(position)
                continue
            contracts = position.get("contracts")
            side = str(position.get("side") or "").lower()
            row = {
                "symbol": str(position.get("symbol", "")).split(":", 1)[0].replace("/", ""),
                "positionSide": "BOTH", "marginAsset": "USDT",
            }
            if contracts is not None:
                signed = float(contracts) * (-1.0 if side == "short" else 1.0)
                row["positionAmt"] = f"{signed:.6f}"
            for key in ("entryPrice", "markPrice"):
                if position.get(key) is not None:
                    row[key] = str(position[key])
            rows.append(row)
        return rows

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("positions", symbols, params))
        return self.positions

    def fetch_position_mode(self, symbol=None, params=None):
        self.calls.append(("position_mode", symbol, params))
        return {"hedged": self.position_mode, "info": {"dualSidePosition": self.position_mode}}

    def fetch_mark_price(self, symbol, params=None):
        self.calls.append(("mark", symbol, params))
        if self.mark_price is not None:
            return self.mark_price
        # Fresh by construction: a hardcoded timestamp would turn every
        # default-mark test into a stale-mark test as the clock advances.
        return {
            "markPrice": "136.0",
            "timestamp": int(datetime.now(UTC).timestamp() * 1000),
        }

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", symbol, side, amount, params))
        if "stopLossPrice" not in params and "takeProfitPrice" not in params:
            # A plain reduce-only market close (no conditional algo status):
            # the venue's confirmed "closed" status proves the fill.
            return {"id": "close-1", "status": "closed"}
        order_id = str(len(self.orders) + 1)
        key = "stopLossPrice" if "stopLossPrice" in params else "takeProfitPrice"
        order_type = "STOP_MARKET" if key == "stopLossPrice" else "TAKE_PROFIT_MARKET"
        self.orders[order_id] = {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "type": "market",
            "status": "open",
            "info": {
                "algoId": order_id,
                "symbol": "TIAUSDT",
                "side": side.upper(),
                "algoStatus": "NEW",
                "orderType": order_type,
                "triggerPrice": str(params[key]),
                "quantity": str(amount),
                "reduceOnly": True,
                "workingType": params.get("workingType"),
                "clientAlgoId": params.get("clientOrderId"),
            },
        }
        return {"id": order_id}

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("open", symbol, params))
        return [order for order in self.orders.values() if order["status"] == "open"]

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("fetch_order", order_id, symbol, params))
        return self.orders[order_id]

    def cancel_order(self, order_id, symbol, params=None):
        self.calls.append(("cancel", order_id, symbol, params))
        if self.cancel_error is not None:
            raise self.cancel_error
        self.orders[order_id]["status"] = "canceled"
        self.orders[order_id]["info"]["algoStatus"] = "CANCELED"
        return {"id": order_id, "info": {"algoId": order_id, "code": 200, "msg": "success"}}


class _ProtectionBridge:
    exchange_id = "binance"

    def __init__(self, exchange):
        self.config = {"exec_base_url": "https://demo-api.binance.com", "exec_sandbox": True}
        self.exchange = exchange
        self.audit = []

    def client(self):
        return self.exchange

    def _redact(self, message):
        return message

    def audit_plan(self, order, **kwargs):
        self.audit.append(kwargs)


def _linked_position(*, side="long", entry=100.0, stop=90.0, target=130.0):
    position_key = "TIA/USDT:USDT:buy:entry-1" if side == "long" else "TIA/USDT:USDT:sell:entry-1"
    task_id = "bracket:TIA/USDT:USDT:entry-1"
    job_store.persist_futures_entry_state(
        position_key,
        side=side,
        entry=entry,
        quantity=1.0,
        stop=stop,
        targets=[target],
        mark=entry,
        order_id="entry-1",
        identity=VERIFIED_IDENTITY,
    )
    state = job_store.load_position_state(position_key)
    state["bracket_task_id"] = task_id
    state["venue_key"] = "binance|https://demo-api.binance.com|true"
    assert job_store.save_position_state(position_key, state["revision"], state)
    job_store.create_bracket_recovery(
        task_id,
        symbol="TIA/USDT:USDT",
        entry_order_id="entry-1",
        close_side="sell" if side == "long" else "buy",
        quantity=1.0,
        legs=[
            {"name": "stop-loss", "params": {"stopLossPrice": stop}, "status": "placed", "order_id": "old-stop"},
            {"name": "take-profit", "params": {"takeProfitPrice": target}, "status": "placed", "order_id": "old-target"},
        ],
        now="2026-10-19T00:00:00+00:00",
        venue_key="binance|https://demo-api.binance.com|True",
        identity=VERIFIED_IDENTITY,
    )
    return position_key, task_id


def _resting_leg_orders():
    """The two conditional legs a bracket places, as the venue lists them."""
    return {
        "old-stop": {
            "id": "old-stop", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-stop", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "STOP_MARKET",
                     "triggerPrice": "90.0", "quantity": "1.0", "reduceOnly": True,
                     "workingType": "MARK_PRICE"},
        },
        "old-target": {
            "id": "old-target", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-target", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                     "triggerPrice": "130.0", "quantity": "1.0", "reduceOnly": True,
                     "workingType": "MARK_PRICE"},
        },
    }


def _set_opened_hours_ago(position_key: str, hours: float) -> None:
    state = job_store.load_position_state(position_key)
    state["snapshot"]["opened_at"] = (
        datetime.now(UTC) - timedelta(hours=hours)
    ).isoformat()
    assert job_store.save_position_state(position_key, state["revision"], state)


def test_live_position_management_uses_venue_entry_quantity_and_mark(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 2.0,
        "entryPrice": 101.0, "markPrice": 136.0,
    }])
    exchange.orders = {
        "old-stop": {
            "id": "old-stop", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-stop", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "STOP_MARKET",
                     "triggerPrice": "90.0", "quantity": "1.0", "reduceOnly": True,
                     "workingType": "MARK_PRICE"},
        },
        "old-target": {
            "id": "old-target", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-target", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                     "triggerPrice": "130.0", "quantity": "1.0", "reduceOnly": True,
                     "workingType": "MARK_PRICE"},
        },
    }
    bridge = _ProtectionBridge(exchange)

    results = manage_active_positions(
        bridge, allow_mutation=True, config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert results == [{"position_key": position_key, "status": "updated"}]
    state = job_store.load_position_state(position_key)
    assert state["snapshot"]["entry"] == pytest.approx(101.0)
    assert state["snapshot"]["quantity"] == pytest.approx(2.0)
    assert state["snapshot"]["mark"] == pytest.approx(136.0)
    assert state["snapshot"]["stop"] == pytest.approx(125.0)
    assert state["snapshot"]["current_target"] == pytest.approx(141.0)
    created = [call for call in exchange.calls if call[0] == "create"]
    # Binance conditional legs trigger on the mark price, so a wick that never
    # reaches the last-traded price still closes the position.
    assert [(call[0], call[1], call[2], call[3]) for call in created] == [
        ("create", "TIA/USDT:USDT", "sell", 2.0),
        ("create", "TIA/USDT:USDT", "sell", 2.0),
    ]
    assert [call[4].get("stopLossPrice", call[4].get("takeProfitPrice")) for call in created] == [125.0, 141.0]
    assert all(call[4]["reduceOnly"] and call[4]["workingType"] == "MARK_PRICE" for call in created)
    assert all(call[4]["clientOrderId"].startswith("br-") for call in created)
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["retired_ids"] == []
    assert task["legs"][1]["retired_ids"] == []


@pytest.mark.parametrize("rows", [
    [],
    [{"symbol": "TIA/USDT:USDT", "side": "long"}],
    [{"symbol": "TIA/USDT:USDT", "side": "long", "contracts": "NaN"}],
    [{"symbol": "TIA/USDT:USDT", "side": "long", "contracts": 0}],
    [None],
])
def test_unproven_flat_never_cancels_or_deletes(rows):
    position_key, task_id = _linked_position()
    exchange = _ProtectionExchange(positions=rows)
    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )
    assert result == [{"position_key": position_key, "status": "position_unknown"}]
    assert job_store.load_position_state(position_key) is not None
    assert job_store.load_bracket_recovery(task_id)["status"] == "pending"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_emergency_bracket_state_is_manual_only(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    job_store.update_bracket_recovery(
        task_id, status="emergency_close_failed", placed_ids=[], attempts=1,
        last_error="manual test state", now="2026-10-20T00:00:00+00:00",
    )
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 100.0,
    }])

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{
        "position_key": position_key,
        "status": "manual_reconciliation_required",
    }]
    assert job_store.load_bracket_recovery(task_id)["status"] == "emergency_close_failed"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_submission_uncertain_uses_verified_venue_reconciliation(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    task = job_store.load_bracket_recovery(task_id)
    state = job_store.load_position_state(position_key)
    state["venue_key"] = task["venue_key"]
    assert job_store.save_position_state(position_key, state["revision"], state)
    job_store.update_bracket_recovery(
        task_id, status="submission_uncertain", placed_ids=["old-stop", "old-target"],
        attempts=0, last_error="test uncertainty", now="2026-10-20T00:00:00+00:00",
        legs=task["legs"],
    )
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 100.0,
    }])
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "unchanged"}]
    assert job_store.load_bracket_recovery(task_id)["status"] == "monitoring"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_malformed_position_key_does_not_prevent_other_states():
    position_key, _task_id = _linked_position()
    job_store.persist_futures_entry_state(
        "malformed", side="long", entry=100, quantity=1,
        stop=90, targets=[130], mark=100, order_id="entry-bad",
    )
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1,
        "entryPrice": 100, "markPrice": 135,
    }])
    results = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )
    statuses = {item["position_key"]: item["status"] for item in results}
    assert statuses[position_key] in {"account_identity_unknown", "leg_state_unknown"}
    assert statuses["malformed"].startswith("error:ValueError:")
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_unknown_account_does_not_change_open_position_protection():
    position_key, task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 135.0,
    }])
    state = job_store.load_position_state(position_key)
    task = job_store.load_bracket_recovery(task_id)
    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )
    assert result == [{"position_key": position_key, "status": "account_identity_unknown"}]
    assert job_store.load_position_state(position_key) == state
    assert job_store.load_bracket_recovery(task_id) == task
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_closed_mutation_gate_does_not_create_or_cancel_orders():
    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 135.0,
    }])

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=False,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "mutation_gated"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_missing_mark_does_not_mutate_protection():
    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": None,
    }])

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "invalid_position_snapshot"}]
    assert job_store.load_position_state(position_key)["snapshot"]["stop"] == 90.0
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_flat_position_cleans_legs_before_deleting_durable_state():
    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[])
    exchange.orders = {
        "old-stop": {
            "id": "old-stop", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-stop", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "STOP_MARKET",
                     "triggerPrice": "90.0", "quantity": "1.0", "reduceOnly": True},
        },
        "old-target": {
            "id": "old-target", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-target", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                     "triggerPrice": "130.0", "quantity": "1.0", "reduceOnly": True},
        },
    }

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "position_unknown"}]
    assert job_store.load_position_state(position_key) is not None
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_flat_position_retains_state_when_conditional_cleanup_is_uncertain():
    import ccxt

    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[])
    exchange.orders = {
        "old-stop": {
            "id": "old-stop", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-stop", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "STOP_MARKET",
                     "triggerPrice": "90.0", "quantity": "1.0", "reduceOnly": True},
        },
        "old-target": {
            "id": "old-target", "symbol": "TIA/USDT:USDT", "side": "sell",
            "amount": 1.0, "type": "market", "status": "open",
            "info": {"algoId": "old-target", "symbol": "TIAUSDT", "side": "SELL",
                     "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET",
                     "triggerPrice": "130.0", "quantity": "1.0", "reduceOnly": True},
        },
    }
    exchange.cancel_error = ccxt.NetworkError("venue unavailable")

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "position_unknown"}]
    assert job_store.load_position_state(position_key) is not None
    assert not [call for call in exchange.calls if call[0] == "cancel"]


def test_position_monitor_lease_has_one_owner_and_recovers_after_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(job_store.time, "time", lambda: now[0])
    position_key = "BTC/USDT:USDT:buy:entry-lease"

    first_owner = job_store.acquire_position_monitor_lease(position_key, lease_seconds=10)
    assert first_owner is not None
    assert job_store.acquire_position_monitor_lease(position_key, lease_seconds=10) is None

    now[0] = 111.0
    second_owner = job_store.acquire_position_monitor_lease(position_key, lease_seconds=10)
    assert second_owner is not None
    assert second_owner != first_owner
    assert not job_store.release_position_monitor_lease(position_key, first_owner)
    assert job_store.release_position_monitor_lease(position_key, second_owner)


def test_multiple_entry_states_for_one_net_position_are_not_managed():
    _linked_position()
    second_key = "TIA/USDT:USDT:buy:entry-2"
    assert job_store.persist_futures_entry_state(
        second_key, side="long", entry=105.0, quantity=1.0,
        stop=95.0, targets=[135.0], mark=105.0, order_id="entry-2",
    )
    second = job_store.load_position_state(second_key)
    second["bracket_task_id"] = "bracket:TIA/USDT:USDT:entry-2"
    assert job_store.save_position_state(second_key, second["revision"], second)
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 2.0,
        "entryPrice": 103.0, "markPrice": 140.0,
    }])

    results = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert {item["status"] for item in results} == {"ambiguous_net_position"}
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_legacy_position_state_backfills_only_an_exact_bracket_task():
    position_key = "TIA/USDT:USDT:buy:legacy-entry"
    assert job_store.persist_futures_entry_state(
        position_key, side="long", entry=100.0, quantity=1.0,
        stop=90.0, targets=[130.0], mark=100.0, order_id="legacy-entry",
    )
    job_store.create_bracket_recovery(
        "bracket:TIA/USDT:USDT:legacy-entry",
        symbol="TIA/USDT:USDT", entry_order_id="legacy-entry",
        close_side="sell", quantity=1.0,
        legs=[
            {"name": "stop-loss", "params": {"stopLossPrice": 90.0}, "status": "placed", "order_id": "old-stop"},
            {"name": "take-profit", "params": {"takeProfitPrice": 130.0}, "status": "placed", "order_id": "old-target"},
        ],
        now="2026-10-20T00:00:00+00:00",
        venue_key="binance|https://demo-api.binance.com|True",
    )
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 100.0,
    }])

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "missing_bracket_link"}]
    assert not job_store.load_position_state(position_key).get("bracket_task_id")


    _position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 135.0,
    }])
    bridge = _ProtectionBridge(exchange)
    bridge.exchange_id = "bybit"

    result = manage_active_positions(
        bridge, allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result[0]["status"] == "venue_unsupported"
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_hedge_mode_blocks_position_mutations():
    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 135.0,
        }],
        position_mode=True,
    )

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "hedge_mode_unsupported"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_stale_mark_price_blocks_position_mutations():
    position_key, _task_id = _linked_position()
    exchange = _ProtectionExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 135.0,
        }],
        mark_price={"markPrice": "135.0", "timestamp": 1},
    )

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={
            "risk_derivatives_profit_protection_enabled": True,
            "risk_derivatives_mark_max_age_seconds": 120,
        },
    )

    assert result == [{"position_key": position_key, "status": "mark_stale"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_missing_opened_at_is_backfilled_without_closing():
    position_key, _task_id = _linked_position()
    state = job_store.load_position_state(position_key)
    state["snapshot"].pop("opened_at")
    assert job_store.save_position_state(position_key, state["revision"], state)
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 105.0,
    }])
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={
            "risk_derivatives_profit_protection_enabled": True,
            "risk_derivatives_max_hold_hours": 0.0001,
        },
    )

    assert result == [{"position_key": position_key, "status": "opened_at_unknown"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert "opened_at" not in snapshot
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_time_stop_past_the_deadline_below_minimum_r_closes_and_deletes_state(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 105.0,
    }])
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(
        bridge, allow_mutation=True,
        config={
            "risk_derivatives_profit_protection_enabled": True,
            "risk_derivatives_max_hold_hours": 72.0,
            "risk_derivatives_time_stop_min_r": 1.0,
        },
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    closes = [call for call in exchange.calls if call[0] == "create"]
    assert len(closes) == 1
    _kind, _symbol, close_side, amount, params = closes[0]
    assert close_side == "sell"
    assert amount == pytest.approx(1.0)
    assert params == {"reduceOnly": True, "clientOrderId": params["clientOrderId"]}
    assert params["clientOrderId"].startswith("timestop-")
    assert {call[1] for call in exchange.calls if call[0] == "cancel"} == {"old-stop", "old-target"}
    assert job_store.load_position_state(position_key) is None
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]


def test_time_stop_past_the_deadline_after_minimum_r_keeps_the_position(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 115.0,
    }])
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={
            "risk_derivatives_profit_protection_enabled": True,
            "risk_derivatives_max_hold_hours": 72.0,
            "risk_derivatives_time_stop_min_r": 1.0,
        },
    )

    assert result == [{"position_key": position_key, "status": "updated"}]
    assert not [call for call in exchange.calls if call[0] == "create" and str(call[4].get("clientOrderId", "")).startswith("timestop-")]
    assert job_store.load_position_state(position_key) is not None


class _FundingExchange(_ProtectionExchange):
    """Adds the funding endpoints the time stop consults at the deadline."""

    def __init__(self, *, positions, funding_rows=(), funding_rate=None, mark_price=None):
        super().__init__(positions=positions, mark_price=mark_price)
        self.funding_rows = funding_rows
        self.funding_rate = funding_rate
        self.funding_calls = []

    def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
        self.funding_calls.append(("funding_history", symbol, since))
        if isinstance(self.funding_rows, Exception):
            raise self.funding_rows
        return list(self.funding_rows)

    def fetch_funding_rate(self, symbol, params=None):
        self.funding_calls.append(("funding_rate", symbol, params))
        if isinstance(self.funding_rate, Exception):
            raise self.funding_rate
        return self.funding_rate


def _short_position_state():
    position_key, task_id = _linked_position(side="short", stop=110.0, target=70.0)
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _FundingExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "short", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 105.0,
        }],
    )
    return position_key, task_id, exchange


_TIME_STOP_CONFIG = {
    "risk_derivatives_profit_protection_enabled": True,
    "risk_derivatives_max_hold_hours": 72.0,
    "risk_derivatives_time_stop_min_r": 1.0,
}


def test_strong_funding_blocks_the_time_stop_and_records_the_reason(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    # Mark 105 on entry 100 puts the short at -0.5R; +30 USDT of funding
    # against a 10 USDT initial risk is +3.0R, enough to hold the position.
    exchange.funding_rows = [{"amount": 15.0}, {"amount": 15.0}]
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    assert result == [{
        "position_key": position_key,
        "status": "time_stop_blocked_by_funding",
    }]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["phase"] == "time_stop_blocked_by_funding"
    assert snapshot["cumulative_funding_r"] == pytest.approx(3.0)
    assert snapshot["round_trip_fee_r"] == pytest.approx(0.01)  # 0.001 * 100 / 10
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    assert bridge.audit == []


def test_earning_short_inside_the_settlement_window_postpones_the_time_stop(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    # +2 USDT of funding lifts the effective R only to -0.31, so the time stop
    # would close — but the next settlement pays this short and is 30m away.
    settlement_ms = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000)
    exchange.funding_rows = [{"amount": 2.0}]
    exchange.funding_rate = {"fundingRate": 0.0001, "nextFundingTimestamp": settlement_ms}
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    assert result == [{
        "position_key": position_key,
        "status": "time_stop_postponed_funding",
    }]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["phase"] == "time_stop_postponed_funding"
    assert datetime.fromisoformat(snapshot["observed_at"]).tzinfo is not None
    assert snapshot["cumulative_funding_r"] == pytest.approx(0.2)
    assert datetime.fromisoformat(snapshot["time_stop_postponed_for"]) == (
        datetime.fromtimestamp(settlement_ms / 1000, tz=UTC)
    )
    assert [event["action"] for event in bridge.audit] == ["time_stop_postponed"]

    second = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    assert second == [{
        "position_key": position_key,
        "status": "time_stop_postponed_funding",
    }]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert datetime.fromisoformat(snapshot["time_stop_postponed_for"]) == (
        datetime.fromtimestamp(settlement_ms / 1000, tz=UTC)
    )
    assert [event["action"] for event in bridge.audit] == ["time_stop_postponed"]


def test_a_second_settlement_inside_the_window_closes_instead_of_deferring(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    exchange.funding_rows = [{"amount": 2.0}]
    exchange.funding_rate = {
        "fundingRate": 0.0001,
        "nextFundingTimestamp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000),
    }
    bridge = _ProtectionBridge(exchange)

    first = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)
    assert first == [{"position_key": position_key, "status": "time_stop_postponed_funding"}]

    # The held-through settlement passes and the next one is still inside the
    # 90m window: holding again would defer the time stop one cycle at a time
    # forever, so the position closes instead.
    exchange.funding_rate = {
        "fundingRate": 0.0001,
        "nextFundingTimestamp": int((datetime.now(UTC) + timedelta(minutes=45)).timestamp() * 1000),
    }
    exchange.orders = _resting_leg_orders()

    second = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    assert second == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert job_store.load_position_state(position_key) is None


def test_strong_funding_blocks_the_time_stop_on_the_brackets_original_size(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    # A scale-out tranche already left: the venue reports half the original
    # contracts while the funding history accrued on the full size.
    exchange.positions[0]["contracts"] = 0.5
    exchange.funding_rows = [{"amount": 15.0}, {"amount": 15.0}]
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    assert result == [{
        "position_key": position_key,
        "status": "time_stop_blocked_by_funding",
    }]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    # 30 USDT over the original 10 USDT initial risk is 3.0R; dividing by the
    # shrunken 5 USDT remainder would claim 6.0R.
    assert snapshot["cumulative_funding_r"] == pytest.approx(3.0)


class _FundingDeadClient:
    """A client whose funding endpoints are unreachable (a dropped connection
    during the funding reads); every other surface proxies the venue."""

    def __init__(self, exchange):
        self._exchange = exchange

    def __getattr__(self, name):
        if name in {"fetch_funding_history", "fetch_funding_rate"}:
            raise RuntimeError("client unavailable")
        return getattr(self._exchange, name)


class _FlakyClientBridge(_ProtectionBridge):
    """A bridge whose client read hiccups exactly at the funding reads, as a
    single dropped connection would: the gates and the close still work, only
    the funding adjustments fall back. The wrapped client stays the SAME
    instance for the whole pass, like a real bridge's cached client."""

    def __init__(self, exchange):
        super().__init__(exchange)
        self._wrapped = _FundingDeadClient(exchange)

    def client(self):
        return self._wrapped


def test_unavailable_venue_client_notes_the_funding_fallback(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 105.0,
    }])
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _FlakyClientBridge(exchange), allow_mutation=True, config=_TIME_STOP_CONFIG,
    )

    assert result == [{
        "position_key": position_key,
        "status": "time_stop_closed",
        "funding_note": "funding_history_unavailable",
    }]
    assert job_store.load_position_state(position_key) is None


def test_paying_long_closes_at_the_deadline_despite_the_upcoming_settlement(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _FundingExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 105.0,
        }],
        funding_rows=[{"amount": -2.0}],
        funding_rate={
            "fundingRate": 0.0001,
            "nextFundingTimestamp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000),
        },
    )
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(bridge, allow_mutation=True, config=_TIME_STOP_CONFIG)

    # A long pays a positive funding rate: the close must not wait for it.
    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert len([call for call in exchange.calls if call[0] == "create"]) == 1
    assert job_store.load_position_state(position_key) is None
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]


def test_failed_funding_history_treats_funding_as_zero_and_notes_it(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _FundingExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 105.0,
        }],
        funding_rows=RuntimeError("venue down"),
    )
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_TIME_STOP_CONFIG,
    )

    assert result == [{
        "position_key": position_key,
        "status": "time_stop_closed",
        "funding_note": "funding_history_unavailable",
    }]
    assert job_store.load_position_state(position_key) is None


def test_disabled_postpone_window_closes_without_reading_the_schedule(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    exchange.funding_rows = [{"amount": 2.0}]
    exchange.funding_rate = {
        "fundingRate": 0.0001,
        "nextFundingTimestamp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000),
    }
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={**_TIME_STOP_CONFIG, "risk_derivatives_time_stop_postpone_minutes": 0.0},
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert not [call for call in exchange.funding_calls if call[0] == "funding_rate"]
    assert job_store.load_position_state(position_key) is None


def test_funding_is_only_consulted_once_the_raw_boundary_trips(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    # Mark 115 on entry 100 is +1.5R: above the time-stop minimum before any
    # funding is considered, so the venue must not be asked.
    exchange = _FundingExchange(
        positions=[{
            "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
            "entryPrice": 100.0, "markPrice": 115.0,
        }],
        funding_rows=[{"amount": 100.0}],
        funding_rate={
            "fundingRate": 0.0001,
            "nextFundingTimestamp": int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000),
        },
    )
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_TIME_STOP_CONFIG,
    )

    assert result == [{"position_key": position_key, "status": "updated"}]
    assert exchange.funding_calls == []


# ---------------------------------------------------------------------------
# Analyzed time stop: a past-deadline position below its minimum R asks a
# position review for a HOLD (deadline extended one interval) or CLOSE verdict
# instead of closing mechanically.
# ---------------------------------------------------------------------------

_REVIEW_CONFIG = {
    **_TIME_STOP_CONFIG,
    "risk_derivatives_time_stop_review_enabled": True,
    "risk_derivatives_time_stop_review_interval_hours": 24.0,
    "risk_derivatives_time_stop_review_grace_hours": 6.0,
}


def _set_review_fields(position_key: str, **fields) -> None:
    state = job_store.load_position_state(position_key)
    state["snapshot"].update(fields)
    assert job_store.save_position_state(position_key, state["revision"], state)


def _recording_hook():
    requests: list[tuple[str, dict]] = []

    def hook(position_key: str, payload: dict) -> None:
        requests.append((position_key, payload))

    return hook, requests


def _stale_review_exchange():
    exchange = _ProtectionExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 105.0,
    }])
    exchange.orders = _resting_leg_orders()
    return exchange


def test_time_stop_past_deadline_requests_a_review_instead_of_closing(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _stale_review_exchange()
    hook, requests = _recording_hook()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_review_requested"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == "pending"
    assert snapshot["phase"] == "time_stop_review_pending"
    assert datetime.fromisoformat(snapshot["time_stop_review_requested_at"]).tzinfo is not None
    assert len(requests) == 1
    requested_key, payload = requests[0]
    assert requested_key == position_key
    assert payload["symbol"] == "TIA/USDT:USDT"
    assert payload["side"] == "long"
    assert payload["favorable_r"] == pytest.approx(0.5)  # (105 - 100) / (100 - 90)
    assert payload["funding_r"] == pytest.approx(0.0)
    assert payload["fee_r"] == pytest.approx(0.01)  # 0.001 * 100 / 10


def test_fresh_pending_review_defers_the_close(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="pending",
        time_stop_review_requested_at=datetime.now(UTC).isoformat(),
        phase="time_stop_review_pending",
    )
    exchange = _stale_review_exchange()
    hook, requests = _recording_hook()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_review_pending"}]
    assert requests == []
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    assert job_store.load_position_state(position_key) is not None


def test_stale_pending_review_is_reclaimed_into_the_grace(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="pending",
        time_stop_review_requested_at=(
            datetime.now(UTC) - timedelta(minutes=45)
        ).isoformat(),
    )
    exchange = _stale_review_exchange()
    hook, _requests = _recording_hook()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(
        bridge, allow_mutation=True, config=_REVIEW_CONFIG, review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_review_grace"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == "unavailable"
    grace_until = datetime.fromisoformat(snapshot["time_stop_review_until"])
    assert timedelta(hours=5) < grace_until - datetime.now(UTC) <= timedelta(hours=6)
    assert [event["action"] for event in bridge.audit] == ["time_stop_review_unavailable"]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_unavailable_grace_blocks_the_close(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="unavailable",
        time_stop_review_until=(datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    )
    exchange = _stale_review_exchange()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=_recording_hook()[0],
    )

    assert result == [{"position_key": position_key, "status": "time_stop_review_grace"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    assert job_store.load_position_state(position_key) is not None


@pytest.mark.parametrize(
    ("config_override", "has_hook", "reason"),
    [
        ({"risk_derivatives_time_stop_review_enabled": False}, True, "position review disabled"),
        ({"risk_derivatives_time_stop_review_interval_hours": 0}, True, "review interval misconfigured"),
        ({}, False, "position review unavailable"),
    ],
)
def test_unavailable_grace_respects_review_configuration(
    monkeypatch, config_override, has_hook, reason,
):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="unavailable",
        time_stop_review_until=(datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    )
    exchange = _stale_review_exchange()
    bridge = _ProtectionBridge(exchange)
    hook = _recording_hook()[0] if has_hook else None

    result = manage_active_positions(
        bridge, allow_mutation=True, config={**_REVIEW_CONFIG, **config_override},
        review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert job_store.load_position_state(position_key) is None
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]
    assert reason in bridge.audit[0]["reason"]


@pytest.mark.parametrize(
    ("config_override", "has_hook"),
    [
        ({"risk_derivatives_time_stop_review_enabled": False}, True),
        ({"risk_derivatives_time_stop_review_interval_hours": 0}, True),
        ({}, False),
    ],
)
def test_granted_hold_survives_review_becoming_unavailable(monkeypatch, config_override, has_hook):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    review_until = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    _set_review_fields(
        position_key,
        time_stop_review_status="hold",
        time_stop_review_until=review_until,
    )
    exchange = _stale_review_exchange()
    bridge = _ProtectionBridge(exchange)
    hook, requests = _recording_hook()

    result = manage_active_positions(
        bridge, allow_mutation=True, config={**_REVIEW_CONFIG, **config_override},
        review_hook=hook if has_hook else None,
    )

    assert result == [{"position_key": position_key, "status": "unchanged"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == "hold"
    assert snapshot["time_stop_review_until"] == review_until
    assert requests == []
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    assert bridge.audit == []


def test_unavailable_grace_expiry_closes_mechanically(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="unavailable",
        time_stop_review_until=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    exchange = _stale_review_exchange()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(
        bridge, allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=_recording_hook()[0],
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert job_store.load_position_state(position_key) is None
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]
    assert "grace expired" in bridge.audit[0]["reason"]


def test_review_close_verdict_closes_with_the_reason_audited(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="close",
        time_stop_review_last={
            "verdict": "close",
            "reasoning": "thesis broken; funding turning against the short",
        },
    )
    exchange = _stale_review_exchange()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(
        bridge, allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=_recording_hook()[0],
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert job_store.load_position_state(position_key) is None
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]
    assert "review verdict close: thesis broken" in bridge.audit[0]["reason"]


@pytest.mark.parametrize("review_status", ["hold", "unavailable"])
def test_future_review_deadline_skips_time_stop_checks(monkeypatch, review_status):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    review_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    _set_review_fields(
        position_key,
        time_stop_review_status=review_status,
        time_stop_review_until=review_until,
        time_stop_review_count=1,
    )
    exchange = _FundingExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 105.0,
    }])
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)
    hook, requests = _recording_hook()

    for _ in range(2):
        result = manage_active_positions(
            bridge, allow_mutation=True, config=_REVIEW_CONFIG, review_hook=hook,
        )
        expected = "unchanged" if review_status == "hold" else "time_stop_review_grace"
        assert result == [{"position_key": position_key, "status": expected}]

    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == review_status
    assert snapshot["time_stop_review_until"] == review_until
    assert snapshot["time_stop_review_count"] == 1
    assert exchange.funding_calls == []
    assert requests == []
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
    assert bridge.audit == []


@pytest.mark.parametrize("review_status", ["hold", "unavailable"])
def test_future_review_deadline_keeps_stop_protection_active(monkeypatch, review_status):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    review_until = (datetime.now(UTC) + timedelta(hours=24)).isoformat()
    _set_review_fields(
        position_key,
        time_stop_review_status=review_status,
        time_stop_review_until=review_until,
        time_stop_review_count=1,
    )
    exchange = _FundingExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 110.0,
    }])
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)
    hook, requests = _recording_hook()

    result = manage_active_positions(
        bridge, allow_mutation=True, config=_REVIEW_CONFIG, review_hook=hook,
    )

    expected = "updated" if review_status == "hold" else "time_stop_review_grace"
    assert result == [{"position_key": position_key, "status": expected}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == review_status
    assert snapshot["time_stop_review_until"] == review_until
    assert snapshot["stop"] == pytest.approx(100.1)
    task = job_store.load_bracket_recovery(task_id)
    stop = next(leg for leg in task["legs"] if leg["name"] == "stop-loss")
    assert float(stop["params"]["stopLossPrice"]) == pytest.approx(100.1)
    assert exchange.funding_calls == []
    assert requests == []
    assert not [
        call for call in exchange.calls
        if call[0] == "create" and str(call[4].get("clientOrderId", "")).startswith("timestop-")
    ]
    assert bridge.audit == []


def test_expired_hold_rerequests_the_review(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    _set_review_fields(
        position_key,
        time_stop_review_status="hold",
        time_stop_review_until=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        time_stop_review_count=1,
    )
    exchange = _stale_review_exchange()
    hook, requests = _recording_hook()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_review_requested"}]
    assert len(requests) == 1
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot["time_stop_review_status"] == "pending"

    second = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=hook,
    )

    assert second == [{"position_key": position_key, "status": "time_stop_review_pending"}]
    assert len(requests) == 1
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_review_disabled_keeps_the_mechanical_close(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id = _linked_position()
    _set_opened_hours_ago(position_key, 80.0)
    exchange = _stale_review_exchange()
    hook, requests = _recording_hook()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(
        bridge, allow_mutation=True, config=_TIME_STOP_CONFIG, review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert requests == []
    assert job_store.load_position_state(position_key) is None
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"
    assert [event["action"] for event in bridge.audit] == ["time_stop_closed"]


def test_settlement_postpone_still_precedes_the_review(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, _task_id, exchange = _short_position_state()
    settlement_ms = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000)
    exchange.funding_rows = [{"amount": 2.0}]
    exchange.funding_rate = {"fundingRate": 0.0001, "nextFundingTimestamp": settlement_ms}
    exchange.orders = _resting_leg_orders()
    hook, _requests = _recording_hook()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True, config=_REVIEW_CONFIG,
        review_hook=hook,
    )

    assert result == [{"position_key": position_key, "status": "time_stop_postponed_funding"}]
    snapshot = job_store.load_position_state(position_key)["snapshot"]
    assert snapshot.get("time_stop_review_status") is None


def test_close_verdict_overrides_a_funding_rescue(monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    position_key, task_id, exchange = _short_position_state()
    # +30 USDT of funding on 10 USDT of initial risk is +3.0R: the mechanical
    # rescue would hold the position, but the reviewer's close verdict wins.
    exchange.funding_rows = [{"amount": 15.0}, {"amount": 15.0}]
    _set_review_fields(
        position_key,
        time_stop_review_status="close",
        time_stop_review_last={
            "verdict": "close",
            "reasoning": "carry income is not a thesis",
        },
    )
    exchange.orders = _resting_leg_orders()
    bridge = _ProtectionBridge(exchange)

    result = manage_active_positions(bridge, allow_mutation=True, config=_REVIEW_CONFIG)

    assert result == [{"position_key": position_key, "status": "time_stop_closed"}]
    assert job_store.load_position_state(position_key) is None
    assert job_store.load_bracket_recovery(task_id)["status"] == "time_stop_closed"
    assert "review verdict close: carry income is not a thesis" in bridge.audit[0]["reason"]
