"""Invoke a tool-bound chain, re-requesting tool calls that carry no arguments.

The OpenAI-compatible relay this project runs against intermittently answers a
tool-bound request with tool calls whose ``arguments`` object is empty. Every
tool here declares at least one required field, so such a call cannot be a
decision the model made: it is a transport defect. Handing it to ``ToolNode``
fails every required field, burns a graph round trip, and leaves the analyst
with no data for that source. A response whose tool calls are all argument-less
is therefore re-requested before the graph sees it.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The relay's empty-argument answer is intermittent rather than sticky, so a
# couple of re-requests clear it without paying an unbounded number of calls.
MAX_ATTEMPTS = 3


def _argument_less_calls(result: Any) -> list[str]:
    """Names of the response's tool calls that carry no arguments at all."""
    calls = getattr(result, "tool_calls", None) or []
    return [str(call.get("name")) for call in calls if not call.get("args")]


def invoke_with_tool_retry(chain: Any, messages: Any, *, attempts: int = MAX_ATTEMPTS) -> Any:
    """Run ``chain`` and re-request while its tool calls have empty arguments.

    A response with no tool calls at all is a legitimate final answer and is
    returned as-is. After ``attempts`` tries the last response is returned even
    if still argument-less: its validation error is a message the model can
    correct on its next turn, which the caller must still be able to receive.
    """
    result = chain.invoke(messages)
    for attempt in range(2, attempts + 1):
        if not _argument_less_calls(result):
            return result
        logger.warning(
            "tool calls without arguments (%s); re-requesting (attempt %d/%d)",
            ", ".join(_argument_less_calls(result)), attempt, attempts,
        )
        result = chain.invoke(messages)
    return result
