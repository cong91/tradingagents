from server import job_store
from tradingagents.execution.position_manager import (
    FakePositionAdapter,
    PositionManager,
    PositionSnapshot,
)


def test_position_manager_break_even_trailing_partial_and_dedupe():
    adapter = FakePositionAdapter(PositionSnapshot(
        position_key="BTC-long", side="long", entry=100, quantity=2,
        stop=95, targets=(110, 120), mark=110, revision=0,
    ))
    manager = PositionManager(adapter, trail_distance=3, partial_fraction=0.5)
    first = manager.tick("BTC-long")
    assert [item.kind for item in first] == ["break_even_stop", "trailing_stop", "partial_take_profit"]
    assert all(item.reduce_only for item in first)
    assert first[-1].quantity == 1
    assert manager.tick("BTC-long") == []


def test_short_management_uses_buy_reduce_only_actions():
    adapter = FakePositionAdapter(PositionSnapshot(
        position_key="ETH-short", side="short", entry=100, quantity=3,
        stop=105, targets=(90, 80), mark=90, revision=1,
    ))
    commands = PositionManager(adapter, trail_distance=3).tick("ETH-short")
    assert commands
    assert all(command.side == "buy" and command.reduce_only for command in commands)
    assert commands[-1].quantity <= 3


def test_restart_reloads_durable_orders_and_duplicate_ticks_are_idempotent(postgres_schema):
    adapter = FakePositionAdapter(PositionSnapshot("BTC-long", "long", 100, 2, 95, (110, 120), 110))
    assert PositionManager(adapter, trail_distance=3, state_store=job_store).tick("BTC-long")
    assert PositionManager(adapter, trail_distance=3, state_store=job_store).tick("BTC-long") == []
    assert len(adapter.commands) == 3


def test_reconcile_uses_exchange_quantity_after_partial_fill(postgres_schema):
    adapter = FakePositionAdapter(PositionSnapshot("BTC-long", "long", 100, 2, 95, (110, 120), 110))
    PositionManager(adapter, state_store=job_store).tick("BTC-long")
    adapter.current = PositionSnapshot("BTC-long", "long", 100, 1, 95, (110, 120), 120, revision=1, completed_targets=(0,))
    commands = PositionManager(adapter, state_store=job_store).tick("BTC-long")
    assert len(commands) == 1
    assert commands[0].quantity == 1


def test_restart_saves_new_management_orders_using_durable_revision(postgres_schema):
    adapter = FakePositionAdapter(PositionSnapshot(
        "BTC-long", "long", 100, 2, 95, (110, 120), 100, revision=0,
    ))
    store = job_store
    store.persist_futures_entry_state(
        "BTC-long", side="long", entry=100, quantity=2, stop=95,
        targets=[110, 120], mark=100, order_id="entry-1",
    )

    adapter.current = PositionSnapshot("BTC-long", "long", 100, 2, 95, (110, 120), 110, revision=0)
    commands = PositionManager(adapter, trail_distance=3, state_store=store).tick("BTC-long")

    assert [command.kind for command in commands] == [
        "break_even_stop", "trailing_stop", "partial_take_profit",
    ]
    durable = store.load_position_state("BTC-long")
    assert durable["revision"] == 2
    assert durable["orders"]


def test_position_manager_ignores_durable_policy_metadata_not_in_snapshot_model(postgres_schema):
    store = job_store
    position_key = "BTC-long"
    store.persist_futures_entry_state(
        position_key, side="long", entry=100, quantity=2, stop=95,
        targets=[110], mark=100, order_id="entry-1",
    )
    durable = store.load_position_state(position_key)
    durable["snapshot"].update({
        "initial_stop": 95,
        "current_target": 110,
        "observed_at": "2026-10-19T00:00:00+00:00",
    })
    store.save_position_state(position_key, durable["revision"], durable)
    adapter = FakePositionAdapter(PositionSnapshot(position_key, "long", 100, 2, 95, (110,), 100))

    assert PositionManager(adapter, state_store=store).tick(position_key) == []


    store = job_store
    store.persist_futures_entry_state(
        "BTC-long", side="long", entry=100, quantity=2, stop=95,
        targets=[110], mark=100, order_id="entry-1",
    )
    adapter = FakePositionAdapter(None)

    assert PositionManager(adapter, state_store=store).tick("BTC-long") == []
    assert store.load_position_state("BTC-long") is None


    assert job_store.save_position_state("BTC-long", 0, {"snapshot": {}, "orders": []})
    assert not job_store.save_position_state("BTC-long", 0, {"snapshot": {}, "orders": []})
