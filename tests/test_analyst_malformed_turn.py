"""An analyst node must survive a provider turn that is not an assistant message.

The relay intermittently answers a tool-bound request with ``role="tool"``,
which LangChain maps to ``ToolMessage``. That object has no ``tool_calls``, so
the node raised ``AttributeError`` after minutes of analysis (observed live on
INJ-USD). The retry seam now re-requests such a turn and always hands the node
an assistant message.
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts.market_analyst import create_market_analyst

pytestmark = pytest.mark.unit

STATE = {
    "company_of_interest": "BTC-USD",
    "trade_date": "2026-10-29",
    "asset_type": "crypto",
    "instrument_context": "",
    "messages": [AIMessage(content="analyse BTC-USD")],
}


class _Relay(RunnableLambda):
    """A bound chat model whose first answer is a malformed (tool) turn."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls = 0
        super().__init__(self._answer)

    def _answer(self, messages):
        self.calls += 1
        return self._responses.pop(0)

    def bind_tools(self, tools):
        return self


def test_market_analyst_recovers_from_a_tool_message_turn():
    relay = _Relay([
        ToolMessage(content="tool output without a request", tool_call_id="c1"),
        AIMessage(content="final market report"),
    ])

    result = create_market_analyst(relay)(dict(STATE))

    assert relay.calls == 2
    assert result["market_report"] == "final market report"
    assert isinstance(result["messages"][0], AIMessage)


def test_market_analyst_returns_a_usable_report_when_the_defect_persists():
    """After the retry budget the node must still return an assistant message,
    so the cycle records a data-source failure rather than crashing."""
    relay = _Relay([ToolMessage(content="boom", tool_call_id="c1") for _ in range(3)])

    result = create_market_analyst(relay)(dict(STATE))

    assert relay.calls == 3
    assert isinstance(result["messages"][0], AIMessage)
    assert "malformed" in result["market_report"].lower()
