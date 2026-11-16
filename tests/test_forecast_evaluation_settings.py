"""Isolated settings-write tests for the forecast evaluation worker."""

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from server import audit, control_store, forecast_evaluation, forecast_evaluation_store, settings
from server.contract import install_error_handlers
from tests.server_api_fixtures import _no_network as _no_network
from tradingagents.dataflows.config import get_config, set_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_worker(monkeypatch):
    monkeypatch.setattr(forecast_evaluation, "_stop_event", threading.Event())
    monkeypatch.setattr(forecast_evaluation, "_worker_thread", None)
    monkeypatch.setattr(forecast_evaluation, "_runtime_enabled", None)
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "existing")
    monkeypatch.setattr(forecast_evaluation, "poll_once", lambda: None)
    yield
    forecast_evaluation._stop_event.set()
    worker = forecast_evaluation._worker_thread
    if worker is not None:
        worker.join(timeout=2)
        assert not worker.is_alive()


def test_concurrent_settings_writes_persist_and_apply_in_order(monkeypatch):
    set_config({"max_debate_rounds": 0})
    entered = threading.Event()
    release = threading.Event()
    second_attempted = threading.Event()
    second_done = threading.Event()
    persisted = {}
    events = []
    errors = []
    real_set_config = set_config

    def persist(change):
        value = change["max_debate_rounds"]
        events.append(("persist", value))
        if value == 1:
            entered.set()
            release.wait(timeout=2)
        persisted.update(change)

    def apply(change):
        events.append(("apply", change["max_debate_rounds"]))
        real_set_config(change)

    def change(value, done, attempted=None):
        try:
            if attempted is not None:
                attempted.set()
            settings._apply_settings_change({"max_debate_rounds": value})
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(control_store, "set_engine_settings", persist)
    monkeypatch.setattr(settings, "set_config", apply)
    first = threading.Thread(target=change, args=(1, threading.Event()))
    second = threading.Thread(target=change, args=(2, second_done, second_attempted))
    first.start()
    try:
        assert entered.wait(timeout=2)
        second.start()
        assert second_attempted.wait(timeout=2)
        completed_early = second_done.wait(timeout=0.1)
    finally:
        release.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
    assert not errors
    assert not completed_early
    assert not first.is_alive() and not second.is_alive()
    assert events == [("persist", 1), ("apply", 1), ("persist", 2), ("apply", 2)]
    assert persisted["max_debate_rounds"] == get_config()["max_debate_rounds"] == 2


def test_failed_disable_then_reenable_resets_cutover(monkeypatch):
    set_config({"forecast_evaluation_enabled": True})
    resets = []
    saved = []
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **kwargs: resets.append(kwargs),
    )

    def persist(change):
        if change.get("forecast_evaluation_enabled") is False:
            raise OSError("database unavailable")
        saved.append(change.copy())

    monkeypatch.setattr(control_store, "set_engine_settings", persist)
    forecast_evaluation.start()
    with pytest.raises(OSError, match="database unavailable"):
        settings._apply_settings_change({"forecast_evaluation_enabled": False})
    assert get_config()["forecast_evaluation_enabled"] is True
    assert forecast_evaluation._enabled() is False

    settings._apply_settings_change({"forecast_evaluation_enabled": True})

    assert resets == [{"reset": True}]
    assert saved == [{"forecast_evaluation_enabled": True}]
    assert get_config()["forecast_evaluation_enabled"] is True
    assert forecast_evaluation._enabled() is True
    assert forecast_evaluation._worker_thread.is_alive()


def test_failed_cutover_reset_keeps_worker_disabled_for_retry(monkeypatch):
    set_config({"forecast_evaluation_enabled": False})
    saved = []
    monkeypatch.setattr(control_store, "set_engine_settings", lambda change: saved.append(change.copy()))

    def fail_reset(**_kwargs):
        raise OSError("cutover unavailable")

    monkeypatch.setattr(forecast_evaluation_store, "initialize_cursor", fail_reset)
    with pytest.raises(OSError, match="cutover unavailable"):
        settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert saved == [], "a failed cutover reset must not persist an enabled worker"
    assert get_config()["forecast_evaluation_enabled"] is False
    assert forecast_evaluation._enabled() is False
    assert forecast_evaluation._worker_thread is None

    resets = []
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **kwargs: resets.append(kwargs),
    )
    settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert resets == [{"reset": True}]
    assert forecast_evaluation._worker_thread.is_alive()


def test_failed_enable_persistence_does_not_start_worker_and_retry_resets_cutover(monkeypatch):
    set_config({"forecast_evaluation_enabled": False})
    resets = []
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **kwargs: resets.append(kwargs),
    )

    def fail_persist(_change):
        raise OSError("settings unavailable")

    monkeypatch.setattr(control_store, "set_engine_settings", fail_persist)
    with pytest.raises(OSError, match="settings unavailable"):
        settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert get_config()["forecast_evaluation_enabled"] is False
    assert forecast_evaluation._enabled() is False
    assert forecast_evaluation._worker_thread is None
    assert resets == [{"reset": True}]

    monkeypatch.setattr(control_store, "set_engine_settings", lambda _change: None)
    settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert resets == [{"reset": True}, {"reset": True}]
    assert forecast_evaluation._worker_thread.is_alive()


def test_failed_reenable_persistence_keeps_fence_and_resets_on_retry(monkeypatch):
    set_config({"forecast_evaluation_enabled": True})
    monkeypatch.setattr(forecast_evaluation, "_runtime_enabled", False)
    forecast_evaluation._stop_event.set()
    resets = []
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **kwargs: resets.append(kwargs),
    )

    def fail_persist(_change):
        raise OSError("settings unavailable")

    monkeypatch.setattr(control_store, "set_engine_settings", fail_persist)
    with pytest.raises(OSError, match="settings unavailable"):
        settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert forecast_evaluation._enabled() is False
    assert forecast_evaluation._worker_thread is None

    monkeypatch.setattr(control_store, "set_engine_settings", lambda _change: None)
    settings._apply_settings_change({"forecast_evaluation_enabled": True})
    assert resets == [{"reset": True}, {"reset": True}]
    assert forecast_evaluation._worker_thread.is_alive()


def test_repeated_enable_does_not_reset_running_cutover(monkeypatch):
    set_config({"forecast_evaluation_enabled": True})
    monkeypatch.setattr(control_store, "set_engine_settings", lambda _change: None)
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor",
        lambda **_kwargs: pytest.fail("an already enabled worker must not reset cutover"),
    )
    forecast_evaluation.start()
    original_worker = forecast_evaluation._worker_thread

    settings._apply_settings_change({"forecast_evaluation_enabled": True})

    assert forecast_evaluation._worker_thread is original_worker
    assert original_worker.is_alive()


def test_asgi_loop_remains_responsive_while_disable_waits_for_poll(monkeypatch):
    set_config({"forecast_evaluation_enabled": True})
    monkeypatch.setattr(control_store, "set_engine_settings", lambda _change: None)
    monkeypatch.setattr(audit, "record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(settings, "_settings_payload", lambda: {"ok": True})
    paused = threading.Event()
    original_pause = forecast_evaluation.pause

    def pause():
        paused.set()
        original_pause()

    monkeypatch.setattr(forecast_evaluation, "pause", pause)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(settings.router)

    poll_entered = threading.Event()
    release_poll = threading.Event()

    def active_poll():
        with forecast_evaluation._poll_lock:
            poll_entered.set()
            release_poll.wait(timeout=3)

    poll_thread = threading.Thread(target=active_poll)
    poll_thread.start()
    try:
        assert poll_entered.wait(timeout=2)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                pending = asyncio.create_task(client.put(
                    "/api/settings", json={"settings": {"forecast_evaluation_enabled": False}},
                ))
                try:
                    assert await asyncio.to_thread(paused.wait, 2)
                    response = await asyncio.wait_for(client.get("/api/settings"), timeout=1)
                    assert response.status_code == 200
                    assert response.json() == {"ok": True}
                    assert not pending.done()
                finally:
                    release_poll.set()
                response = await asyncio.wait_for(pending, timeout=2)
                assert response.status_code == 200
                assert get_config()["forecast_evaluation_enabled"] is False

        asyncio.run(exercise())
    finally:
        release_poll.set()
        poll_thread.join(timeout=2)
    assert not poll_thread.is_alive()
