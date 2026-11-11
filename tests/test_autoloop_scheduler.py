"""test autoloop scheduler."""

import threading

import pytest

from server import (
    autoloop as autoloop_mod,
)
from server.contract import utc_now_iso
from tests.autoloop_runtime_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


def test_ensure_started_reuses_a_live_scheduler(monkeypatch):
    class _Alive:
        def is_alive(self) -> bool:
            return True

    sentinel = _Alive()
    monkeypatch.setattr(autoloop_mod, "_scheduler_thread", sentinel)
    autoloop_mod.ensure_started()
    autoloop_mod.ensure_started()
    assert autoloop_mod._scheduler_thread is sentinel  # idempotent: never a second thread


def test_stop_joins_the_autoloop_scheduler(monkeypatch):
    monkeypatch.setattr(
        autoloop_mod,
        "_load_state",
        lambda: {"enabled": False, "interval_minutes": 1440},
    )
    autoloop_mod._wakeup.clear()

    autoloop_mod.ensure_started()
    scheduler = autoloop_mod._scheduler_thread
    assert scheduler is not None and scheduler.is_alive()

    autoloop_mod.stop()

    assert not scheduler.is_alive()
    assert autoloop_mod._scheduler_thread is None


def test_scheduler_stop_during_state_read_launches_no_new_cycle(monkeypatch):
    """A stop arriving between the loop-top check and the spawn decision must
    not launch one final cycle: shutdown never arms new work."""
    reads: list[int] = []
    spawns: list[int] = []

    def load_state():
        reads.append(1)
        if len(reads) >= 2:
            autoloop_mod._scheduler_stop.set()
        return {"enabled": True, "interval_minutes": 1440, "updated_at": f"u{len(reads)}"}

    class Wakeup:
        def wait(self, timeout=None):
            return True

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_load_state", load_state)
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle",
                        lambda *_args, **_kwargs: spawns.append(1) or "started")
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    autoloop_mod._scheduler_loop()

    assert spawns == [1]


def test_scheduler_loop_runs_enabled_state_immediately(monkeypatch, tmp_path):
    set_config({"auto_spot_enabled": True})
    autoloop_mod._save_state({"enabled": True, "interval_minutes": 1440, "updated_at": utc_now_iso()})
    calls: list[int] = []
    stop = threading.Event()

    def spawn(*args, **kwargs):
        calls.append(1)
        set_config({"auto_spot_enabled": False})
        autoloop_mod._save_state({"enabled": False, "interval_minutes": 1440})
        return "started"

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", spawn)
    monkeypatch.setattr(autoloop_mod, "_wakeup", stop)
    thread = threading.Thread(
        target=autoloop_mod._scheduler_loop, name="server-autoloop-scheduler", daemon=True
    )
    thread.start()
    try:
        thread.join(timeout=1.0)
        assert calls == [1]
    finally:
        autoloop_mod._scheduler_stop.set()
        stop.set()
        thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_scheduler_runs_again_after_interval_timeout(monkeypatch, tmp_path):
    set_config({"auto_spot_enabled": True})
    autoloop_mod._save_state({
        "enabled": True,
        "interval_minutes": 15,
        "updated_at": "initial",
    })
    calls: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return False
            raise StopScheduler

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *_args, **_kwargs: calls.append(1) or "started")
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert calls == [1, 1]


def test_scheduler_ignores_pending_enable_wakeup_but_runs_changed_state(monkeypatch, tmp_path):
    set_config({"auto_spot_enabled": True})
    autoloop_mod._save_state({
        "enabled": True,
        "interval_minutes": 15,
        "updated_at": "initial",
    })
    calls: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return True
            if self.waits == 2:
                autoloop_mod._save_state({
                    "enabled": True,
                    "interval_minutes": 30,
                    "updated_at": "changed",
                })
                return True
            if self.waits == 3:
                raise StopScheduler
            return False

        def clear(self):
            pass

    def spawn(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 2:
            set_config({"auto_spot_enabled": False})
            autoloop_mod._save_state({
                "enabled": False,
                "interval_minutes": 30,
                "updated_at": "disabled",
            })
        return "started"

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", spawn)
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())
    errors: list[BaseException] = []

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    assert calls == [1, 1]
