"""Write-ahead conditional leg lookup must settle before repair retries."""

from copy import deepcopy

import ccxt
import pytest

from server import job_store
from tests.execution_derivatives_support import VERIFIED_IDENTITY, bind_identity
from tradingagents.execution.brackets import BracketManager
from tradingagents.execution.exchange_client import venue_key
from tradingagents.execution.sizing import PlannedOrder

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_db(postgres_schema):
    return postgres_schema


class _AlgoExchange:
    def __init__(self, lookup=None, *, lose_ack=False):
        self.lookup = lookup
        self.lose_ack = lose_ack
        self.calls = []
        self.accepted = None

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append(("open", symbol, params))
        return []

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("lookup", order_id, symbol, params))
        if str(order_id) == "algo-accepted" and self.accepted is not None:
            return deepcopy(self.accepted)
        if self.lookup is None:
            raise ccxt.OrderNotFound("unknown conditional order")
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return deepcopy(self.lookup)

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.calls.append(("create", symbol, kind, side, amount, params))
        self.accepted = {
            "id": "algo-accepted", "symbol": symbol, "side": side,
            "amount": amount, "type": "market", "status": "open",
            "info": {
                "algoId": "algo-accepted", "symbol": "BTCUSDT",
                "side": side.upper(), "algoStatus": "NEW",
                "orderType": "STOP_MARKET", "triggerPrice": "48000",
                "quantity": str(amount), "reduceOnly": True,
                "workingType": "MARK_PRICE",
                "clientAlgoId": params["clientOrderId"],
            },
        }
        if self.lose_ack:
            self.lookup = self.accepted
            raise ccxt.NetworkError("conditional order accepted but ACK lost")
        return {"id": "algo-accepted"}

    def price_to_precision(self, symbol, price):
        return f"{float(price):.2f}"

    def amount_to_precision(self, symbol, quantity):
        return f"{float(quantity):.6f}"


class _Bridge:
    exchange_id = "binance"

    def __init__(self, exchange):
        self.exchange = exchange
        self.config = {
            "exec_base_url": "https://demo-api.binance.com",
            "exec_sandbox": True,
        }

    def client(self):
        return self.exchange

    def _redact(self, message):
        return message

    def audit_plan(self, order, **kwargs):
        return None


def _task(exchange, monkeypatch, *, status="submission_uncertain", leg_status="uncertain"):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    bridge = _Bridge(exchange)
    symbol = "BTC/USDT:USDT"
    task_id = f"bracket:{symbol}:entry-order"
    leg = {
        "name": "stop-loss", "params": {"stopLossPrice": 48_000.0},
        "status": leg_status, "order_id": None, "client_order_id": "br-stable-client",
    }
    assert job_store.create_bracket_recovery(
        task_id, symbol=symbol, entry_order_id="entry-order", close_side="sell",
        quantity=0.005, legs=[leg], now="2026-11-06T00:00:00+00:00",
        venue_key=venue_key(bridge), identity=VERIFIED_IDENTITY,
    )
    task = job_store.load_bracket_recovery(task_id)
    job_store.update_bracket_recovery(
        task_id, status=status, placed_ids=[], attempts=0, last_error=None,
        now="2026-11-06T00:00:01+00:00", legs=task["legs"],
    )
    return BracketManager(bridge), bridge, task_id


def _accepted_order(client_id="br-stable-client", *, status="NEW"):
    return {
        "id": "algo-accepted", "symbol": "BTC/USDT:USDT", "side": "sell",
        "amount": 0.005, "type": "market", "status": "open",
        "info": {
            "algoId": "algo-accepted", "symbol": "BTCUSDT", "side": "SELL",
            "algoStatus": status, "orderType": "STOP_MARKET",
            "triggerPrice": "48000", "quantity": "0.005", "reduceOnly": True,
            "workingType": "MARK_PRICE", "clientAlgoId": client_id,
        },
    }


def test_uncertain_leg_is_adopted_by_conditional_client_id(monkeypatch):
    exchange = _AlgoExchange(lookup=_accepted_order())
    manager, _bridge, task_id = _task(exchange, monkeypatch)

    result = manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    stored = job_store.load_bracket_recovery(task_id)
    assert result == "monitoring"
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "algo-accepted"
    assert [call[0] for call in exchange.calls].count("create") == 0
    assert ("lookup", "br-stable-client", "BTC/USDT:USDT",
            {"trigger": True, "clientOrderId": "br-stable-client"}) in exchange.calls


def test_lost_ack_is_recovered_without_duplicate_conditional_send(monkeypatch):
    exchange = _AlgoExchange(lose_ack=True)
    bridge = _Bridge(exchange)
    manager = BracketManager(bridge)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", market="derivatives",
        signal="Buy", side="buy", quantity=0.005, estimated_price=50_000,
        cost=250, stop_loss=48_000, take_profit=None,
    )
    bind_identity(monkeypatch, VERIFIED_IDENTITY)

    manager.place(order, True, "entry-order")
    task_id = "bracket:BTC/USDT:USDT:entry-order"
    stored = job_store.load_bracket_recovery(task_id)
    client_id = stored["legs"][0]["client_order_id"]
    exchange.calls.clear()
    exchange.lookup = exchange.accepted

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "monitoring"
    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "algo-accepted"
    assert stored["legs"][0]["client_order_id"] == client_id
    assert [call[0] for call in exchange.calls].count("create") == 0


def test_confirmed_client_id_not_found_releases_leg_to_pending_without_sending(monkeypatch):
    exchange = _AlgoExchange(lookup=ccxt.OrderNotFound("-2013 order does not exist"))
    manager, _bridge, task_id = _task(exchange, monkeypatch)

    manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True)

    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "pending"
    assert stored["legs"][0]["client_order_id"] == "br-stable-client"
    assert [call[0] for call in exchange.calls].count("create") == 0


def test_confirmed_canceled_client_id_releases_leg_to_pending_without_sending(monkeypatch):
    exchange = _AlgoExchange(lookup=_accepted_order(status="CANCELED"))
    manager, _bridge, task_id = _task(exchange, monkeypatch)

    manager.reconcile(task_id, repair_missing=False, allow_sibling_cancel=True)

    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "pending"
    assert stored["legs"][0]["client_order_id"] == "br-stable-client"
    assert [call[0] for call in exchange.calls].count("create") == 0


def test_confirmed_not_found_allows_repair_without_duplicate_retry(monkeypatch):
    exchange = _AlgoExchange(lookup=ccxt.OrderNotFound("-2013 order does not exist"))
    manager, _bridge, task_id = _task(exchange, monkeypatch)
    manager._remaining_contracts = lambda task: 0.005

    result = manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True)

    stored = job_store.load_bracket_recovery(task_id)
    assert result == "monitoring"
    assert stored["legs"][0]["status"] == "placed"
    assert stored["legs"][0]["order_id"] == "algo-accepted"
    assert [call[0] for call in exchange.calls].count("create") == 1
    assert [call[0] for call in exchange.calls].count("lookup") == 2


def test_unavailable_client_id_lookup_keeps_leg_uncertain(monkeypatch):
    exchange = _AlgoExchange(lookup=ccxt.NetworkError("query unavailable"))
    manager, _bridge, task_id = _task(exchange, monkeypatch)

    assert manager.reconcile(task_id, repair_missing=True, allow_sibling_cancel=True) == "reconcile_incomplete"

    stored = job_store.load_bracket_recovery(task_id)
    assert stored["legs"][0]["status"] == "uncertain"
    assert stored["legs"][0]["client_order_id"] == "br-stable-client"
    assert [call[0] for call in exchange.calls].count("create") == 0
