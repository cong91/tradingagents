"""Content-free, deterministic telemetry tests; no graph, vendor, or orders."""

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.callbacks import BaseCallbackHandler

import server.autoloop_telemetry as telemetry_mod
from server.autoloop_telemetry import CycleTelemetry

pytestmark = pytest.mark.unit


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    origin = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return origin + timedelta(seconds=clock.now - 100.0)

    monkeypatch.setattr(telemetry_mod, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(telemetry_mod, "datetime", ClockDatetime, raising=False)
    return clock


def graph_task(telemetry, node="Market Analyst", task_id="market", event="task", **details):
    telemetry.graph_event(
        {"type": event, "payload": {"id": task_id, "name": node, **details}},
        "BTC-USD",
    )


def test_nested_graph_llm_tool_timings_and_attribution(clock):
    telemetry = CycleTelemetry()
    assert isinstance(telemetry, BaseCallbackHandler)
    root = telemetry.start("graph_analysis", "BTC-USD")
    clock.now = 101.0
    graph_task(telemetry)
    clock.now = 102.0
    llm_id, tool_id = uuid4(), uuid4()
    telemetry.on_chat_model_start(
        {"name": "ChatOpenAI"}, [], run_id=llm_id,
        metadata={"langgraph_node": "Market Analyst"},
    )
    clock.now = 103.0
    telemetry.on_tool_start(
        {"name": "get_market_data"}, "", run_id=tool_id, parent_run_id=llm_id,
        metadata={"langgraph_node": "Market Analyst"},
    )
    clock.now = 103.25
    snapshot = telemetry.snapshot()
    assert snapshot["active_stage"] == "tool:get_market_data"
    assert snapshot["active_ticker"] == "BTC-USD"
    assert snapshot["active_node"] == "Market Analyst"
    assert snapshot["active_path"] == [
        "graph_analysis", "graph_node:Market Analyst", "llm:ChatOpenAI", "tool:get_market_data",
    ]
    assert snapshot["active_elapsed_ms"] == 250.0
    assert [span["elapsed_ms"] for span in snapshot["active_spans"]] == [
        3250.0, 2250.0, 1250.0, 250.0,
    ]
    assert [span["node"] for span in snapshot["active_spans"]] == [
        None, "Market Analyst", "Market Analyst", "Market Analyst",
    ]
    assert snapshot["active_spans"][-1]["started_at"] == "2026-10-14T12:00:03+00:00"

    clock.now = 104.0
    telemetry.on_tool_end(None, run_id=tool_id)
    assert telemetry.snapshot()["active_stage"] == "llm:ChatOpenAI"
    assert telemetry.snapshot()["active_elapsed_ms"] == 2000.0
    clock.now = 105.0
    telemetry.on_llm_end(None, run_id=llm_id)
    assert telemetry.snapshot()["active_stage"] == "graph_node:Market Analyst"
    clock.now = 106.0
    graph_task(telemetry, event="task_result")
    assert telemetry.snapshot()["active_stage"] == "graph_analysis"
    clock.now = 107.0
    telemetry.finish(root)
    snapshot = telemetry.snapshot()
    assert snapshot["active_stage"] is None
    assert snapshot["active_node"] is None
    assert snapshot["active_path"] == []
    assert snapshot["active_spans"] == []
    assert snapshot["active_elapsed_ms"] is None
    assert snapshot["current_ticker"] == "BTC-USD"
    assert snapshot["last_stage"] == "tool:get_market_data"
    completed = [event for event in snapshot["events"] if event["event"] == "finished"]
    assert [event["duration_ms"] for event in completed] == [1000.0, 3000.0, 5000.0, 7000.0]
    assert completed[1]["node"] == "Market Analyst"
    assert completed[1]["started_at"] == "2026-10-14T12:00:02+00:00"
    for event in snapshot["events"]:
        assert datetime.fromisoformat(event["started_at"]).utcoffset() == timedelta(0)


def test_callback_metadata_attributes_node_without_graph_event(clock):
    telemetry = CycleTelemetry()
    telemetry.start("graph_analysis", "ETH-USD")
    run_id = uuid4()
    telemetry.on_llm_start(
        {}, [], run_id=run_id, metadata={"langgraph_node": "News Analyst"},
    )
    snapshot = telemetry.snapshot()
    assert snapshot["active_stage"] == "llm"
    assert snapshot["active_node"] == "News Analyst"
    assert snapshot["active_ticker"] == "ETH-USD"
    assert snapshot["active_path"] == ["graph_analysis", "graph_node:News Analyst", "llm"]


def test_callback_parent_and_metadata_select_correct_concurrent_node(clock):
    telemetry = CycleTelemetry()
    telemetry.start("graph_analysis", "BTC-USD")
    graph_task(telemetry, "Market Analyst", "market")
    market_llm = uuid4()
    telemetry.on_llm_start(
        {}, [], run_id=market_llm, metadata={"langgraph_node": "Market Analyst"},
    )
    graph_task(telemetry, "News Analyst", "news")
    telemetry.on_llm_start(
        {}, [], run_id=uuid4(), metadata={"langgraph_node": "News Analyst"},
    )
    telemetry.on_tool_start(
        {"name": "prices"}, "", run_id=uuid4(), parent_run_id=market_llm,
        metadata={"langgraph_node": "Market Analyst"},
    )
    snapshot = telemetry.snapshot()
    assert snapshot["active_node"] == "Market Analyst"
    assert snapshot["active_path"] == [
        "graph_analysis", "graph_node:Market Analyst", "llm", "tool:prices",
    ]


def test_events_keep_exact_bound_and_drop_count(clock):
    telemetry = CycleTelemetry()
    total_spans = 55
    for index in range(total_spans):
        span_id = telemetry.start(f"stage:{index}")
        telemetry.finish(span_id)
    snapshot = telemetry.snapshot()
    assert len(snapshot["events"]) == 100
    assert snapshot["dropped_events"] == total_spans * 2 - 100
    assert snapshot["events"][0]["stage"] == "stage:5"
    assert snapshot["events"][-1]["stage"] == "stage:54"


def test_callbacks_run_inline_to_keep_async_manager_event_order():
    assert CycleTelemetry().run_inline is True


def test_nested_diagnostic_span_inherits_root_ticker(clock):
    telemetry = CycleTelemetry()
    telemetry.start("graph_analysis", "ETH-USD")
    with telemetry.span("vendor:market_data"):
        assert telemetry.snapshot()["active_ticker"] == "ETH-USD"
        assert telemetry.snapshot()["current_ticker"] == "ETH-USD"
    assert telemetry.snapshot()["events"][-1]["ticker"] == "ETH-USD"
    telemetry.finish("1")
    with telemetry.span("screening"):
        assert telemetry.snapshot()["active_ticker"] is None


def test_callback_without_metadata_inherits_graph_node(clock):
    telemetry = CycleTelemetry()
    telemetry.start("graph_analysis", "BTC-USD")
    graph_task(telemetry)
    telemetry.on_llm_start({}, [], run_id=uuid4())
    snapshot = telemetry.snapshot()
    assert snapshot["active_node"] == "Market Analyst"
    assert snapshot["active_path"] == ["graph_analysis", "graph_node:Market Analyst", "llm"]


def test_callbacks_and_graph_omit_sensitive_payloads_and_only_store_error_types(clock):
    notifications = []
    telemetry = CycleTelemetry(on_change=notifications.append)
    secret = "secret-payload-never-store"
    serialized = {"name": "SafeName", "api_key": secret, "kwargs": {"token": secret}}
    metadata = {"langgraph_node": "Market Analyst", "api_key": secret, "args": secret}
    error = ValueError(secret)
    llm_id, chat_id, tool_id = uuid4(), uuid4(), uuid4()
    telemetry.on_llm_start(serialized, [secret], run_id=llm_id, metadata=metadata)
    telemetry.on_llm_error(error, run_id=llm_id)
    telemetry.on_chat_model_start(serialized, [[secret]], run_id=chat_id, metadata=metadata)
    telemetry.on_llm_end({"output": secret}, run_id=chat_id)
    telemetry.on_tool_start(
        serialized, secret, run_id=tool_id, metadata=metadata, inputs={"args": secret},
    )
    telemetry.on_tool_error(error, run_id=tool_id)
    graph_task(telemetry, input=secret, triggers=[secret])
    graph_task(telemetry, event="task_result", result={"output": secret}, error=secret)
    snapshot = telemetry.snapshot()
    encoded = json.dumps([snapshot, *notifications])
    assert secret not in encoded
    for key in ("api_key", "kwargs", "args", "inputs", "prompts", "messages", "output", "serialized"):
        assert key not in encoded
    assert [event["error_type"] for event in snapshot["events"] if event["event"] == "failed"] == [
        "ValueError", "ValueError", "GraphNodeError",
    ]
    allowed_event_keys = {"event", "stage", "ticker", "node", "started_at", "duration_ms", "error_type"}
    assert all(set(event) <= allowed_event_keys for event in snapshot["events"])


def test_span_reraises_original_exception_and_records_type_only(clock):
    telemetry = CycleTelemetry()
    error = RuntimeError("private message")
    with pytest.raises(RuntimeError) as caught, telemetry.span("planning", "BTC-USD"):
        clock.now = 100.125
        raise error
    assert caught.value is error
    event = telemetry.snapshot()["events"][-1]
    assert event["event"] == "failed"
    assert event["error_type"] == "RuntimeError"
    assert event["duration_ms"] == 125.0
    assert "private message" not in json.dumps(telemetry.snapshot())


def test_close_freezes_all_state_and_ignores_late_worker_and_callback_events(clock):
    notifications = []
    telemetry = CycleTelemetry(on_change=notifications.append)
    root = telemetry.start("graph_analysis", "BTC-USD")
    graph_task(telemetry)
    llm_id, tool_id = uuid4(), uuid4()
    telemetry.on_llm_start({}, [], run_id=llm_id)
    telemetry.on_tool_start({}, "", run_id=tool_id, parent_run_id=llm_id)
    clock.now = 101.25
    telemetry.close()
    frozen = telemetry.snapshot()
    count = len(notifications)
    clock.now = 200.0
    assert telemetry.start("late", "ETH-USD") == ""
    telemetry.finish(root, RuntimeError("late"))
    graph_task(telemetry, event="task_result", error="late")
    graph_task(telemetry, "late", "late")
    telemetry.on_llm_end(None, run_id=llm_id)
    telemetry.on_llm_error(RuntimeError("late"), run_id=llm_id)
    telemetry.on_tool_end(None, run_id=tool_id)
    telemetry.on_tool_error(RuntimeError("late"), run_id=tool_id)
    telemetry.on_llm_start({}, [], run_id=uuid4())
    telemetry.on_chat_model_start({}, [], run_id=uuid4())
    telemetry.on_tool_start({}, "", run_id=uuid4())
    with telemetry.span("late"):
        pass
    telemetry.close()
    assert telemetry.snapshot() == frozen
    assert frozen["active_elapsed_ms"] == 1250.0
    assert len(frozen["active_spans"]) == 4
    assert telemetry._spans == {}
    assert len(notifications) == count


def test_snapshot_and_notification_payloads_cannot_mutate_internal_state(clock):
    notifications = []
    telemetry = CycleTelemetry(on_change=notifications.append)
    telemetry.start("graph_analysis", "BTC-USD")
    telemetry.close()
    frozen = telemetry.snapshot()
    expected = json.loads(json.dumps(frozen))
    frozen["events"][0]["stage"] = "changed"
    frozen["active_spans"][0]["stage"] = "changed"
    frozen["active_path"].append("changed")
    notifications[0]["events"].clear()
    notifications[0]["active_spans"].clear()
    assert telemetry.snapshot() == expected


def test_duplicate_starts_and_unknown_finishes_are_noops(clock):
    notifications = []
    telemetry = CycleTelemetry(on_change=notifications.append)
    graph_task(telemetry)
    graph_task(telemetry)
    llm_id = uuid4()
    telemetry.on_llm_start({}, [], run_id=llm_id)
    telemetry.on_chat_model_start({}, [], run_id=llm_id)
    telemetry.on_llm_end(None, run_id=llm_id)
    telemetry.on_llm_end(None, run_id=llm_id)
    telemetry.on_tool_end(None, run_id=uuid4())
    telemetry.finish("unknown")
    telemetry.finish("")
    graph_task(telemetry, event="task_result")
    graph_task(telemetry, event="task_result")
    assert len(telemetry.snapshot()["events"]) == 4
    assert len(notifications) == 4
    assert telemetry.snapshot()["active_stage"] is None


@pytest.mark.parametrize("payload", [None, [], {}, {"payload": None}, {"payload": {"name": ""}}])
def test_malformed_graph_events_are_ignored(payload):
    telemetry = CycleTelemetry()
    telemetry.graph_event(payload, "BTC-USD")
    assert telemetry.snapshot()["events"] == []


def test_notifications_hold_lock_preserve_order_and_allow_reentrant_snapshot():
    published = []
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_notified = threading.Event()
    errors = []
    lock_available = []

    def notify(snapshot):
        stage = snapshot["active_stage"]
        if stage == "first":
            def probe_lock():
                acquired = telemetry._lock.acquire(blocking=False)
                lock_available.append(acquired)
                if acquired:
                    telemetry._lock.release()

            probe = threading.Thread(target=probe_lock)
            probe.start()
            probe.join(timeout=1.0)
            first_entered.set()
            if not release_first.wait(timeout=2.0):
                errors.append("first notification was never released")
            assert telemetry.snapshot()["active_stage"] == "first"
        else:
            second_notified.set()
        published.append(stage)

    telemetry = CycleTelemetry(on_change=notify)

    def start(stage):
        try:
            if stage == "second":
                second_attempted.set()
            telemetry.start(stage)
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=start, args=("first",))
    second = threading.Thread(target=start, args=("second",))
    first.start()
    try:
        assert first_entered.wait(timeout=1.0)
        second.start()
        assert second_attempted.wait(timeout=1.0)
        assert not second_notified.wait(timeout=0.05)
    finally:
        release_first.set()
        first.join(timeout=2.0)
        if second.ident is not None:
            second.join(timeout=2.0)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert lock_available == [False]
    assert published == ["first", "second"]


def test_close_waits_for_inflight_notification_and_no_publish_follows_return():
    entered = threading.Event()
    release = threading.Event()
    close_attempted = threading.Event()
    closed = threading.Event()
    published_after_close = []
    errors = []

    def notify(snapshot):
        entered.set()
        if not release.wait(timeout=2.0):
            errors.append("notification was never released")
        published_after_close.append(closed.is_set())

    telemetry = CycleTelemetry(on_change=notify)
    worker = threading.Thread(target=telemetry.start, args=("worker",))

    def close():
        close_attempted.set()
        telemetry.close()
        closed.set()

    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(timeout=1.0)
        closer.start()
        assert close_attempted.wait(timeout=1.0)
        assert not closed.wait(timeout=0.05)
    finally:
        release.set()
        worker.join(timeout=2.0)
        if closer.ident is not None:
            closer.join(timeout=2.0)
    assert not worker.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert closed.is_set()
    frozen = telemetry.snapshot()

    def late_callbacks():
        run_id = uuid4()
        telemetry.on_chat_model_start({}, [], run_id=run_id)
        telemetry.on_llm_end(None, run_id=run_id)
        telemetry.on_tool_start({}, "", run_id=run_id)
        telemetry.on_tool_error(RuntimeError("late"), run_id=run_id)
        telemetry.start("late")

    late_worker = threading.Thread(target=late_callbacks)
    late_worker.start()
    late_worker.join(timeout=2.0)
    assert not late_worker.is_alive()
    assert telemetry.snapshot() == frozen
    assert published_after_close == [False]
