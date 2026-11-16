"""Cycle-scoped Kronos service ownership and cleanup."""

import threading
import time

import pytest

from server import kronos_lifecycle

pytestmark = pytest.mark.unit


class _Service:
    def __init__(self):
        self.close_calls = 0
        self.closed = False

    def close(self):
        self.close_calls += 1
        self.closed = True


def test_owner_close_waits_for_the_active_lease(monkeypatch):
    service = _Service()
    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", lambda _config: service)
    owner = kronos_lifecycle.KronosForecastOwner({})
    try:
        with owner.lease(None):
            assert owner.close(timeout=0) is False
            assert service.close_calls == 0
            with pytest.raises(RuntimeError, match="closing"), owner.lease(None):
                pytest.fail("a closing owner cannot grant a new lease")
        assert owner.close(timeout=2)
        assert service.close_calls == 1
        assert owner.close(timeout=0)
        assert service.close_calls == 1
    finally:
        owner.close(timeout=2)


def test_owner_reports_pending_until_cleanup_finishes(monkeypatch):
    started, release = threading.Event(), threading.Event()

    class BlockingService(_Service):
        def close(self):
            self.close_calls += 1
            started.set()
            assert release.wait(2)
            self.closed = True

    service = BlockingService()
    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", lambda _config: service)
    owner = kronos_lifecycle.KronosForecastOwner({})
    try:
        with owner.lease(None):
            pass
        assert not owner.close(timeout=0.05)
        assert started.wait(2)
        assert owner.cleanup_state() == "pending"
        release.set()
        assert owner.close(timeout=2)
        assert owner.cleanup_state() == "complete"
        assert service.close_calls == 1
    finally:
        release.set()
        owner.close(timeout=2)


def test_owner_rejects_waiting_lease_after_close(monkeypatch):
    service = _Service()
    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", lambda _config: service)
    owner = kronos_lifecycle.KronosForecastOwner({})
    waiting, entered = threading.Event(), threading.Event()
    failures = []
    wait = owner._condition.wait

    def record_wait(timeout=None):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(owner._condition, "wait", record_wait)

    def second_lease():
        try:
            with owner.lease(time.monotonic() + 5):
                entered.set()
        except RuntimeError as exc:
            failures.append(str(exc))

    worker = threading.Thread(target=second_lease)
    try:
        with owner.lease(None):
            worker.start()
            assert waiting.wait(2)
            assert not entered.is_set()
            owner.close(timeout=0)
            worker.join(timeout=2)
            assert not worker.is_alive()
            assert failures == ["Kronos forecast owner is closing"]
            assert service.close_calls == 0
        assert owner.close(timeout=2)
        assert service.close_calls == 1
    finally:
        owner.close(timeout=2)
        if worker.ident is not None:
            worker.join(timeout=2)


def test_owner_can_close_during_service_construction(monkeypatch):
    building, finish, returned = threading.Event(), threading.Event(), threading.Event()
    service = _Service()
    owner = kronos_lifecycle.KronosForecastOwner({})
    failures = []

    def build(_config):
        building.set()
        assert finish.wait(2)
        return service

    def lease():
        try:
            with owner.lease(None) as active:
                assert active is service
                assert service.close_calls == 0
        except Exception as exc:
            failures.append(exc)

    def close():
        owner.close(timeout=0)
        returned.set()

    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", build)
    worker, closer = threading.Thread(target=lease), threading.Thread(target=close)
    try:
        worker.start()
        assert building.wait(2)
        closer.start()
        assert returned.wait(2)
        assert service.close_calls == 0
    finally:
        finish.set()
        worker.join(timeout=2)
        closer.join(timeout=2)
        owner.close(timeout=2)
    assert not worker.is_alive() and not closer.is_alive()
    assert not failures
    assert service.close_calls == 1


def test_owner_keeps_slot_when_cleanup_cannot_be_confirmed(monkeypatch):
    monkeypatch.setattr(kronos_lifecycle, "_WARM_OWNER_SLOT", threading.Lock())

    class BrokenService(_Service):
        def close(self):
            self.close_calls += 1
            raise OSError("cleanup failed")

    service = BrokenService()
    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", lambda _config: service)
    owner = kronos_lifecycle.KronosForecastOwner({})
    with owner.lease(None):
        pass
    assert not owner.close(timeout=2)
    assert owner.cleanup_state() == "failed"
    assert service.close_calls == 2

    next_owner = kronos_lifecycle.KronosForecastOwner({})
    try:
        with pytest.raises(kronos_lifecycle.ActiveForecastUnavailable, match="deadline"), \
                next_owner.lease(time.monotonic() + 0.05):
            pytest.fail("an unconfirmed child must block the next owner")
    finally:
        assert next_owner.close(timeout=2)


def test_owner_retries_transient_cleanup_error_before_releasing_slot(monkeypatch):
    class RetryService(_Service):
        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("temporary cleanup failure")
            self.closed = True

    service = RetryService()
    monkeypatch.setattr(kronos_lifecycle.KronosForecastService, "from_config", lambda _config: service)
    owner = kronos_lifecycle.KronosForecastOwner({})
    with owner.lease(None):
        pass
    assert owner.close(timeout=2)
    assert owner.cleanup_state() == "complete"
    assert service.close_calls == 2

    next_owner = kronos_lifecycle.KronosForecastOwner({})
    try:
        with next_owner.lease(time.monotonic() + 1):
            pass
    finally:
        assert next_owner.close(timeout=2)
