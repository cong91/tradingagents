"""test autoloop scheduler (due-ledger cadence)."""

import threading

import pytest

from server import (
    autoloop as autoloop_mod,
    autoloop_schedule,
)
from server.auto_modes import AutoMode
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
        lambda: {"enabled": False, "mode_schedule": {}},
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
        return {"enabled": True, "mode_schedule": {}, "updated_at": f"u{len(reads)}"}

    class Wakeup:
        def wait(self, timeout=None):
            return True

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_load_state", load_state)
    monkeypatch.setattr(autoloop_mod, "_spawn_due_cycle",
                        lambda last_scheduled: spawns.append(1) or "auto_spot")
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    autoloop_mod._scheduler_loop()

    assert spawns == [1]


def test_scheduler_loop_runs_enabled_state_immediately(monkeypatch, tmp_path):
    set_config({"auto_spot_enabled": True})
    autoloop_mod._save_state({"enabled": True, "updated_at": utc_now_iso()})
    calls: list[int] = []
    stop = threading.Event()

    def spawn(last_scheduled):
        calls.append(1)
        set_config({"auto_spot_enabled": False})
        autoloop_mod._save_state({"enabled": False})
        return "auto_spot"

    monkeypatch.setattr(autoloop_mod, "_spawn_due_cycle", spawn)
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


def test_scheduler_respawns_after_boundary_timeout_only_when_due(monkeypatch):
    """A boundary timeout wakes the loop, but nothing spawns unless a mode is
    actually due: the markers already cover the served candle."""
    set_config({"auto_spot_enabled": True, "auto_spot_timeframe": "1m"})
    autoloop_mod._save_state({"enabled": True, "updated_at": "initial"})
    spawned: list[int] = []
    due_calls: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits <= 2:
                return False  # boundary timeout, nothing changed
            raise StopScheduler

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    def fake_due(_config, _markers, _now):
        due_calls.append(1)
        # Loop top: due. First timeout: served, not due. Second timeout: a new
        # candle closed, due again.
        if len(due_calls) in (1, 3):
            return {AutoMode.SPOT: {"timeframe": "1m", "candle_open": 0}}
        return {}

    monkeypatch.setattr(autoloop_mod.autoloop_schedule, "due_modes", fake_due)
    monkeypatch.setattr(
        autoloop_mod, "_spawn_cycle",
        lambda *args, **kwargs: spawned.append(1) or "started",
    )

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert len(due_calls) == 3
    assert spawned == [1, 1]


def test_spawn_due_cycle_passes_exactly_one_mode(monkeypatch):
    """When both modes are due at the same boundary, the scheduled spawn
    carries ONLY the picked mode — the other stays due for the next cycle
    (one mode per cycle, each with its own full budget)."""
    set_config({
        "auto_spot_enabled": True, "auto_futures_enabled": True,
        "auto_spot_timeframe": "1h", "auto_futures_timeframe": "1h",
    })
    captured: list = []

    def fake_spawn(due=None, graph_builder=None):
        captured.append(dict(due or {}))
        return "started"

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", fake_spawn)

    last = autoloop_mod._spawn_due_cycle(None)
    assert last in ("auto_spot", "auto_futures")
    (due_arg,) = captured
    assert list(due_arg) == [last.replace("auto_", "")]  # exactly one mode key
    # The tie-break alternates: the next pick is the other mode.
    other = autoloop_mod._spawn_due_cycle(last)
    assert other != last
    (due_arg,) = captured[-1:]
    assert list(due_arg) == [other.replace("auto_", "")]


def test_wakeup_without_state_change_recomputes_due(monkeypatch):
    """An explicit wake (timeframe change or a finished cycle re-arming a
    busy-skipped mode) spawns due work at once — it must not wait for the
    next boundary timeout. The due answer flips only on the wake path, so a
    pre-fix loop (which spawned on timeouts only) can never produce this
    spawn: the top-of-loop call saw an empty due set."""
    set_config({"auto_spot_enabled": True, "auto_spot_timeframe": "4h"})
    spawns: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return True  # event wake, no timeout
            raise StopScheduler

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())
    monkeypatch.setattr(
        autoloop_mod, "_load_state",
        lambda: {"enabled": True, "updated_at": "initial"},
    )
    due_calls: list[int] = []

    def fake_due(_config, _markers, _now):
        due_calls.append(1)
        # Top-of-loop evaluation (call 1): nothing due. The post-wait
        # evaluation (call 2): the timeframe change made the current candle
        # due — only a loop that recomputes on the event wake itself ever
        # makes this second call before any timeout.
        if len(due_calls) == 1:
            return {}
        return {AutoMode.SPOT: {"timeframe": "4h", "candle_open": 0}}

    monkeypatch.setattr(autoloop_mod.autoloop_schedule, "due_modes", fake_due)
    monkeypatch.setattr(
        autoloop_mod, "_spawn_cycle",
        lambda *args, **kwargs: spawns.append(1) or "started",
    )

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert spawns == [1]  # spawned on the event wake itself, before any timeout


def test_busy_skipped_cycle_does_not_rearm_the_scheduler(monkeypatch):
    """A skipped_busy cycle consumed no boundary and another worker still
    holds the pipeline: re-arming immediately would hot loop (spawn → skip →
    audit row) until that work ends. The guard lives in _run_cycle_locked's
    finally, so this test runs the REAL spawn path (only _run_cycle is a
    no-op) and pins both directions: skipped_busy never wakes, completed
    wakes exactly once."""
    set_config({"auto_spot_enabled": True})
    wakes: list[int] = []
    finished = threading.Event()

    class Wakeup:
        def wait(self, timeout=None):
            raise AssertionError("the guard test must not enter the scheduler loop")

        def clear(self):
            pass

        def set(self):
            wakes.append(1)

    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    def fake_run_cycle(*args, **kwargs):
        finished.set()
        return []

    monkeypatch.setattr(autoloop_mod, "_run_cycle", fake_run_cycle)

    def scenario(status: str) -> None:
        finished.clear()
        monkeypatch.setattr(
            autoloop_mod, "_last_cycle",
            {"status": status, "reason": "test"} if status else None,
        )
        assert autoloop_mod._spawn_cycle() == "started"
        finished.wait(timeout=2.0)
        thread = autoloop_mod._cycle_thread
        if thread is not None:
            thread.join(timeout=2.0)
        assert not thread.is_alive()

    # skipped_busy consumed no boundary: the finally must NOT re-arm.
    scenario("skipped_busy")
    assert not wakes

    # A completed cycle re-arms the scheduler (the wake is a no-op for due
    # work because markers consumed the boundary, but it must still fire).
    scenario("completed")
    assert wakes == [1]


def test_mode_dispatch_marker_is_written_when_the_mode_starts(
    tmp_path, monkeypatch, postgres_schema,
):
    """The per-timeframe marker lands only when the worker actually starts the
    mode, so a busy-skip or a deadline expiry never consumes a boundary."""
    set_config({"auto_spot_enabled": True, "auto_futures_enabled": False})
    autoloop_mod._save_state({"enabled": True, "updated_at": utc_now_iso()})
    results = autoloop_mod._run_cycle(
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    )
    del results
    markers = autoloop_mod._load_mode_schedule()
    served = markers.get("auto_spot") or {}
    assert served, "starting the mode must record its boundary"
    assert set(served) == {"4h"}  # default timeframe marker key
    assert not markers.get("auto_futures")


def test_record_mode_dispatch_keeps_per_timeframe_markers(monkeypatch):
    """A 4h→1m→4h round trip must not lose the served 4h boundary."""
    state: dict = {}
    monkeypatch.setattr(
        autoloop_mod.control_store, "load_autoloop_state", lambda: dict(state)
    )

    def save(payload):
        state.clear()
        state.update(payload)

    monkeypatch.setattr(autoloop_mod.control_store, "save_autoloop_state", save)
    autoloop_mod._record_mode_dispatch({}, AutoMode.FUTURES)
    autoloop_mod._record_mode_dispatch({"auto_futures_timeframe": "1m"}, AutoMode.FUTURES)
    autoloop_mod._record_mode_dispatch({}, AutoMode.FUTURES)

    served = state[autoloop_schedule.MODE_SCHEDULE_KEY]["auto_futures"]
    assert set(served) == {"4h", "1m"}
