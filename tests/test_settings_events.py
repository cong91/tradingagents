"""Unit tests for the settings SSE invalidation channel (server/settings_events.py).

The stream itself is a thin generator over ``events_after`` — these tests pin
the store semantics the chip relies on: publish wakes waiting subscribers,
ids are monotonic, and an idle wait returns nothing instead of spinning.
"""

from __future__ import annotations

import threading

from server import settings_events


def test_publish_wakes_a_waiting_subscriber():
    received: list[list[dict]] = []

    def subscriber():
        received.append(settings_events.events_after(settings_events.latest_event_id(), wait_seconds=5.0))

    waiter = threading.Thread(target=subscriber)
    waiter.start()
    settings_events.publish_change("operator_config_updated")
    waiter.join(timeout=2.0)
    assert not waiter.is_alive()
    assert len(received[0]) == 1
    assert received[0][0]["type"] == "changed"
    assert received[0][0]["reason"] == "operator_config_updated"


def test_idle_wait_returns_no_events():
    before = settings_events.latest_event_id()
    events = settings_events.events_after(before, wait_seconds=0.05)
    assert events == []
    assert settings_events.latest_event_id() == before


def test_ids_are_monotonic_and_events_after_skips_consumed_ones():
    first = settings_events.latest_event_id()
    settings_events.publish_change("operator_config_updated")
    settings_events.publish_change("operator_config_reset")
    new_id = settings_events.latest_event_id()
    assert new_id >= first + 2

    consumed = settings_events.events_after(first, wait_seconds=0.0)
    assert [event["id"] for event in consumed] == [new_id - 1, new_id]
    # Already-consumed events are not replayed.
    assert settings_events.events_after(new_id, wait_seconds=0.0) == []
