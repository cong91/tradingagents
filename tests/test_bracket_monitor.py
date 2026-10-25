"""Offline tests for durable bracket monitoring and server lifecycle wiring."""

import os

import pytest

from server import bracket_monitor, job_store
from tradingagents.dataflows.config import set_config
from tradingagents.execution import derivatives as derivatives_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def monitor_state(tmp_path, monkeypatch, postgres_schema):
    set_config({"exec_derivatives": False, "exec_live": False})
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)


def _create_task(task_id: str, status: str = "pending") -> None:
    job_store.create_bracket_recovery(
        task_id,
        symbol="BTC/USDT:USDT",
        entry_order_id=f"entry-{task_id}",
        close_side="sell",
        quantity=0.25,
        legs=[{"kind": "stop", "price": 90.0}],
        now="2026-10-19T00:00:00+00:00",
    )
    if status != "pending":
        job_store.update_bracket_recovery(
            task_id,
            status=status,
            placed_ids=[],
            attempts=0,
            last_error=None,
            now="2026-10-19T00:00:00+00:00",
        )


def _install_executor(monkeypatch, *, failures=None):
    calls = {"configs": [], "recover": [], "monitor": []}
    failures = failures or {}

    class FakeDerivativesExecutor:
        def __init__(self, config):
            self.config = config
            calls["configs"].append(config)

        def effective_mode(self):
            raw = os.environ.get("TRADINGAGENTS_EXEC_LIVE")
            live = self.config.get("exec_live") is True and raw and raw.strip().lower() in {
                "true", "1", "yes", "on",
            }
            return "live" if live else "dry"

        def recover_bracket(self, task_id, *, confirm=True):
            calls["recover"].append((task_id, confirm))
            if task_id in failures:
                raise failures[task_id]
            return f"recovered:{task_id}"

        def monitor_bracket(self, task_id):
            calls["monitor"].append(task_id)
            if task_id in failures:
                raise failures[task_id]
            return f"monitored:{task_id}"

    monkeypatch.setattr(derivatives_mod, "DerivativesExecutor", FakeDerivativesExecutor)
    return calls


def test_no_active_tasks_does_not_construct_executor(monitor_state, monkeypatch):
    monkeypatch.setattr(
        derivatives_mod,
        "DerivativesExecutor",
        lambda **_kwargs: pytest.fail("executor should not be constructed without tasks"),
    )

    assert bracket_monitor.poll_active_brackets() == []


@pytest.mark.parametrize(
    ("config_gate", "derivatives_env", "live_config", "live_env", "expected"),
    [
        (True, "true", True, "true", "recover"),
        (False, "true", True, "true", "monitor"),
        (True, "false", True, "true", "monitor"),
        (True, "true", False, "true", "monitor"),
        (True, "true", True, "false", "monitor"),
    ],
)
def test_pending_recovery_requires_all_live_and_derivatives_gates(
    monitor_state,
    monkeypatch,
    config_gate,
    derivatives_env,
    live_config,
    live_env,
    expected,
):
    _create_task("pending")
    set_config({"exec_derivatives": config_gate, "exec_live": live_config})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", derivatives_env)
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", live_env)
    calls = _install_executor(monkeypatch)

    results = bracket_monitor.poll_active_brackets()

    assert len(calls["configs"]) == 1
    if expected == "recover":
        assert results == [{"task_id": "pending", "status": "recovered:pending"}]
        assert calls["recover"] == [("pending", True)]
        assert calls["monitor"] == []
    else:
        assert results == [{"task_id": "pending", "status": "monitored:pending"}]
        assert calls["recover"] == []
        assert calls["monitor"] == ["pending"]


def test_monitoring_tasks_dispatch_to_monitor_when_live_gates_are_open(
    monitor_state, monkeypatch
):
    _create_task("pending")
    _create_task("already-monitoring", status="monitoring")
    set_config({"exec_derivatives": True, "exec_live": True})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    calls = _install_executor(monkeypatch)

    results = bracket_monitor.poll_active_brackets()

    assert {item["task_id"]: item["status"] for item in results} == {
        "pending": "recovered:pending",
        "already-monitoring": "monitored:already-monitoring",
    }
    assert calls["recover"] == [("pending", True)]
    assert calls["monitor"] == ["already-monitoring"]


def test_uncertain_and_failed_emergency_tasks_are_polled(monitor_state, monkeypatch):
    _create_task("uncertain", status="submission_uncertain")
    _create_task("emergency", status="emergency_close_failed")
    _create_task("cleanup", status="emergency_cleanup_pending")
    calls = _install_executor(monkeypatch)

    results = bracket_monitor.poll_active_brackets()

    assert {item["task_id"] for item in results} == {"uncertain", "emergency", "cleanup"}
    assert set(calls["monitor"]) == {"uncertain", "emergency", "cleanup"}
    assert calls["recover"] == []


def test_task_failure_is_reported_and_does_not_stop_later_tasks(monitor_state, monkeypatch):
    _create_task("fails", status="monitoring")
    _create_task("continues", status="monitoring")
    calls = _install_executor(
        monkeypatch,
        failures={"fails": RuntimeError("venue response unavailable")},
    )

    results = bracket_monitor.poll_active_brackets()

    assert {item["task_id"]: item["status"] for item in results} == {
        "fails": "error:RuntimeError:venue response unavailable",
        "continues": "monitored:continues",
    }
    assert set(calls["monitor"]) == {"fails", "continues"}


class _FakeEvent:
    def __init__(self):
        self.is_set = False
        self.clear_calls = 0
        self.set_calls = 0

    def clear(self):
        self.clear_calls += 1
        self.is_set = False

    def set(self):
        self.set_calls += 1
        self.is_set = True


class _FakeThread:
    def __init__(self, *, target, name, daemon, exits_on_join):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.exits_on_join = exits_on_join
        self.alive = False
        self.start_calls = 0
        self.join_timeouts = []

    def start(self):
        self.start_calls += 1
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        if self.exits_on_join:
            self.alive = False


@pytest.fixture
def monitor_lifecycle(monkeypatch):
    event = _FakeEvent()
    threads = []
    monkeypatch.setattr(bracket_monitor, "_stop_event", event)
    monkeypatch.setattr(bracket_monitor, "_monitor_thread", None)

    def make_thread(*, target, name, daemon):
        thread = _FakeThread(
            target=target,
            name=name,
            daemon=daemon,
            exits_on_join=bool(getattr(make_thread, "exits_on_join", False)),
        )
        threads.append(thread)
        return thread

    monkeypatch.setattr(bracket_monitor.threading, "Thread", make_thread)
    return event, threads, make_thread


def test_start_and_stop_are_idempotent_for_a_worker_that_exits(monitor_lifecycle):
    event, threads, make_thread = monitor_lifecycle
    make_thread.exits_on_join = True

    bracket_monitor.start()
    bracket_monitor.start()
    assert len(threads) == 1
    assert threads[0].start_calls == 1
    assert threads[0].name == "server-bracket-monitor"
    assert threads[0].daemon is True

    bracket_monitor.stop()
    bracket_monitor.stop()

    assert threads[0].join_timeouts == [bracket_monitor.MONITOR_INTERVAL_SECONDS + 5]
    assert bracket_monitor._monitor_thread is None
    assert event.is_set is True


def test_stop_keeps_reference_when_join_times_out(monitor_lifecycle):
    _event, threads, _make_thread = monitor_lifecycle
    bracket_monitor.start()
    worker = threads[0]

    bracket_monitor.stop()

    assert worker.alive is True
    assert bracket_monitor._monitor_thread is worker


def test_second_start_after_join_timeout_does_not_clear_stop_or_replace_worker(
    monitor_lifecycle,
):
    event, threads, _make_thread = monitor_lifecycle
    bracket_monitor.start()
    worker = threads[0]
    clear_count_after_first_start = event.clear_calls

    bracket_monitor.stop()
    bracket_monitor.start()

    assert (event.clear_calls, len(threads), bracket_monitor._monitor_thread) == (
        clear_count_after_first_start,
        1,
        worker,
    )
    assert event.is_set is True


def test_lifespan_starts_and_stops_monitor_with_other_lifecycle_hooks(monkeypatch):
    from server import main

    calls = []
    monkeypatch.setattr(main, "_apply_operator_config", lambda: calls.append("config"))
    monkeypatch.setattr(main, "_open_state_db", lambda: calls.append("database"))
    monkeypatch.setattr(main.watchlist, "ensure_seeded", lambda: calls.append("watchlist"))
    monkeypatch.setattr(main.autoloop, "ensure_started", lambda: calls.append("autoloop"))
    monkeypatch.setattr(main.bracket_monitor, "start", lambda: calls.append("bracket-monitor-start"))
    monkeypatch.setattr(main.bracket_monitor, "stop", lambda: calls.append("bracket-monitor-stop"))
    monkeypatch.setattr(main.position_monitor, "start", lambda: calls.append("position-monitor-start"))
    monkeypatch.setattr(main.position_monitor, "stop", lambda: calls.append("position-monitor-stop"))

    async def exercise_lifespan():
        async with main.lifespan(None):
            calls.append("yield")

    manager = main.lifespan(None)
    enter = manager.__aenter__()
    with pytest.raises(StopIteration):
        enter.send(None)
    calls.append("yield")
    exit_lifespan = manager.__aexit__(None, None, None)
    with pytest.raises(StopIteration):
        exit_lifespan.send(None)

    assert calls == [
        # Migrations first: operator overrides are read from PostgreSQL, so the
        # table must exist before the config is applied.
        "database",
        "config",
        "watchlist",
        "autoloop",
        "bracket-monitor-start",
        "position-monitor-start",
        "yield",
        "position-monitor-stop",
        "bracket-monitor-stop",
    ]
