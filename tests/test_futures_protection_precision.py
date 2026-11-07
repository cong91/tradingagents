"""Initial stops and current stops must use the same venue tick geometry."""

from decimal import ROUND_HALF_UP, Decimal

import pytest

from server import job_store
from tests.execution_derivatives_support import VERIFIED_IDENTITY, bind_identity
from tests.test_futures_protection import (
    _linked_position,
    _ProtectionBridge,
    _ProtectionExchange,
    _resting_leg_orders,
)
from tradingagents.execution.futures_protection import manage_active_positions

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema, monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    return postgres_schema


class _TickExchange(_ProtectionExchange):
    def price_to_precision(self, symbol, price):
        return str(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@pytest.mark.parametrize("side,initial_stop,current_stop,mark,target", [
    ("short", 110.006, 110.01, 99.0, 70.0),
    ("long", 90.004, 90.0, 101.0, 130.0),
])
def test_initial_stop_rounding_does_not_break_position_management(
    side, initial_stop, current_stop, mark, target,
):
    position_key, task_id = _linked_position(side=side, stop=initial_stop, target=target)
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][0]["params"]["stopLossPrice"] = current_stop
    job_store.update_bracket_recovery(
        task_id, status=task["status"], placed_ids=task["placed_ids"], attempts=0,
        last_error=None, now=task["updated_at"], legs=task["legs"],
    )
    exchange = _TickExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": side, "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": mark,
    }])

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "unchanged"}]
    assert job_store.load_position_state(position_key)["snapshot"]["initial_stop"] == current_stop
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


def test_tick_rounding_does_not_accept_a_genuinely_loosened_stop():
    position_key, task_id = _linked_position(stop=90.004)
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][0]["params"]["stopLossPrice"] = 89.99
    job_store.update_bracket_recovery(
        task_id, status=task["status"], placed_ids=task["placed_ids"], attempts=0,
        last_error=None, now=task["updated_at"], legs=task["legs"],
    )
    exchange = _TickExchange(positions=[{
        "symbol": "TIA/USDT:USDT", "side": "long", "contracts": 1.0,
        "entryPrice": 100.0, "markPrice": 101.0,
    }])
    exchange.orders = _resting_leg_orders()

    result = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert result == [{"position_key": position_key, "status": "operation_lock_error:ValueError"}]
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
