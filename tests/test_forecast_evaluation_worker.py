"""Poll and lifecycle tests for the forecast evaluation worker."""

import threading
from datetime import UTC, datetime

import pytest

from server import forecast_evaluation, forecast_evaluation_scoring, forecast_evaluation_store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_worker(monkeypatch):
    monkeypatch.setattr(forecast_evaluation, "_stop_event", threading.Event())
    monkeypatch.setattr(forecast_evaluation, "_worker_thread", None)
    monkeypatch.setattr(forecast_evaluation, "_runtime_enabled", None)
    evaluate = forecast_evaluation._evaluate_block
    now = datetime.fromtimestamp((1_800_000_000_000 + 3_600_000) / 1000, UTC)
    monkeypatch.setattr(
        forecast_evaluation, "_evaluate_block",
        lambda cycle, block, *, exchange: evaluate(cycle, block, exchange=exchange, now=now),
    )
    yield
    forecast_evaluation._stop_event.set()
    worker = forecast_evaluation._worker_thread
    if worker is not None:
        worker.join(timeout=2)
        assert not worker.is_alive()


def _block() -> dict:
    opened = 1_800_000_000_000
    return {
        "symbol": "BTC/USDT:USDT",
        "status": "ok",
        "generation_kind": "forward",
        "timeframe": "1h",
        "data_open_time": opened,
        "horizon_bars": 1,
        "horizon_hours": 1,
        "as_of": datetime.fromtimestamp((opened + 3_600_000) / 1000, UTC).isoformat(),
        "created_at": datetime.fromtimestamp((opened + 3_600_001) / 1000, UTC).isoformat(),
        "recorded_at": datetime.fromtimestamp((opened + 3_600_002) / 1000, UTC).isoformat(),
        "reference_close": 100.0,
        "direction": "up",
        "direction_ratio": {"up": 0.8, "down": 0.2, "flat": 0.0},
        "band": {"low": 99.0, "p50": 102.0, "high": 104.0},
    }


def _cycle(*, block: dict | None = None, scope: dict | None = None, updated_at: str = "2026-11-16T00:00:00+00:00"):
    snapshot = block or _block()
    payload = {"kronos_forecast": snapshot}
    if scope is not None:
        payload["kronos_venue_scope"] = scope
    return {
        "cycle_id": "cycle-worker-1",
        "symbol": "BTC/USDT:USDT",
        "cycle_timeframe": snapshot["timeframe"],
        "cycle_selected_open": snapshot["data_open_time"],
        "status": "rejected",
        "phase": "forecast",
        "payload": payload,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def _enable(monkeypatch):
    monkeypatch.setattr(
        forecast_evaluation, "get_config",
        lambda: {"forecast_evaluation_enabled": True, "forecast_evaluation_interval_seconds": 1},
    )
    monkeypatch.setattr(forecast_evaluation, "_runtime_enabled", True)


def test_disabled_poll_does_not_read_store(monkeypatch):
    monkeypatch.setattr(forecast_evaluation, "get_config", lambda: {"forecast_evaluation_enabled": False})
    monkeypatch.setattr(
        forecast_evaluation.forecast_evaluation_store,
        "get_cursor",
        lambda: pytest.fail("disabled worker must not query its store"),
    )
    assert forecast_evaluation.poll_once() == {"status": "disabled"}


def test_busy_poll_does_not_read_store(monkeypatch):
    _enable(monkeypatch)
    assert forecast_evaluation._poll_lock.acquire(blocking=False)
    try:
        monkeypatch.setattr(
            forecast_evaluation.forecast_evaluation_store,
            "get_cursor",
            lambda: pytest.fail("busy worker must not query its store"),
        )
        assert forecast_evaluation.poll_once() == {"status": "busy"}
    finally:
        forecast_evaluation._poll_lock.release()


def test_configure_waits_for_poll_before_disabling(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def active_poll():
        entered.set()
        release.wait(timeout=2)

    poll_thread = threading.Thread(
        target=lambda: (forecast_evaluation._poll_lock.acquire(), active_poll(), forecast_evaluation._poll_lock.release())
    )
    poll_thread.start()
    assert entered.wait(timeout=2)
    done = threading.Event()
    disable_thread = threading.Thread(
        target=lambda: (forecast_evaluation.configure(enabled=False), done.set())
    )
    disable_thread.start()
    assert not done.wait(timeout=0.05)
    release.set()
    poll_thread.join(timeout=2)
    disable_thread.join(timeout=2)
    assert done.is_set()
    assert forecast_evaluation._enabled() is False
    forecast_evaluation.configure(enabled=True, reset_cursor=False)


def test_pause_timeout_fences_worker_until_inflight_poll_exits(monkeypatch):
    _enable(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(forecast_evaluation, "_STOP_TIMEOUT_SECONDS", 0.05, raising=False)

    def active_poll():
        with forecast_evaluation._poll_lock:
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=active_poll)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(TimeoutError, match="forecast evaluation"):
            forecast_evaluation.pause()
        assert forecast_evaluation._enabled() is False
        assert forecast_evaluation._stop_event.is_set()
        with pytest.raises(TimeoutError, match="forecast evaluation"):
            forecast_evaluation.configure(enabled=True)
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    forecast_evaluation.pause()
    forecast_evaluation.configure(enabled=True)
    assert forecast_evaluation._enabled() is True


def test_boot_enabled_resets_existing_cutover_before_start(monkeypatch):
    _enable(monkeypatch)
    resets = []
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **kwargs: resets.append(kwargs),
    )
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "old-cutover")
    monkeypatch.setattr(forecast_evaluation, "poll_once", lambda: None)
    forecast_evaluation.start_at_boot()
    try:
        assert resets == [{"reset": True}]
        assert forecast_evaluation._worker_thread.is_alive()
    finally:
        forecast_evaluation.pause()


def test_boot_disabled_does_not_query_or_reset_cutover(monkeypatch):
    monkeypatch.setattr(forecast_evaluation, "get_config", lambda: {"forecast_evaluation_enabled": False})
    monkeypatch.setattr(
        forecast_evaluation_store, "initialize_cursor", lambda **_kwargs: pytest.fail("disabled at boot"),
    )
    forecast_evaluation.start_at_boot()
    assert forecast_evaluation._worker_thread is None


def test_start_does_not_create_a_worker_when_disabled(monkeypatch):
    monkeypatch.setattr(
        forecast_evaluation, "get_config", lambda: {"forecast_evaluation_enabled": False},
    )
    monkeypatch.setattr(
        forecast_evaluation.forecast_evaluation_store,
        "get_cursor",
        lambda: pytest.fail("disabled worker must not initialize its cursor"),
    )
    monkeypatch.setattr(forecast_evaluation, "_worker_thread", None)
    forecast_evaluation.start()
    assert forecast_evaluation._worker_thread is None


def test_concurrent_start_creates_only_one_worker(monkeypatch):
    from types import SimpleNamespace

    _enable(monkeypatch)
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "existing")
    monkeypatch.setattr(forecast_evaluation, "poll_once", lambda: None)
    real_thread = threading.Thread
    constructing = threading.Event()
    release = threading.Event()
    second_attempted = threading.Event()
    second_finished = threading.Event()
    workers = []
    calls = []
    errors = []

    def worker_factory(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            constructing.set()
            release.wait(timeout=2)
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(
        forecast_evaluation, "threading",
        SimpleNamespace(Thread=worker_factory, Event=threading.Event, current_thread=threading.current_thread),
    )

    def start(finished, attempted=None):
        try:
            if attempted is not None:
                attempted.set()
            forecast_evaluation.start()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    first = real_thread(target=start, args=(threading.Event(),))
    second = real_thread(target=start, args=(second_finished, second_attempted))
    first.start()
    try:
        assert constructing.wait(timeout=2)
        second.start()
        assert second_attempted.wait(timeout=2)
        second_completed_early = second_finished.wait(timeout=0.1)
    finally:
        release.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
        forecast_evaluation._stop_event.set()
        for worker in workers:
            worker.join(timeout=2)
    assert not errors
    assert not second_completed_early
    assert len(workers) == 1
    assert all(not worker.is_alive() for worker in workers)


def test_pause_waits_for_start_before_stopping_worker(monkeypatch):
    from types import SimpleNamespace

    _enable(monkeypatch)
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "existing")
    monkeypatch.setattr(forecast_evaluation, "poll_once", lambda: None)
    real_thread = threading.Thread
    constructing = threading.Event()
    release = threading.Event()
    pausing = threading.Event()
    paused = threading.Event()

    def worker_factory(*args, **kwargs):
        constructing.set()
        release.wait(timeout=2)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(
        forecast_evaluation, "threading",
        SimpleNamespace(Thread=worker_factory, Event=threading.Event, current_thread=threading.current_thread),
    )

    def pause():
        pausing.set()
        forecast_evaluation.pause()
        paused.set()

    starter = real_thread(target=forecast_evaluation.start)
    pauser = real_thread(target=pause)
    starter.start()
    try:
        assert constructing.wait(timeout=2)
        pauser.start()
        assert pausing.wait(timeout=2)
        paused_early = paused.wait(timeout=0.1)
    finally:
        release.set()
        starter.join(timeout=2)
        if pauser.ident is not None:
            pauser.join(timeout=2)
    assert not paused_early
    assert not starter.is_alive() and not pauser.is_alive()
    assert paused.is_set()
    assert forecast_evaluation._enabled() is False
    assert forecast_evaluation._worker_thread is None


def test_pause_fences_poll_and_reenable_joins_old_worker_without_lock_inversion(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "existing")
    poll_entered = threading.Event()
    poll_checked_enabled = threading.Event()
    release_poll = threading.Event()
    new_poll_entered = threading.Event()
    pause_done = threading.Event()
    enable_attempted = threading.Event()
    enable_done = threading.Event()
    observed_enabled = []
    errors = []
    calls = []

    def poll():
        calls.append(True)
        if len(calls) > 1:
            new_poll_entered.set()
            return
        with forecast_evaluation._poll_lock:
            poll_entered.set()
            forecast_evaluation._stop_event.wait(timeout=2)
            observed_enabled.append(forecast_evaluation._enabled())
            poll_checked_enabled.set()
            release_poll.wait(timeout=2)

    def pause():
        try:
            forecast_evaluation.pause()
        except Exception as exc:
            errors.append(exc)
        finally:
            pause_done.set()

    def reenable():
        try:
            enable_attempted.set()
            forecast_evaluation.configure(enabled=True)
            forecast_evaluation.start()
        except Exception as exc:
            errors.append(exc)
        finally:
            enable_done.set()

    monkeypatch.setattr(forecast_evaluation, "poll_once", poll)
    forecast_evaluation.start()
    old_worker = forecast_evaluation._worker_thread
    old_stop = forecast_evaluation._stop_event
    assert poll_entered.wait(timeout=2)
    pauser = threading.Thread(target=pause)
    enabler = threading.Thread(target=reenable)
    pauser.start()
    try:
        assert poll_checked_enabled.wait(timeout=2)
        enabler.start()
        assert enable_attempted.wait(timeout=2)
        paused_early = pause_done.wait(timeout=0.1)
        enabled_early = enable_done.wait(timeout=0.1)
    finally:
        release_poll.set()
        pauser.join(timeout=2)
        if enabler.ident is not None:
            enabler.join(timeout=2)
    assert not errors
    assert not paused_early and not enabled_early
    assert pause_done.is_set() and enable_done.is_set()
    assert observed_enabled == [False]
    assert not old_worker.is_alive()
    assert old_stop.is_set()
    assert forecast_evaluation._stop_event is not old_stop
    assert forecast_evaluation._worker_thread is not old_worker
    assert new_poll_entered.wait(timeout=2)


def test_start_does_not_revive_a_paused_worker(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: "existing")
    monkeypatch.setattr(forecast_evaluation, "poll_once", lambda: None)
    forecast_evaluation.start()
    forecast_evaluation.pause()
    forecast_evaluation.start()
    assert forecast_evaluation._enabled() is False
    assert forecast_evaluation._worker_thread is None


def test_poll_quarantines_missing_evidence_and_advances_only_after_durable_rows(monkeypatch):
    _enable(monkeypatch)
    cursor = "2026-11-16T00:00:00+00:00|2026-11-16T00:00:00+00:00|"
    cycles = [
        _cycle(block=None, updated_at="2026-11-16T00:00:01+00:00"),
        _cycle(scope={"exchange_id": "binance", "venue_profile": "demo"}, updated_at="2026-11-16T00:00:02+00:00"),
    ]
    cycles[0]["payload"] = {"kronos_forecast": None}
    written = []
    cursor_updates = []
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: cursor)
    monkeypatch.setattr(forecast_evaluation_store, "terminal_forecast_cycles_after", lambda *_a, **_k: cycles)
    monkeypatch.setattr(forecast_evaluation_store, "pending_evaluations_due", lambda **_k: [])
    monkeypatch.setattr(forecast_evaluation_store, "upsert_evaluation", lambda row, **_k: written.append(row))
    monkeypatch.setattr(
        forecast_evaluation_store,
        "advance_cursor",
        lambda previous, stamp, cycle_id: cursor_updates.append((previous, stamp, cycle_id)) or True,
    )
    monkeypatch.setattr(forecast_evaluation, "_public_futures_exchange", lambda _scope: pytest.fail("unavailable block must not construct venue"))
    monkeypatch.setattr(forecast_evaluation, "_close_exchanges", lambda _clients: None)

    result = forecast_evaluation.poll_once(batch_size=10)

    assert result["cursor_blocked"] is False
    assert len(written) == 2
    assert written[0]["status"] == "invalid"
    assert written[1]["status"] == "pending"  # exact horizon has not closed
    assert cursor_updates == [(cursor, cycles[-1]["updated_at"], cycles[-1]["cycle_id"])]


@pytest.mark.parametrize("due_retry", [False, True])
def test_poll_preserves_evidence_and_cursor_when_first_write_fails(monkeypatch, due_retry):
    _enable(monkeypatch)
    cursor = "2026-11-16T00:00:00+00:00|2026-11-16T00:00:00+00:00|"
    block = _block()
    cycle = _cycle(block=block, updated_at="2026-11-16T00:00:01+00:00")
    if due_retry:
        cycle = {
            **cycle,
            "payload": {},
            "forecast_snapshot": block,
            "forecast_digest": forecast_evaluation_scoring.digest(block),
            "horizon_key": "1h|1800000000000|1",
            "venue_scope": {"exchange_id": "binance", "venue_profile": "demo"},
        }
    writes = []
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: cursor)
    monkeypatch.setattr(forecast_evaluation_store, "terminal_forecast_cycles_after", lambda *_a, **_k: [] if due_retry else [cycle])
    monkeypatch.setattr(forecast_evaluation_store, "pending_evaluations_due", lambda **_k: [cycle] if due_retry else [])

    def fail_first_write(row, **_kwargs):
        writes.append(row)
        if len(writes) == 1:
            raise OSError("database temporarily unavailable")

    monkeypatch.setattr(forecast_evaluation_store, "upsert_evaluation", fail_first_write)
    advanced = []
    monkeypatch.setattr(forecast_evaluation_store, "advance_cursor", lambda *_a: advanced.append(True))

    result = forecast_evaluation.poll_once(batch_size=1)

    assert len(writes) == 1
    assert writes[0]["forecast_snapshot"] == block
    assert writes[0]["forecast_digest"] == forecast_evaluation_scoring.digest(block)
    assert result["processed"] == []
    assert result["cursor_blocked"] is True
    assert advanced == []

    retried = forecast_evaluation.poll_once(batch_size=1)
    assert retried["cursor_blocked"] is False
    assert retried["processed"] == [cycle["cycle_id"]]
    assert writes[1]["forecast_snapshot"] == block
    assert writes[1]["forecast_digest"] == writes[0]["forecast_digest"]
    assert writes[1]["horizon_key"] == writes[0]["horizon_key"]
    assert advanced == ([] if due_retry else [True])


def test_unexpected_scoring_failure_blocks_cursor_instead_of_quarantining(monkeypatch):
    _enable(monkeypatch)
    cursor = "2026-11-16T00:00:00+00:00|2026-11-16T00:00:00+00:00|"
    cycle = _cycle()
    writes = []
    advances = []
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: cursor)
    monkeypatch.setattr(forecast_evaluation_store, "terminal_forecast_cycles_after", lambda *_a, **_k: [cycle])
    monkeypatch.setattr(forecast_evaluation_store, "pending_evaluations_due", lambda **_k: [])
    monkeypatch.setattr(forecast_evaluation_store, "upsert_evaluation", lambda *a, **_k: writes.append(a))
    monkeypatch.setattr(forecast_evaluation_store, "advance_cursor", lambda *a: advances.append(a))

    def fail_score(*_args, **_kwargs):
        raise RuntimeError("unexpected scoring failure")

    monkeypatch.setattr(forecast_evaluation, "_row_from_cycle", fail_score)
    result = forecast_evaluation.poll_once()
    assert result["cursor_blocked"] is True
    assert result["processed"] == []
    assert writes == []
    assert advances == []


def test_poll_uses_immutable_due_snapshot_without_cycle_payload(monkeypatch):
    _enable(monkeypatch)
    cursor = "2026-11-16T00:00:00+00:00|2026-11-16T00:00:00+00:00|"
    block = _block()
    due = {
        "cycle_id": "cycle-retry",
        "symbol": "BTC/USDT:USDT",
        "cycle_timeframe": block["timeframe"],
        "cycle_selected_open": block["data_open_time"],
        "forecast_snapshot": block,
        "forecast_digest": forecast_evaluation_scoring.digest(block),
        "horizon_key": "1h|1800000000000|1",
        "venue_scope": {"exchange_id": "binance", "venue_profile": "demo"},
    }
    written = []
    monkeypatch.setattr(forecast_evaluation_store, "get_cursor", lambda: cursor)
    monkeypatch.setattr(forecast_evaluation_store, "terminal_forecast_cycles_after", lambda *_a, **_k: [])
    monkeypatch.setattr(forecast_evaluation_store, "pending_evaluations_due", lambda **_k: [due])
    monkeypatch.setattr(forecast_evaluation_store, "upsert_evaluation", lambda row, **_k: written.append(row))
    monkeypatch.setattr(forecast_evaluation, "_public_futures_exchange", lambda _scope: pytest.fail("horizon is pending"))
    monkeypatch.setattr(forecast_evaluation, "_close_exchanges", lambda _clients: None)

    result = forecast_evaluation.poll_once()

    assert result["due_retries"] == 1
    assert written[0]["forecast_snapshot"] == block
    assert written[0]["cycle_id"] == "cycle-retry"
