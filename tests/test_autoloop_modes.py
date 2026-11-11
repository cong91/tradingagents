"""Isolated mode, scheduling, and dispatch authorization tests."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import anyio
import pytest

from server import (
    autoloop,
    autoloop_analysis,
    autoloop_api,
    autoloop_candidates,
    autoloop_execution,
    autoloop_futures_context,
    autoloop_futures_execution,
    futures_cycle_telemetry,
    job_store,
)
from server.auto_modes import MODE_KEYS, AutoMode
from server.contract import ApiError
from tests.server_api_fixtures import _no_network as _no_network
from tradingagents.dataflows.config import set_config
from tradingagents.execution import futures_signals
from tradingagents.execution.auto_authorization import capture_process_config, mode_update_lock
from tradingagents.execution.daily import DailyResult

pytestmark = pytest.mark.unit


@pytest.fixture
def mode_state(monkeypatch):
    config = {"auto_spot_enabled": False, "auto_futures_enabled": False}
    persisted = {"enabled": False, "interval_minutes": 30, "updated_at": "old"}
    writes = []
    starts = []
    wakeup = threading.Event()
    monkeypatch.setattr(autoloop, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop_execution, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop.control_store, "load_autoloop_state", lambda: dict(persisted))

    def save(state):
        persisted.clear()
        persisted.update(state)
        writes.append(dict(state))

    monkeypatch.setattr(autoloop.control_store, "save_autoloop_state", save)
    monkeypatch.setattr(autoloop, "ensure_started", lambda: starts.append(True))
    monkeypatch.setattr(autoloop, "_wakeup", wakeup)
    monkeypatch.setattr(job_store, "list_futures_cycles", lambda **_kwargs: [])
    monkeypatch.setattr(autoloop, "_last_cycle", None)
    monkeypatch.setattr(autoloop, "_last_telemetry", None)
    return config, persisted, writes, starts, wakeup


@pytest.mark.parametrize("spot,futures", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("persisted_enabled", [False, True])
def test_load_state_and_payload_derive_enabled_from_modes(mode_state, spot, futures, persisted_enabled):
    config, persisted, _writes, _starts, _wakeup = mode_state
    config.update(auto_spot_enabled=spot, auto_futures_enabled=futures)
    persisted["enabled"] = persisted_enabled

    state = autoloop._load_state()
    payload = autoloop_api._payload()

    expected = spot or futures
    assert state["enabled"] is expected
    assert payload["enabled"] is expected
    assert payload["modes"] == {
        "auto_spot": spot, "auto_futures": futures, "any_auto_enabled": expected,
    }
    assert payload["interval_minutes"] == 30


def test_sync_mode_state_persists_changes_starts_and_wakes(mode_state, monkeypatch):
    config, persisted, writes, starts, wakeup = mode_state
    clock = iter(["enabled-at", "disabled-at"])
    monkeypatch.setattr(autoloop, "utc_now_iso", lambda: next(clock))

    config["auto_spot_enabled"] = True
    assert autoloop.sync_mode_state() == {
        "enabled": True, "interval_minutes": 30, "updated_at": "enabled-at",
    }
    assert starts == [True]
    assert wakeup.is_set()
    wakeup.clear()

    config.update(auto_spot_enabled=False, auto_futures_enabled=True)
    autoloop.sync_mode_state()
    assert len(writes) == 1
    assert starts == [True, True]
    assert wakeup.is_set()
    wakeup.clear()

    config["auto_futures_enabled"] = False
    assert autoloop.sync_mode_state() == {
        "enabled": False, "interval_minutes": 30, "updated_at": "disabled-at",
    }
    assert persisted["enabled"] is False
    assert len(writes) == 2
    assert starts == [True, True]
    assert wakeup.is_set()


def test_unreadable_scheduler_store_remains_fail_closed(mode_state, monkeypatch):
    config, _persisted, _writes, _starts, _wakeup = mode_state
    config["auto_spot_enabled"] = True

    def unreadable():
        raise OSError("store unavailable")

    monkeypatch.setattr(autoloop.control_store, "load_autoloop_state", unreadable)
    assert autoloop._load_state()["enabled"] is False
    assert autoloop_api._payload()["enabled"] is False
    with pytest.raises(OSError, match="store unavailable"):
        autoloop.sync_mode_state()


def test_interval_update_cannot_disable_an_enabled_mode(mode_state, monkeypatch):
    config, persisted, _writes, starts, _wakeup = mode_state
    config["auto_spot_enabled"] = True
    monkeypatch.setattr(autoloop.audit_log, "record", lambda *_args, **_kwargs: None)

    async def body(_request):
        return {"enabled": False, "interval_minutes": 45}

    monkeypatch.setattr(autoloop_api, 'json_body', body)
    response = anyio.run(autoloop_api.set_autoloop, None)

    assert response["enabled"] is True
    assert persisted["enabled"] is True
    assert persisted["interval_minutes"] == 45
    assert starts == [True]


def test_run_now_requires_enabled_mode(mode_state, monkeypatch):
    config, _persisted, _writes, _starts, _wakeup = mode_state
    calls = []
    monkeypatch.setattr(autoloop, "_spawn_cycle", lambda: calls.append(True) or "started")
    monkeypatch.setattr(autoloop.runs_mod, "active_run_id", lambda: None)
    monkeypatch.setattr(autoloop.daily_mod, "active_job_id", lambda: None)

    async def body(_request):
        return {}

    monkeypatch.setattr(autoloop_api, 'json_body', body)
    with pytest.raises(ApiError) as closed:
        anyio.run(autoloop_api.run_autoloop_now, None)
    config["auto_futures_enabled"] = True
    opened = anyio.run(autoloop_api.run_autoloop_now, None)

    assert closed.value.status_code == 409
    assert closed.value.code == "mode_closed"
    assert opened["status"] == "started"
    assert calls == [True]


def _install_cycle_stubs(monkeypatch, config):
    calls = []

    class Authorization:
        def allows(self, mode, *, live=False):
            return config.get(MODE_KEYS[mode]) is True

    monkeypatch.setattr(autoloop, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop_execution, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop, "capture_process_config", lambda: (dict(config), Authorization()))
    monkeypatch.setattr(autoloop.audit_log, "record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autoloop.runs_mod, "active_run_id", lambda: None)
    monkeypatch.setattr(autoloop.daily_mod, "active_job_id", lambda: None)
    monkeypatch.setattr(autoloop, "_last_cycle", None)
    monkeypatch.setattr(autoloop, "_last_telemetry", None)
    monkeypatch.setattr(autoloop, "_build_bridge", lambda _config: object())
    executor = SimpleNamespace(_bridge=object())
    monkeypatch.setattr(autoloop, "_build_derivatives_executor", lambda _config: executor)
    monkeypatch.setattr(autoloop_candidates, '_futures_crypto_universe', lambda *_args: lambda *_args: [])
    monkeypatch.setattr(autoloop_candidates, '_pinning_screen', lambda screen, *_args, **_kwargs: screen)

    def screen(*_args, mode, timeout_seconds, **_kwargs):
        calls.append(("screen", mode, timeout_seconds))
        return [], [], [{"ticker": f"{mode.value}-ticker", "asset_type": "crypto"}]

    def select(candidates, derivatives, **_kwargs):
        return candidates, [], None

    def candidate(_bridge, candidate, _config, _builder, _derivatives,
                  deadline, cancelled, _lock, telemetry, *, mode, authorization):
        calls.append(("candidate", mode, deadline, cancelled, telemetry, authorization))
        return DailyResult(ticker=candidate["ticker"], signal="Hold")

    monkeypatch.setattr(autoloop_candidates, '_screen_with_timeout', screen)
    monkeypatch.setattr(autoloop_candidates, '_select_cycle_candidates', select)
    monkeypatch.setattr(autoloop, "_run_candidate_with_timeout", candidate)
    return calls


@pytest.mark.parametrize(
    "spot,futures,expected",
    [
        (False, False, []),
        (True, False, [AutoMode.SPOT]),
        (False, True, [AutoMode.FUTURES]),
        (True, True, [AutoMode.SPOT, AutoMode.FUTURES]),
    ],
)
def test_cycle_dispatches_only_enabled_modes_in_order(monkeypatch, spot, futures, expected):
    config = {"auto_spot_enabled": spot, "auto_futures_enabled": futures}
    calls = _install_cycle_stubs(monkeypatch, config)

    autoloop._run_cycle_locked(lambda *_args: None)

    assert [call[1] for call in calls if call[0] == "screen"] == expected
    candidates = [call for call in calls if call[0] == "candidate"]
    assert [call[1] for call in candidates] == expected
    if len(candidates) == 2:
        assert candidates[0][2] == candidates[1][2]
        assert candidates[0][3] is not candidates[1][3]
        assert candidates[0][4] is not candidates[1][4]
        assert candidates[0][5] is candidates[1][5]
    summary = autoloop._last_cycle
    assert summary["status"] == "completed"
    for mode in AutoMode:
        item = summary["modes"][f"auto_{mode.value}"]
        assert item["status"] == ("completed" if mode in expected else "disabled")
        assert len(item["results"]) == int(mode in expected)


def test_timeout_in_spot_does_not_cancel_futures_mode(monkeypatch):
    config = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    calls = _install_cycle_stubs(monkeypatch, config)
    original = autoloop._run_candidate_with_timeout

    def candidate(*args, mode, **kwargs):
        if mode is AutoMode.SPOT:
            args[6].set()
            raise TimeoutError("spot timed out")
        assert not args[6].is_set()
        return original(*args, mode=mode, **kwargs)

    monkeypatch.setattr(autoloop, "_run_candidate_with_timeout", candidate)
    autoloop._run_cycle_locked(lambda *_args: None)

    assert [call[1] for call in calls if call[0] == "screen"] == [AutoMode.SPOT, AutoMode.FUTURES]
    assert autoloop._last_cycle["modes"]["auto_spot"]["status"] == "timeout"
    assert autoloop._last_cycle["modes"]["auto_futures"]["status"] == "completed"


def test_pending_mode_reports_timeout_when_shared_deadline_expires(monkeypatch):
    config = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    calls = _install_cycle_stubs(monkeypatch, config)
    monkeypatch.setattr(autoloop_execution, 'CYCLE_TIMEOUT_SECONDS', 1.0)
    clock = [10.0]
    monkeypatch.setattr(autoloop.time, "monotonic", lambda: clock[0])
    original = autoloop._run_candidate_with_timeout

    def candidate(*args, mode, **kwargs):
        result = original(*args, mode=mode, **kwargs)
        clock[0] = 12.0
        return result

    monkeypatch.setattr(autoloop, "_run_candidate_with_timeout", candidate)
    autoloop._run_cycle_locked(lambda *_args: None)

    assert [call[1] for call in calls if call[0] == "screen"] == [AutoMode.SPOT]
    assert autoloop._last_cycle["status"] == "timeout"
    assert autoloop._last_cycle["modes"]["auto_futures"]["status"] == "timeout"


def test_futures_disabled_during_venue_setup_never_screens(monkeypatch):
    config = {"auto_futures_enabled": True}
    calls = _install_cycle_stubs(monkeypatch, config)

    def build(_config):
        config["auto_futures_enabled"] = False
        return SimpleNamespace(_bridge=object())

    monkeypatch.setattr(autoloop, "_build_derivatives_executor", build)
    autoloop._run_cycle_locked(lambda *_args: None)

    assert not calls
    assert autoloop._last_cycle["modes"]["auto_futures"]["status"] == "disabled"


def test_mode_disabled_during_graph_build_never_propagates(monkeypatch):
    config = {"auto_spot_enabled": True}
    monkeypatch.setattr(autoloop, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop_execution, "get_config", lambda: dict(config))
    propagated = []

    def build(*_args):
        config["auto_spot_enabled"] = False
        return SimpleNamespace(propagate=lambda *_args, **_kwargs: propagated.append(True))

    with pytest.raises(autoloop_execution.AutoModeDisabled, match="before analysis"):
        autoloop_analysis._process_candidate(
            object(), {"ticker": "BTC-USD", "asset_type": "crypto"}, config, build,
            mode=AutoMode.SPOT,
        )
    assert propagated == []


def test_futures_disable_after_portfolio_sync_stops_planning(monkeypatch):
    config = {"auto_futures_enabled": True}
    monkeypatch.setattr(autoloop, "get_config", lambda: dict(config))
    monkeypatch.setattr(autoloop_execution, "get_config", lambda: dict(config))
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autoloop_futures_context, '_unified_mark_price', lambda *_args: 100.0)
    monkeypatch.setattr(futures_signals.FuturesSignalEnvelope, "create", lambda **_kwargs: None)
    now_ms = int(time.time() * 1000)
    one = SimpleNamespace(open_time=now_ms - 3_600_000, close_time=now_ms - 1)
    four = SimpleNamespace(open_time=now_ms - 14_400_000, close_time=now_ms - 1)
    context = {
        "unified_symbol": "BTC/USDT:USDT", "one_hour": one, "four_hour": four,
        "one_hour_direction": "bullish", "four_hour_direction": "bullish",
    }
    planned = []

    def sync():
        config["auto_futures_enabled"] = False
        return object()

    executor = SimpleNamespace(
        _bridge=SimpleNamespace(sync_portfolio=sync), risk_guard_decisions=[],
        plan_order=lambda *_args, **_kwargs: planned.append(True),
    )
    result = DailyResult(ticker="BTC-USD")
    proposal = {
        "margin_mode": "isolated", "leverage": 2, "risk_fraction": 0.01,
        "margin_fraction": 0.25, "rationale": "Aligned signals with bounded risk.",
    }
    with pytest.raises(autoloop_execution.AutoModeDisabled, match="before futures planning"):
        autoloop_futures_execution._process_futures_candidate(
            executor, "BTC-USD", "Buy", config, result, context=context, proposal=proposal,
            brackets={"long": (90.0, 110.0), "short": (110.0, 90.0)}, intent="enter",
            cycle_id="test-cycle", cycle_payload={"execution_status": "in_progress"},
            mode=AutoMode.FUTURES,
        )
    assert planned == []


def test_execution_guard_refuses_when_mode_is_disabled(monkeypatch):
    monkeypatch.setattr(autoloop, "get_config", lambda: {"auto_spot_enabled": False})
    submitted = []
    with pytest.raises(autoloop_execution.AutoModeDisabled, match="before execution"):
        autoloop_execution.execute_with_deadline(
            lambda: submitted.append(True), None, None, threading.Lock(), AutoMode.SPOT,
        )
    assert submitted == []


def _set_auto_modes(**values):
    with mode_update_lock():
        set_config(values)


def _reenable_mode(mode):
    key = MODE_KEYS[mode]
    _set_auto_modes(**{key: False})
    _set_auto_modes(**{key: True})


@pytest.mark.parametrize("changed", list(AutoMode))
def test_mode_generation_change_during_screening_stops_only_old_mode(monkeypatch, changed):
    config = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    _set_auto_modes(**config)
    calls = _install_cycle_stubs(monkeypatch, config)
    monkeypatch.setattr(autoloop, "capture_process_config", capture_process_config)
    first_screen = autoloop_candidates._screen_with_timeout

    def screen(*args, mode, **kwargs):
        result = first_screen(*args, mode=mode, **kwargs)
        if mode is AutoMode.SPOT:
            _reenable_mode(changed)
        return result

    monkeypatch.setattr(autoloop_candidates, '_screen_with_timeout', screen)
    autoloop._run_cycle_locked(lambda *_args: None)

    unaffected = AutoMode.FUTURES if changed is AutoMode.SPOT else AutoMode.SPOT
    assert [call[1] for call in calls if call[0] == "candidate"] == [unaffected]
    assert autoloop._last_cycle["modes"][f"auto_{changed.value}"]["status"] == "disabled"
    assert autoloop._last_cycle["modes"][f"auto_{unaffected.value}"]["status"] == "completed"


def test_graph_callback_uses_captured_generation(monkeypatch):
    _set_auto_modes(auto_spot_enabled=True)
    config, authorization = capture_process_config()
    steps = []

    class Graph:
        def propagate(self, *_args, **_kwargs):
            pytest.fail("telemetry graph path expected")

        def propagate_with_telemetry(self, *_args, telemetry, check_cancelled, **_kwargs):
            check_cancelled()
            steps.append("first")
            _reenable_mode(AutoMode.SPOT)
            check_cancelled()
            steps.append("second")
            return {}, "Hold"

    with pytest.raises(autoloop_execution.AutoModeDisabled):
        autoloop_analysis._process_candidate(
            object(), {"ticker": "BTC-USD", "asset_type": "crypto"}, config,
            lambda *_args: Graph(), mode=AutoMode.SPOT, authorization=authorization,
        )
    assert steps == ["first"]


@pytest.mark.parametrize("mode", list(AutoMode))
def test_execution_guard_rejects_reenabled_generation(mode):
    _set_auto_modes(**{MODE_KEYS[mode]: True})
    _, authorization = capture_process_config()
    _reenable_mode(mode)
    submitted = []
    with pytest.raises(autoloop_execution.AutoModeDisabled):
        autoloop_execution.execute_with_deadline(
            lambda: submitted.append(True), None, None, threading.Lock(), mode, authorization,
        )
    assert submitted == []


def test_real_builders_receive_the_original_token(monkeypatch):
    from tradingagents.execution import bridge, derivatives

    _set_auto_modes(auto_spot_enabled=True, auto_futures_enabled=True)
    config, authorization = capture_process_config()
    constructed = []

    def build(**kwargs):
        constructed.append(kwargs)
        return object()

    monkeypatch.setattr(bridge, "ExchangeBridge", build)
    monkeypatch.setattr(derivatives, "DerivativesExecutor", build)
    autoloop._call_authorized_builder(autoloop._build_bridge, config, authorization)
    autoloop._call_authorized_builder(autoloop._build_derivatives_executor, config, authorization)

    assert len(constructed) == 2
    assert all(item["config"] is config and item["authorization"] is authorization for item in constructed)
