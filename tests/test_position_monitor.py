import pytest

from server import job_store, position_monitor
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


@pytest.fixture
def monitor_state(tmp_path, monkeypatch, postgres_schema):
    set_config({"exec_derivatives": False, "exec_live": False})
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_LIVE", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)


def _state(position_key: str):
    assert job_store.persist_futures_entry_state(
        position_key,
        side="long",
        entry=100.0,
        quantity=1.0,
        stop=90.0,
        targets=[130.0],
        mark=100.0,
        order_id=position_key,
    )
    state = job_store.load_position_state(position_key)
    state["bracket_task_id"] = f"bracket:{position_key}:task"
    assert job_store.save_position_state(position_key, state["revision"], state)


def test_no_states_does_not_construct_executor(monitor_state, monkeypatch):
    monkeypatch.setattr(
        position_monitor,
        "_build_executor",
        lambda _config: pytest.fail("executor should not be constructed without states"),
    )

    assert position_monitor.poll_active_positions() == []


def test_mutation_requires_both_derivatives_and_live_gates(monitor_state, monkeypatch):
    _state("BTC/USDT:USDT:buy:entry-1")
    calls = []

    class FakeExecutor:
        def __init__(self, config):
            calls.append(("init", config))
            self._bridge = object()

    monkeypatch.setattr(position_monitor, "_build_executor", FakeExecutor)
    set_config({"exec_derivatives": True, "exec_live": True,
                "risk_derivatives_profit_protection_enabled": True})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "false")

    results = position_monitor.poll_active_positions()

    assert results == [{"position_key": "BTC/USDT:USDT:buy:entry-1", "status": "mutation_gated"}]
    assert calls == []


def test_one_state_failure_does_not_stop_later_state(monitor_state, monkeypatch):
    _state("fails")
    _state("continues")
    set_config({
        "exec_derivatives": True,
        "exec_live": True,
        "risk_derivatives_profit_protection_enabled": True,
    })
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    calls = []

    class FakeExecutor:
        def __init__(self, config):
            self._bridge = object()

    def fake_manage(bridge, *, allow_mutation, config):
        calls.append((allow_mutation, config["risk_derivatives_profit_protection_enabled"]))
        return [
            {"position_key": "fails", "status": "error:RuntimeError:bad venue"},
            {"position_key": "continues", "status": "unchanged"},
        ]

    monkeypatch.setattr(position_monitor, "_build_executor", FakeExecutor)
    monkeypatch.setattr(position_monitor, "manage_active_positions", fake_manage)
    results = position_monitor.poll_active_positions()

    assert results == [
        {"position_key": "fails", "status": "error:RuntimeError:bad venue"},
        {"position_key": "continues", "status": "unchanged"},
    ]
    assert calls
    assert calls == [(True, True)]


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
def lifecycle(monkeypatch):
    event = _FakeEvent()
    threads = []
    monkeypatch.setattr(position_monitor, "_stop_event", event)
    monkeypatch.setattr(position_monitor, "_monitor_thread", None)

    def make_thread(*, target, name, daemon):
        thread = _FakeThread(
            target=target,
            name=name,
            daemon=daemon,
            exits_on_join=bool(getattr(make_thread, "exits_on_join", False)),
        )
        threads.append(thread)
        return thread

    monkeypatch.setattr(position_monitor.threading, "Thread", make_thread)
    return event, threads, make_thread


def test_start_and_stop_are_idempotent(lifecycle):
    event, threads, make_thread = lifecycle
    make_thread.exits_on_join = True

    position_monitor.start()
    position_monitor.start()
    assert len(threads) == 1
    assert threads[0].name == "server-position-monitor"
    assert threads[0].daemon is True

    position_monitor.stop()
    position_monitor.stop()
    assert threads[0].join_timeouts == [position_monitor.MONITOR_INTERVAL_SECONDS + 5]
    assert position_monitor._monitor_thread is None
    assert event.is_set is True
