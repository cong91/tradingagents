"""Deterministic position policy and command adapter for offline futures management."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Protocol


@dataclass(frozen=True)
class PositionSnapshot:
    position_key: str
    side: str
    entry: float
    quantity: float
    stop: float
    targets: tuple[float, ...]
    mark: float
    revision: int = 0
    break_even_armed: bool = False
    trail_anchor: float | None = None
    completed_targets: tuple[int, ...] = ()


@dataclass(frozen=True)
class PositionCommand:
    client_order_id: str
    side: str
    quantity: float
    reduce_only: bool
    kind: str
    stop: float | None = None


class PositionAdapter(Protocol):
    def position(self, position_key: str) -> PositionSnapshot | None: ...
    def apply(self, command: PositionCommand) -> None: ...


class PositionStateStore(Protocol):
    def load_position_state(self, position_key: str) -> dict | None: ...
    def save_position_state(self, position_key: str, expected_revision: int, state: dict) -> bool: ...
    def delete_position_state(self, position_key: str, expected_revision: int | None = None) -> bool: ...


class PositionManager:
    """Compute monotonic protection and reduce-only partial closes."""

    def __init__(self, adapter: PositionAdapter, *, break_even_r: float = 1.0,
                 trail_distance: float = 0.0, partial_fraction: float = 0.5,
                 state_store: PositionStateStore | None = None) -> None:
        if not 0 < partial_fraction <= 1 or break_even_r <= 0 or trail_distance < 0:
            raise ValueError("invalid position management policy")
        self.adapter = adapter
        self.break_even_r = break_even_r
        self.trail_distance = trail_distance
        self.partial_fraction = partial_fraction
        self.state_store = state_store
        self._submitted: set[str] = set()

    def tick(self, position_key: str) -> list[PositionCommand]:
        position = self.adapter.position(position_key)
        if position is None:
            if self.state_store:
                durable = self.state_store.load_position_state(position_key)
                if durable is not None:
                    self.state_store.delete_position_state(
                        position_key, expected_revision=durable.get("revision")
                    )
            return []
        if (position.side not in {"long", "short"} or not all(
                isfinite(value) and value > 0 for value in
                (position.entry, position.quantity, position.stop, position.mark))):
            raise ValueError("venue returned an invalid position snapshot")
        durable = self.state_store.load_position_state(position_key) if self.state_store else None
        expected_revision = position.revision
        if durable is not None:
            prior = durable["snapshot"]
            position = PositionSnapshot(**{
                **prior,
                "mark": position.mark,
                "quantity": position.quantity,
                "revision": prior.get("revision", position.revision),
                "stop": position.stop,
            })
            expected_revision = durable.get("revision", position.revision)
            self._submitted.update(durable.get("orders", []))
        long = position.side == "long"
        risk = abs(position.entry - position.stop)
        favorable = position.mark - position.entry if long else position.entry - position.mark
        commands: list[PositionCommand] = []
        if favorable >= risk * self.break_even_r and not position.break_even_armed:
            commands.append(PositionCommand(
                f"{position.position_key}:stop:{position.revision + 1}",
                "sell" if long else "buy", position.quantity, True,
                "break_even_stop", position.entry,
            ))
        if self.trail_distance and favorable > 0:
            candidate = position.mark - self.trail_distance if long else position.mark + self.trail_distance
            improves = candidate > position.stop if long else candidate < position.stop
            if improves:
                commands.append(PositionCommand(
                    f"{position.position_key}:trail:{position.revision + 1}",
                    "sell" if long else "buy", position.quantity, True,
                    "trailing_stop", candidate,
                ))
        for index, target in enumerate(position.targets):
            reached = position.mark >= target if long else position.mark <= target
            if reached and index not in position.completed_targets:
                qty = position.quantity if index == len(position.targets) - 1 else min(
                    position.quantity, position.quantity * self.partial_fraction
                )
                commands.append(PositionCommand(
                    f"{position.position_key}:tp:{index}",
                    "sell" if long else "buy", qty, True,
                    "partial_take_profit" if index < len(position.targets) - 1 else "final_take_profit",
                ))
                break
        new_orders = []
        for command in commands:
            if command.client_order_id not in self._submitted:
                self.adapter.apply(command)
                self._submitted.add(command.client_order_id)
                new_orders.append(command)
        if self.state_store and new_orders:
            state = asdict(position)
            state["targets"] = tuple(position.targets)
            state["completed_targets"] = tuple(sorted(set(position.completed_targets) | {
                int(c.client_order_id.rsplit(":", 1)[-1]) for c in new_orders if ":tp:" in c.client_order_id
            }))
            state["break_even_armed"] = position.break_even_armed or any(c.kind == "break_even_stop" for c in new_orders)
            state["trail_anchor"] = next((c.stop for c in new_orders if c.kind == "trailing_stop"), position.trail_anchor)
            state["revision"] = position.revision + 1
            if not self.state_store.save_position_state(
                position_key,
                expected_revision,
                {"snapshot": state, "orders": sorted(self._submitted)},
            ):
                return []
        return new_orders


class FakePositionAdapter:
    """In-memory adapter for deterministic tests; never contacts a venue."""

    def __init__(self, position: PositionSnapshot | None) -> None:
        self.current = position
        self.commands: dict[str, PositionCommand] = {}

    def position(self, position_key: str) -> PositionSnapshot | None:
        if self.current is None or self.current.position_key != position_key:
            return None
        return self.current

    def apply(self, command: PositionCommand) -> None:
        if not command.reduce_only:
            raise ValueError("position manager commands must be reduce-only")
        if command.client_order_id in self.commands:
            return
        if command.quantity > (self.current.quantity if self.current else 0):
            raise ValueError("close quantity exceeds reconciled position")
        self.commands[command.client_order_id] = command
        if command.stop is not None:
            self.current = PositionSnapshot(
                **{**asdict(self.current), "targets": tuple(self.current.targets),
                   "completed_targets": tuple(self.current.completed_targets), "stop": command.stop}
            )
        if command.kind in {"partial_take_profit", "final_take_profit"}:
            remaining = max(0.0, self.current.quantity - command.quantity)
            self.current = PositionSnapshot(
                **{**asdict(self.current), "targets": tuple(self.current.targets),
                   "completed_targets": tuple(self.current.completed_targets),
                   "quantity": remaining}
            )
