from datetime import datetime, timezone

import pytest

from server import job_store
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
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }

    def market(self, symbol):
        return {"quote": "USDT", "settle": "USDT", "inverse": False}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", symbol, side, amount, params))
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
    )
    return position_key, task_id


def test_live_position_management_uses_venue_entry_quantity_and_mark():
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
    assert created == [
        ("create", "TIA/USDT:USDT", "sell", 2.0,
         {"stopLossPrice": 125.0, "reduceOnly": True, "workingType": "MARK_PRICE"}),
        ("create", "TIA/USDT:USDT", "sell", 2.0,
         {"takeProfitPrice": 141.0, "reduceOnly": True, "workingType": "MARK_PRICE"}),
    ]
    task = job_store.load_bracket_recovery(task_id)
    assert task["legs"][0]["retired_ids"] == []
    assert task["legs"][1]["retired_ids"] == []


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

    assert result == [{"position_key": position_key, "status": "closed"}]
    assert job_store.load_position_state(position_key) is None
    assert {call[1] for call in exchange.calls if call[0] == "cancel"} == {"old-stop", "old-target"}


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

    assert result == [{"position_key": position_key, "status": "closed_position_cleanup_pending"}]
    assert job_store.load_position_state(position_key) is not None


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

    assert result == [{"position_key": position_key, "status": "unchanged"}]
    linked = job_store.load_position_state(position_key)
    assert linked["bracket_task_id"] == "bracket:TIA/USDT:USDT:legacy-entry"


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
