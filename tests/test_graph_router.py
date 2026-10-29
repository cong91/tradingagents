"""The graph router must never crash on a non-assistant turn.

The relay intermittently answers with ``role="tool"``, which LangChain maps to
``ToolMessage`` -- an object with no ``tool_calls`` attribute. Reading it
directly raised ``AttributeError`` inside a live cycle (observed on INJ-USD),
so the router treats a missing attribute as "no tool calls" and finishes the
analyst's turn instead of killing the graph.
"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tradingagents.graph.setup import _tools_or_clear

pytestmark = pytest.mark.unit

SPEC = SimpleNamespace(tool_node="tools_market", clear_node="Msg Clear Market")


def route(message):
    return _tools_or_clear(SPEC)({"messages": [message]})


def test_assistant_turn_with_tool_calls_routes_to_the_tool_node():
    message = AIMessage(
        content="",
        tool_calls=[{"name": "get_stock_data", "args": {"ticker": "BTC-USD"}, "id": "c1"}],
    )

    assert route(message) == "tools_market"


def test_assistant_turn_without_tool_calls_finishes_the_report():
    assert route(AIMessage(content="final report")) == "Msg Clear Market"


def test_tool_message_routes_to_clear_instead_of_raising():
    """A ``ToolMessage`` has no ``tool_calls``; the router must not crash."""
    assert route(ToolMessage(content="tool output", tool_call_id="c1")) == "Msg Clear Market"


def test_human_message_routes_to_clear_instead_of_raising():
    """Any non-assistant turn is a transport defect, not a tool decision."""
    assert route(HumanMessage(content="proceed")) == "Msg Clear Market"
