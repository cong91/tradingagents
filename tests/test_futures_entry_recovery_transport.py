"""Recovery reads may isolate HTTP transport without rebinding the execution client."""

from copy import copy

import pytest

from server import job_store
from tests import test_futures_entry_recovery as support
from tradingagents.execution.futures_entry_recovery import reconcile_entry
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema):
    return postgres_schema


def test_isolated_transport_copy_does_not_count_as_a_changed_venue_client(tmp_path, monkeypatch):
    exchange, bridge = support._setup(tmp_path, monkeypatch)
    exchange.orders[support.ORDER_ID] = support._entry_order()
    support._add_terminal_bracket(exchange)
    monkeypatch.setattr("server.binance_futures_positions.isolated_client", copy)

    with operation_lock(support.SYMBOL):
        status = reconcile_entry(bridge, support.INTENT_ID)

    assert status == "completed"
    assert job_store.load_bracket_recovery(support.INTENT_ID)["status"] == "completed"


def test_actual_execution_client_rebinding_still_blocks_recovery(tmp_path, monkeypatch):
    exchange, bridge = support._setup(tmp_path, monkeypatch)
    exchange.orders[support.ORDER_ID] = support._entry_order()
    support._add_terminal_bracket(exchange)
    original_fetch = exchange.fetch_order

    def fetch_and_rebind(order_id, symbol, params=None):
        result = original_fetch(order_id, symbol, params)
        bridge._exchange = copy(exchange)
        return result

    monkeypatch.setattr(exchange, "fetch_order", fetch_and_rebind)
    monkeypatch.setattr("server.binance_futures_positions.isolated_client", copy)

    with operation_lock(support.SYMBOL):
        status = reconcile_entry(bridge, support.INTENT_ID)

    assert status == "client_changed"
    assert job_store.load_bracket_recovery(support.INTENT_ID)["status"] == "submission_uncertain"
