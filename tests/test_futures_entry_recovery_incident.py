"""End-to-end regression for reconciling an already-flat futures entry."""

from copy import deepcopy

import pytest

from server import job_store
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _recovery_db as _recovery_db,
    bind_identity,
    live_config,
    live_env,
    make_executor,
)
from tradingagents.execution.exchange_client import venue_key

pytestmark = pytest.mark.unit

SYMBOL = "CLANKER/USDT:USDT"
ENTRY_ID = "212651489"
ENTRY_CLIENT_ID = "entry-ac4bd63f956302885d384571"
STOP_ID = "1000000274238838"
STOP_CLIENT_ID = "x-cvBPrNm920b9c4a327167fb5324f0a"
CHILD_ID = "212756000"
SIBLING_ID = "1000000274238844"
QUANTITY = 18.39
PLANNED_QUANTITY = 18.392129918072534


class _IncidentExchange(FakeDerivExchange):
    """Offline demo exchange with the account and order evidence for the event."""

    def __init__(self):
        super().__init__(api_key="incident-key", secret="incident-secret")
        self.options = {"enableDemoTrading": True, "sandboxMode": False, "validateServerSsl": True}
        self.verify = True
        self.validateServerSsl = True
        self.verbose = False
        self._markets = {
            "CLANKER/USDT:USDT": {
                "id": "CLANKERUSDT", "symbol": "CLANKER/USDT:USDT",
                "base": "CLANKER", "quote": "USDT", "settle": "USDT",
                "settleId": "USDT", "contract": True, "linear": True,
                "inverse": False, "contractSize": 1.0,
            },
            "XMR/USDT:USDT": {
                "id": "XMRUSDT", "symbol": "XMR/USDT:USDT",
                "base": "XMR", "quote": "USDT", "settle": "USDT",
                "settleId": "USDT", "contract": True, "linear": True,
                "inverse": False, "contractSize": 1.0,
            },
        }
        self.markets_by_id = {item["id"]: item for item in self._markets.values()}
        self.position_rows = [
            self._position("CLANKERUSDT", "0", "0"),
            self._position("XMRUSDT", "-0.2", "-37"),
        ]
        self.order_responses = {
            ENTRY_ID: {
                "id": ENTRY_ID, "clientOrderId": ENTRY_CLIENT_ID,
                "symbol": SYMBOL, "side": "sell", "type": "market",
                "status": "closed", "amount": QUANTITY, "filled": QUANTITY,
                "remaining": 0.0, "average": 15.88, "price": 15.88,
                "timestamp": 1793893914415,
                "info": {
                    "orderId": ENTRY_ID, "clientOrderId": ENTRY_CLIENT_ID,
                    "symbol": "CLANKERUSDT", "side": "SELL", "type": "MARKET",
                    "status": "FILLED", "origQty": "18.39",
                    "executedQty": "18.39", "avgPrice": "15.8800000",
                    "cumQuote": "292.0332000", "price": "0.00000",
                    "time": 1793893914415, "updateTime": 1793893914419,
                    "reduceOnly": False, "positionSide": "BOTH",
                },
            },
            STOP_ID: {
                "id": STOP_ID, "symbol": SYMBOL, "side": "buy", "type": "market",
                "status": "closed", "amount": QUANTITY, "triggerPrice": 18.89,
                "reduceOnly": True,
                "info": {
                    "algoId": STOP_ID, "clientAlgoId": STOP_CLIENT_ID,
                    "symbol": "CLANKERUSDT", "side": "BUY",
                    "algoStatus": "FINISHED", "orderType": "STOP_MARKET",
                    "triggerPrice": "18.89000", "quantity": "18.39",
                    "reduceOnly": True, "workingType": "MARK_PRICE",
                    "actualOrderId": CHILD_ID, "actualQty": "18.39",
                    "createTime": 1793893914882, "updateTime": 1793918701132,
                },
            },
            SIBLING_ID: {
                "id": SIBLING_ID, "symbol": SYMBOL, "side": "buy",
                "type": "market", "status": "canceled", "amount": QUANTITY,
                "triggerPrice": 12.38, "reduceOnly": True,
                "info": {
                    "algoId": SIBLING_ID, "clientAlgoId": "x-cvB-take-client",
                    "symbol": "CLANKERUSDT", "side": "BUY",
                    "algoStatus": "CANCELED", "orderType": "TAKE_PROFIT_MARKET",
                    "triggerPrice": "12.38000", "quantity": "18.39",
                    "reduceOnly": True, "workingType": "MARK_PRICE",
                    "createTime": 1793893914998, "updateTime": 1793918708685,
                },
            },
            CHILD_ID: {
                "id": CHILD_ID, "clientOrderId": STOP_CLIENT_ID,
                "symbol": SYMBOL, "side": "buy", "type": "market",
                "status": "closed", "amount": QUANTITY, "filled": QUANTITY,
                "remaining": 0.0, "average": 18.64, "price": 18.64,
                "info": {
                    "orderId": CHILD_ID, "clientOrderId": STOP_CLIENT_ID,
                    "symbol": "CLANKERUSDT", "side": "BUY", "type": "MARKET",
                    "status": "FILLED", "origQty": "18.39",
                    "executedQty": "18.39", "avgPrice": "18.64",
                    "cumQuote": "342.7896", "price": "0.00000",
                    "reduceOnly": True, "positionSide": "BOTH",
                },
            },
        }

    @staticmethod
    def _position(symbol, amount, notional):
        return {
            "symbol": symbol, "positionSide": "BOTH", "positionAmt": amount,
            "entryPrice": "18.50", "markPrice": "18.64", "notional": notional,
            "unRealizedProfit": "0", "marginAsset": "USDT",
            "updateTime": 1783000100000,
        }

    def load_markets(self):
        self.calls.append(("load_markets",))
        self._markets_loaded = True
        return deepcopy(self._markets)

    def market(self, symbol):
        return deepcopy(self._markets[symbol])

    def amount_to_precision(self, symbol, amount):
        if symbol == SYMBOL:
            return "18.39" if float(amount) == PLANNED_QUANTITY else f"{float(amount):.2f}"
        return super().amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol, price):
        if symbol == SYMBOL:
            return f"{float(price):.2f}"
        raise ValueError(f"unexpected symbol {symbol}")

    def fapiPrivateV3GetPositionRisk(self, params=None):
        self.calls.append(("fapiPrivateV3GetPositionRisk", params))
        return deepcopy(self.position_rows)

    def fetch_positions(self, symbols=None, params=None):
        self.calls.append(("fetch_positions", symbols, params))
        return []

    def fetch_open_orders(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("fetch_open_orders", symbol, since, limit, params))
        return []

    def fetch_order(self, order_id, symbol, params=None):
        self.calls.append(("fetch_order", str(order_id), symbol, params))
        order = self.order_responses.get(str(order_id))
        if order is None:
            import ccxt

            raise ccxt.OrderNotFound(f"unknown incident order {order_id}")
        return deepcopy(order)


def _incident(tmp_path, monkeypatch):
    exchange = _IncidentExchange()
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    executor = make_executor(
        tmp_path,
        monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
        env=live_env(),
    )
    intent_id = f"entry-intent:{SYMBOL}:{ENTRY_CLIENT_ID}"
    bracket_id = f"bracket:{SYMBOL}:{ENTRY_ID}"
    position_key = f"{SYMBOL}:sell:{ENTRY_ID}"
    venue = venue_key(executor._bridge)

    assert job_store.create_bracket_recovery(
        intent_id,
        symbol=SYMBOL,
        entry_order_id=ENTRY_CLIENT_ID,
        close_side="buy",
        quantity=PLANNED_QUANTITY,
        legs=[{
            "name": "entry-intent", "status": "uncertain", "order_id": None,
            "client_order_id": ENTRY_CLIENT_ID,
        }],
        now="2026-11-05T15:51:54+00:00",
        venue_key=None,
        identity=VERIFIED_IDENTITY,
    )
    job_store.update_bracket_recovery(
        intent_id,
        status="submission_uncertain",
        placed_ids=[ENTRY_ID],
        attempts=0,
        last_error="entry ACK persisted; reconciliation pending",
        now="2026-11-05T12:00:01+00:00",
        legs=[{
            "name": "entry-intent", "status": "uncertain", "order_id": ENTRY_ID,
            "client_order_id": ENTRY_CLIENT_ID,
        }],
    )
    assert job_store.create_bracket_recovery(
        bracket_id,
        symbol=SYMBOL,
        entry_order_id=ENTRY_ID,
        close_side="buy",
        quantity=QUANTITY,
        legs=[
            {"name": "stop-loss", "params": {"stopLossPrice": 18.89},
             "status": "filled", "order_id": STOP_ID},
            {"name": "take-profit", "params": {"takeProfitPrice": 12.38},
             "status": "cancelled", "order_id": SIBLING_ID},
        ],
        now="2026-11-05T12:00:02+00:00",
        venue_key=venue,
        identity=VERIFIED_IDENTITY,
    )
    job_store.update_bracket_recovery(
        bracket_id,
        status="reconciled_stop_loss",
        placed_ids=[STOP_ID, SIBLING_ID],
        attempts=1,
        last_error=None,
        now="2026-11-05T12:00:03+00:00",
        legs=[
            {"name": "stop-loss", "params": {"stopLossPrice": 18.89},
             "status": "filled", "order_id": STOP_ID},
            {"name": "take-profit", "params": {"takeProfitPrice": 12.38},
             "status": "cancelled", "order_id": SIBLING_ID},
        ],
    )
    return executor, exchange, intent_id, bracket_id, position_key


def test_flat_clanker_entry_is_completed_from_owned_terminal_exit_evidence(
    tmp_path, monkeypatch, _recovery_db,
):
    executor, exchange, intent_id, bracket_id, position_key = _incident(tmp_path, monkeypatch)

    result = executor.monitor_bracket(intent_id)

    assert result == "completed"
    intent = job_store.load_bracket_recovery(intent_id)
    assert intent["status"] == "completed"
    assert intent["placed_ids"] == [ENTRY_ID]
    assert intent["legs"][0]["order_id"] == ENTRY_ID
    assert intent["legs"][0]["client_order_id"] == ENTRY_CLIENT_ID
    bracket = job_store.load_bracket_recovery(bracket_id)
    assert bracket["status"] == "reconciled_stop_loss"
    assert [leg["status"] for leg in bracket["legs"]] == ["filled", "cancelled"]
    assert job_store.load_position_state(position_key) is None
    assert ("fetch_order", ENTRY_ID, SYMBOL, {}) in exchange.calls
    assert ("fetch_order", STOP_ID, SYMBOL, {"trigger": True}) in exchange.calls
    assert ("fetch_order", CHILD_ID, SYMBOL, {}) in exchange.calls
    assert not [call for call in exchange.calls if call[0] in {"create_order", "cancel_order"}]
