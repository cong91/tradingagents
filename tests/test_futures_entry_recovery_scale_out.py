"""Entry recovery verifies the two planned tranches at their own venue sizes."""

from pathlib import Path

import pytest

from server import job_store
from tests import test_futures_entry_partial_fill as partial, test_futures_entry_recovery as support
from tradingagents.execution.futures_entry_recovery import reconcile_entry
from tradingagents.execution.futures_operation_lock import operation_lock

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema):
    return postgres_schema


@pytest.mark.parametrize("quantity", [support.QUANTITY, partial.FILLED_QUANTITY])
def test_terminal_entry_recovery_accepts_journaled_scale_out(tmp_path, monkeypatch, quantity):
    exchange, bridge = support._setup(tmp_path, monkeypatch, position=partial._partial_position(quantity))
    exchange.orders[support.ORDER_ID] = partial._terminal_order(
        "FILLED" if quantity == support.QUANTITY else "CANCELED", filled=quantity,
    )
    exchange.trades[0]["qty"] = str(quantity)
    partial._set_intent_terms()
    task = job_store.load_bracket_recovery(support.INTENT_ID)
    task["legs"][0]["scale_out_policy"] = {
        "enabled": True, "first_r": 1.0, "first_fraction": 0.3,
        "second_r": 2.0, "second_fraction": 0.3,
    }
    job_store.update_bracket_recovery(
        support.INTENT_ID, status=task["status"], placed_ids=task["placed_ids"], attempts=0,
        last_error=None, now=task["updated_at"], legs=task["legs"],
    )
    support._seed_task(
        support.BRACKET_ID, entry_order_id=support.ORDER_ID, quantity=quantity,
        legs=[support._leg("stop-loss", "stop", "placed", support.STOP),
              {**support._leg("take-profit-1", "tp1", "placed", 11.0), "quantity": quantity * 0.3},
              {**support._leg("take-profit-2", "tp2", "placed", 12.0), "quantity": quantity * 0.3}],
    )
    for name, order_id, size, trigger in (
        ("stop-loss", "stop", quantity, support.STOP),
        ("take-profit-1", "tp1", quantity * 0.3, 11.0),
        ("take-profit-2", "tp2", quantity * 0.3, 12.0),
    ):
        exchange.orders[order_id] = support._conditional(order_id, name, quantity=size, trigger=trigger)
    event = partial._audit_rows(bridge, quantity=quantity)[0]
    partial._write_audit(Path(bridge.config["exec_log_path"]), [event])

    with operation_lock(support.SYMBOL):
        status = reconcile_entry(bridge, support.INTENT_ID)

    assert status == "completed"
    snapshot = job_store.load_position_state(f"{support.SYMBOL}:buy:{support.ORDER_ID}")["snapshot"]
    assert snapshot["quantity"] == quantity
    assert snapshot["targets"] == [11.0, 12.0]
