"""Remove a closed position snapshot only after its bracket and account prove closure."""

from datetime import UTC, datetime

import pytest

from server import job_store
from tests.execution_derivatives_support import VERIFIED_IDENTITY, bind_identity
from tests.test_futures_protection import _linked_position, _ProtectionBridge, _ProtectionExchange
from tradingagents.execution.futures_protection import manage_active_positions

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_db(postgres_schema, monkeypatch):
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    return postgres_schema


def _closed_bracket(task_id, *, retired=False):
    task = job_store.load_bracket_recovery(task_id)
    task["legs"][0]["status"] = "filled"
    task["legs"][1]["status"] = "cancelled"
    if retired:
        task["legs"][0]["retired_ids"] = ["old-protection"]
    job_store.update_bracket_recovery(
        task_id, status="reconciled_stop_loss", placed_ids=task["placed_ids"], attempts=0,
        last_error=None, now=datetime.now(UTC).isoformat(), legs=task["legs"],
    )


def test_verified_flat_account_retires_snapshot_after_bracket_exit_cleanup():
    position_key, task_id = _linked_position()
    _closed_bracket(task_id)
    exchange = _ProtectionExchange(positions=[])

    outcome = manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=True,
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert outcome == [{"position_key": position_key, "status": "completed"}]
    assert job_store.load_position_state(position_key) is None
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]


@pytest.mark.parametrize("fault", ["pending_cleanup", "wrong_account", "mutation_gated", "unresolved_entry"])
def test_closed_snapshot_retained_when_closure_evidence_is_incomplete(fault):
    position_key, task_id = _linked_position()
    _closed_bracket(task_id, retired=fault == "pending_cleanup")
    if fault == "wrong_account":
        state = job_store.load_position_state(position_key)
        state["account_identity_hash"] = "0" * 64
        assert job_store.save_position_state(position_key, state["revision"], state)
    if fault == "unresolved_entry":
        job_store.create_bracket_recovery(
            "entry-intent:TIA/USDT:USDT:client", symbol="TIA/USDT:USDT",
            entry_order_id="client", close_side="sell", quantity=2,
            legs=[{"name": "entry-intent", "status": "uncertain"}],
            now=datetime.now(UTC).isoformat(), identity=VERIFIED_IDENTITY,
        )
    exchange = _ProtectionExchange(positions=[])

    manage_active_positions(
        _ProtectionBridge(exchange), allow_mutation=fault != "mutation_gated",
        config={"risk_derivatives_profit_protection_enabled": True},
    )

    assert job_store.load_position_state(position_key) is not None
    assert not [call for call in exchange.calls if call[0] in {"create", "cancel"}]
