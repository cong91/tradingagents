"""Cooperative graph cancellation without network or provider calls."""

from threading import Event, Thread
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import Mock

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import RunControl

from tradingagents.dataflows.config import get_config, set_config
from tradingagents.graph import trading_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKER = "NVDA"
TRADE_DATE = "2026-01-09"
FINAL_STATE = {"final_trade_decision": "Rating: Buy", "final_rating": "Buy"}


class Cancelled(Exception):
    pass


class Observer:
    def __init__(self, on_event=None):
        self.events = []
        self.on_event = on_event

    def graph_event(self, event, ticker):
        self.events.append((event, ticker))
        if self.on_event is not None:
            self.on_event(event)


class RecordingStream:
    def __init__(self, events):
        self.events = iter(events)
        self.closed = False
        self.next_calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.next_calls += 1
        return next(self.events)

    def close(self):
        self.closed = True
        self.events.close()


@pytest.fixture
def graph():
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"checkpoint_enabled": False}
    graph._checkpointer_ctx = None
    graph._resuming = False
    graph.propagator = SimpleNamespace(get_graph_args=lambda: {})
    graph.create_run_state = Mock(return_value={"initial": True})
    graph.record_decision = Mock()
    graph.clear_checkpoint_on_success = Mock()
    graph.graph = Mock()
    return graph


def _event(kind="task", name="analyst"):
    return {"type": kind, "payload": {"name": name}}


def _cancel_when_set(cancelled):
    def check():
        if cancelled.is_set():
            raise Cancelled("mode disabled")
    return check


def test_cancelled_before_preparation_never_starts_graph(graph):
    cancelled = Event()
    cancelled.set()

    with pytest.raises(Cancelled, match="mode disabled"):
        graph.propagate_with_telemetry(
            TICKER, TRADE_DATE, telemetry=Observer(), check_cancelled=_cancel_when_set(cancelled),
        )

    graph.create_run_state.assert_not_called()
    graph.graph.stream.assert_not_called()
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()


def test_cancelled_during_preparation_never_starts_graph(graph):
    cancelled = Event()
    graph.create_run_state.side_effect = lambda *args: cancelled.set() or {"initial": True}

    with pytest.raises(Cancelled, match="mode disabled"):
        graph.propagate_with_telemetry(
            TICKER, TRADE_DATE, telemetry=Observer(), check_cancelled=_cancel_when_set(cancelled),
        )

    graph.graph.stream.assert_not_called()
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()


def test_cancelled_between_events_closes_stream_without_starting_next_step(graph):
    cancelled = Event()
    started = []

    def events():
        started.append("analyst")
        yield (), "debug", _event()
        started.append("trader")
        yield (), "values", FINAL_STATE

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream
    observer = Observer(on_event=lambda event: cancelled.set())

    with pytest.raises(Cancelled, match="mode disabled"):
        graph.propagate_with_telemetry(
            TICKER, TRADE_DATE, telemetry=observer, check_cancelled=_cancel_when_set(cancelled),
        )

    assert started == ["analyst"]
    assert stream.next_calls == 1
    assert stream.closed
    assert graph.graph.stream.call_args.kwargs["control"].drain_requested
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()


def test_cancelled_after_stream_exhaustion_does_not_finalize(graph):
    cancelled = Event()

    def events():
        yield (), "values", FINAL_STATE
        cancelled.set()

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream
    with pytest.raises(Cancelled, match="mode disabled"):
        graph.propagate_with_telemetry(
            TICKER, TRADE_DATE, telemetry=Observer(), check_cancelled=_cancel_when_set(cancelled),
        )

    assert stream.closed
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()


def test_stream_exception_propagates_and_closes_without_finalizing(graph):
    failure = RuntimeError("provider failed")

    def events():
        yield "values", FINAL_STATE
        raise failure

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream
    with pytest.raises(RuntimeError) as caught:
        graph.propagate_with_telemetry(TICKER, TRADE_DATE, telemetry=Observer())

    assert caught.value is failure
    assert stream.closed
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()


def test_callback_reads_live_caller_config_instead_of_run_snapshot(graph):
    set_config({"auto_spot_enabled": True})
    graph.config["auto_spot_enabled"] = True

    def events():
        set_config({"auto_spot_enabled": False})
        yield (), "values", FINAL_STATE

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream

    def check():
        if not get_config()["auto_spot_enabled"]:
            raise Cancelled("mode disabled")

    with pytest.raises(Cancelled, match="mode disabled"):
        graph.propagate_with_telemetry(TICKER, TRADE_DATE, telemetry=Observer(), check_cancelled=check)

    assert stream.closed
    graph.record_decision.assert_not_called()


def test_completed_cancellable_run_keeps_top_level_state_and_telemetry(graph):
    def events():
        yield ("analyst:1",), "values", {"private": "nested"}
        yield ("analyst:1",), "debug", _event(name="nested")
        yield (), "debug", _event()
        yield (), "values", FINAL_STATE

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream
    observer = Observer()
    state, signal = graph.propagate_with_telemetry(
        TICKER, TRADE_DATE, telemetry=observer, check_cancelled=lambda: None,
    )

    assert (state, signal) == (FINAL_STATE, "Buy")
    assert observer.events == [(_event(), TICKER)]
    assert stream.closed
    assert graph.graph.stream.call_args.kwargs["subgraphs"] is True
    graph.record_decision.assert_called_once_with(TICKER, TRADE_DATE, FINAL_STATE)
    graph.clear_checkpoint_on_success.assert_called_once()


def test_private_uninstrumented_cancellation_streams_instead_of_invoke(graph):
    def events():
        yield (), "values", FINAL_STATE

    stream = RecordingStream(events())
    graph.graph.stream.return_value = stream
    state, signal = graph._propagate(
        TICKER, TRADE_DATE, "stock", None, check_cancelled=lambda: None,
    )

    assert (state, signal) == (FINAL_STATE, "Buy")
    assert stream.closed
    graph.graph.invoke.assert_not_called()


def test_active_subgraph_drains_after_inflight_node_finishes(graph, monkeypatch):
    started = Event()
    release = Event()
    drain_requested = Event()
    cancelled = Event()
    later_calls = []
    outcome = []

    class RecordingControl(RunControl):
        def request_drain(self, reason="shutdown"):
            super().request_drain(reason)
            drain_requested.set()

    class State(TypedDict, total=False):
        initial: bool
        final_trade_decision: str
        final_rating: str

    def inflight(state):
        started.set()
        cancelled.set()
        assert release.wait(10), "test did not release the in-flight node"
        return {}

    def later(state):
        later_calls.append("later")
        return FINAL_STATE

    nested = StateGraph(State)
    nested.add_node("inflight", inflight)
    nested.add_node("later", later)
    nested.add_edge(START, "inflight")
    nested.add_edge("inflight", "later")
    nested.add_edge("later", END)
    outer = StateGraph(State)
    outer.add_node("analyst", nested.compile())
    outer.add_edge(START, "analyst")
    outer.add_edge("analyst", END)
    graph.graph = outer.compile()
    monkeypatch.setattr(trading_graph, "RunControl", RecordingControl)

    def run():
        try:
            graph.propagate_with_telemetry(
                TICKER, TRADE_DATE, telemetry=Observer(),
                check_cancelled=_cancel_when_set(cancelled),
            )
        except Exception as exc:
            outcome.append(exc)

    worker = Thread(target=run)
    worker.start()
    try:
        assert drain_requested.wait(8), (
            f"cancellation did not request graph drain; started={started.is_set()}, outcome={outcome!r}"
        )
        assert worker.is_alive(), "an in-flight node must be allowed to finish"
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)
    assert later_calls == []
    graph.record_decision.assert_not_called()
    graph.clear_checkpoint_on_success.assert_not_called()
