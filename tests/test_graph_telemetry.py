"""Graph telemetry: propagate_with_telemetry must not change run results.

The instrumented path streams the real graph (values + debug modes), keeps the
same final state and signal as plain propagate(), reports only debug metadata to
the observer, wires the observer's LangChain callback hooks through the graph
config, and brackets the run with preparation/finalization spans — including
checkpoint cleanup on success/failure/resume.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.graph import END, START, StateGraph

from tradingagents.graph.checkpointer import checkpoint_step
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph

TICKER = "NVDA"
TRADE_DATE = "2026-10-12"
DECISION = "**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"

_should_crash = False


class _RunState(TypedDict):
    count: int
    final_trade_decision: str


def _workflow(llm) -> StateGraph:
    def analyst_node(state: _RunState) -> dict:
        llm.invoke("analyst prompt")
        return {"count": state.get("count", 0) + 1}

    def trader_node(state: _RunState) -> dict:
        llm.invoke("trader prompt")
        if _should_crash:
            raise RuntimeError("simulated mid-run crash")
        return {"count": state.get("count", 0) + 10, "final_trade_decision": DECISION}

    builder = StateGraph(_RunState)
    builder.add_node("analyst", analyst_node)
    builder.add_node("trader", trader_node)
    builder.add_edge(START, "analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


class RecordingObserver(BaseCallbackHandler):
    """Duck-typed telemetry observer that also records LangChain callbacks."""

    def __init__(self):
        self.events: list[dict] = []
        self._seq = 0

    def _record(self, kind, **detail):
        self._seq += 1
        self.events.append({"seq": self._seq, "kind": kind, **detail})

    @contextmanager
    def span(self, stage, ticker=None):
        self._record("span_start", stage=stage, ticker=ticker)
        yield
        self._record("span_end", stage=stage, ticker=ticker)

    def graph_event(self, payload, ticker):
        details = payload.get("payload", {})
        self._record(
            "graph_event",
            type=payload.get("type"),
            node=details.get("name"),
            ticker=ticker,
        )

    def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
        self._record("llm_start", node=(metadata or {}).get("langgraph_node"))


def _graph(tmp_path, *, checkpoint_enabled=False):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = copy.deepcopy({
        "checkpoint_enabled": checkpoint_enabled,
        "data_cache_dir": str(tmp_path),
        "results_dir": str(tmp_path),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    })
    graph.selected_analysts = ("market",)
    graph.propagator = Propagator(max_recur_limit=50)
    graph.memory_log = SimpleNamespace(
        store_decision=lambda **k: None, get_past_context=lambda *a, **k: "",
    )
    graph.settle_pending = lambda company: None
    graph.resolve_instrument_context = lambda *a, **k: "instrument context"
    graph._log_state = lambda *a, **k: None
    graph.workflow = _workflow(FakeListChatModel(responses=["ok"] * 8))
    graph.graph = graph.workflow.compile()
    graph._checkpointer_ctx = None
    graph._resuming = False
    return graph


def _graph_events(observer):
    return [event for event in observer.events if event["kind"] == "graph_event"]


def test_instrumented_path_returns_same_state_and_only_debug_metadata(tmp_path):
    normal_state, normal_signal = _graph(tmp_path).propagate(TICKER, TRADE_DATE)

    observer = RecordingObserver()
    state, signal = _graph(tmp_path).propagate_with_telemetry(
        TICKER, TRADE_DATE, telemetry=observer
    )

    assert (state, signal) == (normal_state, normal_signal)
    events = _graph_events(observer)
    assert [event["node"] for event in events] == ["analyst", "analyst", "trader", "trader"]
    # Only metadata: no full state values ever reach the observer.
    assert all(event["type"] in {"task", "task_result"} for event in events)
    assert "FINAL TRANSACTION PROPOSAL" not in json.dumps(observer.events)


def test_callbacks_fire_and_spans_bracket_the_run_in_order(tmp_path):
    observer = RecordingObserver()
    _graph(tmp_path).propagate_with_telemetry(TICKER, TRADE_DATE, telemetry=observer)

    kinds = [event["kind"] for event in observer.events]
    assert kinds[0] == "span_start" and kinds[-1] == "span_end"
    stages = [event["stage"] for event in observer.events if "stage" in event]
    # Preparation wraps state creation (incl. settle); finalization wraps
    # logging/decision recording; both sit outside the streamed nodes.
    assert stages[0] == "prepare_state"
    assert stages[-1] == "finalize_state"

    # Node-level callbacks arrive through the graph config...
    llm_starts = [event for event in observer.events if event["kind"] == "llm_start"]
    assert [event["node"] for event in llm_starts] == ["analyst", "trader"]
    # ...and the debug task event for a node is emitted before its LLM starts.
    first_node_event = next(
        event["seq"] for event in _graph_events(observer) if event["node"] == "analyst"
    )
    assert first_node_event < llm_starts[0]["seq"]


def test_duck_typed_observer_without_span_or_callbacks_still_works(tmp_path):
    events: list[tuple] = []

    class GraphEventOnly:
        def graph_event(self, payload, ticker):
            events.append((payload["type"], ticker))

    state, signal = _graph(tmp_path).propagate_with_telemetry(
        TICKER, TRADE_DATE, telemetry=GraphEventOnly()
    )
    assert signal == "Overweight"
    assert len(events) == 4


def test_checkpoint_survives_failure_then_resume_clears_it(tmp_path, monkeypatch):
    global _should_crash
    _should_crash = True
    graph = _graph(tmp_path, checkpoint_enabled=True)
    with pytest.raises(RuntimeError, match="mid-run crash"):
        graph.propagate_with_telemetry(TICKER, TRADE_DATE, telemetry=RecordingObserver())

    signature = graph._run_signature("stock")
    # The failed run kept its checkpoint for a later resume...
    assert checkpoint_step(str(tmp_path), TICKER, TRADE_DATE, signature) is not None
    # ...and the checkpoint scope still closed cleanly.
    assert graph._checkpointer_ctx is None

    _should_crash = False
    resumed = _graph(tmp_path, checkpoint_enabled=True)
    observer = RecordingObserver()
    state, signal = resumed.propagate_with_telemetry(
        TICKER, TRADE_DATE, telemetry=observer
    )

    assert signal == "Overweight"
    assert state["count"] == 11  # analyst's +1 came from the checkpoint
    assert checkpoint_step(str(tmp_path), TICKER, TRADE_DATE, signature) is None
    # The resumed run streamed the same instrumented path to completion.
    assert [event["node"] for event in _graph_events(observer) if event["type"] == "task"] == [
        "trader",
    ]
    assert resumed._checkpointer_ctx is None
