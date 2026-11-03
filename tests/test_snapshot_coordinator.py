"""Regression tests for the display-only snapshot coordinator and observer."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from server import snapshot_coordinator, snapshot_observer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", "true")
    monkeypatch.setenv("TRADINGAGENTS_UI_SNAPSHOT_SINGLE_PROCESS", "true")
    snapshot_coordinator.reset_for_tests()
    monkeypatch.setattr(snapshot_coordinator.history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    yield
    snapshot_coordinator.reset_for_tests()


def bridge():
    client = SimpleNamespace(apiKey="key", secret="secret")
    venue = SimpleNamespace(
        exchange_id="binance",
        config={"exec_base_url": "https://demo-api.binance.com"},
    )
    return venue, client


def context_of(venue, client) -> str:
    context = snapshot_coordinator.credential_context(venue, client)
    assert context is not None
    return context


def test_twenty_callers_share_one_inflight_read():
    venue, client = bridge()
    context = context_of(venue, client)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def load():
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(2)
        return {"value": 7}

    results = []
    errors = []

    def caller():
        try:
            results.append(snapshot_coordinator.read(context, "portfolio", {"scope": "all"}, load, ttl_seconds=30))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=caller) for _ in range(20)]
    for worker in workers:
        worker.start()
    assert started.wait(2)
    time.sleep(0.05)
    release.set()
    for worker in workers:
        worker.join(2)

    assert errors == []
    assert calls == 1
    assert len(results) == 20
    assert all(result[0] == {"value": 7} for result in results)
    assert results[0][1]["freshness"] == "unknown"
    assert results[0][1]["status"] == "partial"


def test_mutation_landing_midflight_supersedes_the_result():
    venue, client = bridge()
    context = context_of(venue, client)
    started = threading.Event()
    release = threading.Event()

    def load():
        started.set()
        assert release.wait(2)
        return {"value": "before"}

    results = []
    errors = []

    def read_before_mutation():
        try:
            results.append(snapshot_coordinator.read(context, "portfolio", {}, load, ttl_seconds=30))
        except snapshot_coordinator.SnapshotUnavailable as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=read_before_mutation)
    worker.start()
    assert started.wait(2)
    token = snapshot_observer.before(venue, client, operation="create_order", symbol="BTC")
    snapshot_observer.after(venue, client, operation="create_order", symbol="BTC", token=token)
    release.set()
    worker.join(2)

    # The read started before the mutation: it is not published as current.
    assert results == []
    assert errors == ["superseded"]
    # Pending cleared exactly once by the outcome token.
    assert snapshot_coordinator.status(context)["pending"] is False
    events, missed = snapshot_coordinator.events_after(0, wait_seconds=0)
    assert missed is False
    assert [event["phase"] for event in events] == ["pending", "outcome"]
    assert all(event["operation"] == "create_order" for event in events)


def test_undisturbed_read_clears_dirty_and_is_cached():
    venue, client = bridge()
    context = context_of(venue, client)
    token = snapshot_observer.before(venue, client, operation="cancel_order", symbol="BTC")
    snapshot_observer.after(venue, client, operation="cancel_order", symbol="BTC", token=token)

    result, metadata = snapshot_coordinator.read(
        context, "portfolio", {}, lambda: {"fresh": True}, ttl_seconds=30,
    )

    assert result == {"fresh": True}
    assert metadata["dirty"] is False
    assert metadata["pending"] is False
    assert snapshot_coordinator.status(context)["dirty"] is False
    cached, cached_metadata = snapshot_coordinator.read(
        context, "portfolio", {}, lambda: pytest.fail("cache missed"), ttl_seconds=30,
    )
    assert cached == result
    assert cached_metadata["cache"] == "hit"


def test_cache_hit_within_ttl_and_disabled_mode_passes_through(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    calls = []

    def load():
        calls.append(len(calls) + 1)
        return {"n": len(calls)}

    first, first_metadata = snapshot_coordinator.read(context, "portfolio", {}, load, ttl_seconds=30)
    second, second_metadata = snapshot_coordinator.read(context, "portfolio", {}, load, ttl_seconds=30)
    assert calls == [1]
    assert first == second
    assert first_metadata["cache"] == "miss"
    assert second_metadata["cache"] == "hit"

    monkeypatch.delenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", raising=False)
    disabled, disabled_metadata = snapshot_coordinator.read(context, "portfolio", {}, load)
    assert calls == [1, 2]
    assert disabled == {"n": 2}
    assert disabled_metadata["context_id"] is None
    assert disabled_metadata["generation"] is None


def test_observer_failure_never_changes_venue_result_or_exception(monkeypatch):
    venue, client = bridge()

    def broken(**_kwargs):
        raise RuntimeError("publisher down")

    monkeypatch.setattr(snapshot_coordinator, "publish_mutation", broken)

    assert snapshot_observer.run(
        venue, client, operation="cancel_order", symbol="BTC", call=lambda: "returned",
    ) == "returned"

    with pytest.raises(ValueError, match="venue"):
        snapshot_observer.run(
            venue, client, operation="cancel_order", symbol="BTC",
            call=lambda: (_ for _ in ()).throw(ValueError("venue")),
        )


def test_observer_is_a_noop_when_the_feature_is_disabled(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_UI_SNAPSHOT_SYNC", raising=False)
    snapshot_coordinator.reset_for_tests()
    venue, client = bridge()

    token = snapshot_observer.before(venue, client, operation="create_order", symbol="BTC")
    snapshot_observer.after(venue, client, operation="create_order", symbol="BTC", token=token)

    assert token is None
    events, missed = snapshot_coordinator.events_after(0, wait_seconds=0)
    assert events == [] and missed is False


def test_bounds_and_identity_rebinding():
    venue, client = bridge()
    context = context_of(venue, client)
    for _ in range(snapshot_coordinator.MAX_READS_PER_MINUTE):
        snapshot_coordinator.read(
            context, "portfolio", {}, lambda: {"ok": True},
            ttl_seconds=0, min_interval_seconds=0,
        )
    with pytest.raises(snapshot_coordinator.SnapshotUnavailable, match="rate_limited"):
        snapshot_coordinator.read(
            context, "portfolio", {}, lambda: {"ok": True},
            ttl_seconds=0, min_interval_seconds=0,
        )

    # A rotated credential rebinds the context and gets its own budget.
    client.secret = "rotated-secret"
    rotated = context_of(venue, client)
    assert rotated != context
    result, metadata = snapshot_coordinator.read(
        rotated, "portfolio", {}, lambda: {"ok": True},
        ttl_seconds=0, min_interval_seconds=0,
    )
    assert result == {"ok": True}
    assert metadata["context_id"] == rotated


def test_event_spam_cannot_bypass_start_gap_and_old_data_is_marked_stale(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)
    starts = []

    def load():
        starts.append(clock.now)
        return {"value": len(starts)}

    first, first_meta = snapshot_coordinator.read(context, "portfolio", {}, load)
    for _ in range(30):
        token = snapshot_observer.before(venue, client, operation="cancel", symbol="BTC")
        snapshot_observer.after(venue, client, operation="cancel", symbol="BTC", token=token)
        old, metadata = snapshot_coordinator.read(context, "portfolio", {}, load)
        assert old == first
        assert metadata["cache"] == "stale"
        assert metadata["dirty"] is True
        assert metadata["response_received_at"] == first_meta["response_received_at"]
    assert starts == [100.0]
    clock.now = 105.0
    current, metadata = snapshot_coordinator.read(context, "portfolio", {}, load)
    assert current == {"value": 2}
    assert metadata["dirty"] is False
    assert starts == [100.0, 105.0]
    assert snapshot_coordinator.read(context, "portfolio", {}, load)[1]["cache"] == "hit"
    assert starts == [100.0, 105.0]


def test_overflow_and_lost_outcome_recover_without_sticky_dirty(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)
    token = snapshot_observer.before(venue, client, operation="create", symbol="BTC")
    assert token is not None
    snapshot_coordinator._OVERFLOW.set()
    clock.now += snapshot_coordinator.PENDING_TIMEOUT_SECONDS
    _, metadata = snapshot_coordinator.read(context, "portfolio", {}, lambda: {"value": 1})
    assert metadata["pending"] is False
    assert metadata["dirty"] is False
    assert snapshot_coordinator.read(context, "portfolio", {}, lambda: {})[1]["cache"] == "hit"


def test_shared_cooldown_blocks_new_snapshot_start(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    calls = []
    snapshot_coordinator.history_cache.start_cooldown({"retry_after_seconds": 1800})
    with pytest.raises(snapshot_coordinator.SnapshotUnavailable, match="rate_limit_cooldown"):
        snapshot_coordinator.read(context, "portfolio", {}, lambda: calls.append(1))
    assert calls == []


def test_mutation_invalidates_each_cache_key_after_another_key_refreshes(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)

    for scope in ("spot", "futures"):
        snapshot_coordinator.read(
            context, "portfolio", {"scope": scope}, lambda: {"value": "before"},
            min_interval_seconds=0,
        )

    token = snapshot_observer.before(venue, client, operation="cancel", symbol="BTC")
    snapshot_observer.after(venue, client, operation="cancel", symbol="BTC", token=token)
    spot, spot_meta = snapshot_coordinator.read(
        context, "portfolio", {"scope": "spot"}, lambda: {"value": "after"},
        min_interval_seconds=0,
    )
    futures, futures_meta = snapshot_coordinator.read(
        context, "portfolio", {"scope": "futures"}, lambda: {"value": "after"},
        min_interval_seconds=0,
    )

    assert spot == futures == {"value": "after"}
    assert spot_meta["cache"] == futures_meta["cache"] == "miss"
    assert spot_meta["generation"] == futures_meta["generation"] == 2


def test_identity_changed_failure_does_not_fall_back_to_cached_account():
    venue, client = bridge()
    context = context_of(venue, client)
    snapshot_coordinator.read(
        context, "portfolio", {}, lambda: {"value": "old"}, ttl_seconds=0,
        min_interval_seconds=0,
    )

    def reject():
        raise snapshot_coordinator.SnapshotUnavailable("account_identity_changed")

    with pytest.raises(snapshot_coordinator.SnapshotUnavailable, match="account_identity_changed"):
        snapshot_coordinator.read(
            context, "portfolio", {}, reject, ttl_seconds=0, min_interval_seconds=0,
        )


def test_superseded_other_key_fallback_is_stale_after_dirty_clears(monkeypatch):
    venue, client = bridge()
    context = context_of(venue, client)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(snapshot_coordinator, "monotonic", lambda: clock.now)

    for scope in ("spot", "futures"):
        snapshot_coordinator.read(context, "portfolio", {"scope": scope}, lambda scope=scope: scope)
    token = snapshot_observer.before(venue, client, operation="cancel", symbol="BTC")
    snapshot_observer.after(venue, client, operation="cancel", symbol="BTC", token=token)
    clock.now += 5
    snapshot_coordinator.read(context, "portfolio", {"scope": "spot"}, lambda: "new")
    snapshot_coordinator.history_cache.start_cooldown({"retry_after_seconds": 10})

    result, metadata = snapshot_coordinator.read(
        context, "portfolio", {"scope": "futures"}, lambda: pytest.fail("cooldown"),
    )

    assert result == "futures"
    assert metadata["cache"] == "stale"
    assert metadata["dirty"] is True
    assert metadata["generation"] == 0
