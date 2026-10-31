"""The relay sometimes answers a tool-bound request with argument-less tool calls.

Every tool here requires at least one argument, so such a call cannot be a
model decision — it is a transport defect that fails validation in ``ToolNode``
and leaves the analyst with no data. The analyst nodes must re-request instead
of handing it to the graph.
"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tradingagents.agents.tool_calls import invoke_with_tool_retry


def _response(calls):
    return SimpleNamespace(tool_calls=calls)


def _call(name, args):
    return {"name": name, "args": args, "id": "c1"}


class _Chain:
    """A chain that replays canned responses and counts its invocations."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.invocations = 0

    def invoke(self, messages):
        self.invocations += 1
        return self._responses.pop(0)


@pytest.mark.unit
def test_argument_less_tool_call_is_re_requested():
    chain = _Chain([
        _response([_call("get_news", {})]),
        _response([_call("get_news", {"ticker": "ENA-USD"})]),
    ])

    result = invoke_with_tool_retry(chain, [])

    assert chain.invocations == 2
    assert result.tool_calls[0]["args"] == {"ticker": "ENA-USD"}


@pytest.mark.unit
def test_valid_tool_call_is_returned_without_a_second_request():
    chain = _Chain([_response([_call("get_news", {"ticker": "ENA-USD"})])])

    invoke_with_tool_retry(chain, [])

    assert chain.invocations == 1


@pytest.mark.unit
def test_final_answer_without_tool_calls_is_not_re_requested():
    chain = _Chain([_response([])])

    invoke_with_tool_retry(chain, [])

    assert chain.invocations == 1


@pytest.mark.unit
def test_one_argument_less_call_among_valid_ones_triggers_a_retry():
    chain = _Chain([
        _response([_call("get_news", {"ticker": "ENA-USD"}), _call("get_macro_indicators", {})]),
        _response([_call("get_macro_indicators", {"indicator": "cpi", "curr_date": "2026-10-26"})]),
    ])

    result = invoke_with_tool_retry(chain, [])

    assert chain.invocations == 2
    assert result.tool_calls[0]["args"]["indicator"] == "cpi"


@pytest.mark.unit
def test_a_persistently_empty_answer_is_returned_after_the_attempt_budget():
    chain = _Chain([_response([_call("get_news", {})]) for _ in range(3)])

    result = invoke_with_tool_retry(chain, [], attempts=3)

    assert chain.invocations == 3
    # The caller must still receive it: the validation message is how the model
    # learns to supply the arguments on its next turn.
    assert result.tool_calls[0]["args"] == {}


# --- a non-assistant turn is a transport defect too --------------------------


@pytest.mark.unit
def test_a_tool_message_response_is_re_requested_not_returned():
    """The relay sometimes answers with role="tool", which LangChain maps to a
    ``ToolMessage`` — an object with no ``tool_calls`` at all. Returning it made
    the analyst and the graph router crash on ``.tool_calls`` mid-cycle."""
    chain = _Chain([
        ToolMessage(content="tool output without a request", tool_call_id="c1"),
        AIMessage(content="", tool_calls=[_call("get_stock_data", {"ticker": "ENA-USD"})]),
    ])

    result = invoke_with_tool_retry(chain, [])

    assert chain.invocations == 2
    assert result.tool_calls[0]["name"] == "get_stock_data"


@pytest.mark.unit
def test_a_persistent_tool_message_becomes_an_ai_message_after_the_budget():
    """After the retry budget the caller must still get something it can use:
    an assistant turn explaining the defect, never a bare ``ToolMessage``."""
    chain = _Chain([ToolMessage(content="boom", tool_call_id="c1") for _ in range(3)])

    result = invoke_with_tool_retry(chain, [], attempts=3)

    assert chain.invocations == 3
    assert isinstance(result, AIMessage)
    assert result.tool_calls == []
    assert "malformed" in result.content.lower()
