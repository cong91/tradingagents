"""Invoke a tool-bound chain, re-requesting responses that cannot be a decision.

Two transport defects are handled here, both from the OpenAI-compatible relay
this project runs against:

1. A tool call whose ``arguments`` object is empty. Every tool here declares at
   least one required field, so such a call cannot be a decision the model made:
   handing it to ``ToolNode`` fails every required field, burns a graph round
   trip, and leaves the analyst with no data for that source.
2. A response that is not an assistant turn at all -- typically ``role="tool"``,
   which LangChain maps to ``ToolMessage``. That object has no ``tool_calls``
   attribute, so the analyst node and the graph router crashed on it mid-cycle.

Either way the response is re-requested before the graph sees it.
"""

import logging
from typing import Any

from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

# The relay's malformed answers are intermittent rather than sticky, so a
# couple of re-requests clear them without paying an unbounded number of calls.
MAX_ATTEMPTS = 3

_MALFORMED_NOTE = (
    "The model provider returned a malformed response (no assistant turn) for "
    "this tool-bound request. No tool was run; report this as a data-source "
    "failure rather than inventing values."
)


def _is_assistant_turn(result: Any) -> bool:
    """Whether ``result`` is a turn that can carry a decision.

    A real answer is an assistant message; ``tool_calls`` defaults to an empty
    list there. Anything else lacks the attribute the graph routes on.
    """
    return hasattr(result, "tool_calls")


def _argument_less_calls(result: Any) -> list[str]:
    """Names of the response's tool calls that carry no arguments at all."""
    calls = getattr(result, "tool_calls", None) or []
    return [str(call.get("name")) for call in calls if not call.get("args")]


def _is_malformed(result: Any) -> bool:
    return not _is_assistant_turn(result)


def invoke_with_tool_retry(chain: Any, messages: Any, *, attempts: int = MAX_ATTEMPTS) -> Any:
    """Run ``chain`` and re-request while its response is unusable.

    A response with no tool calls at all is a legitimate final answer and is
    returned as-is. After ``attempts`` tries an argument-less response is
    returned anyway: its validation error is a message the model can correct on
    its next turn, which the caller must still be able to receive. A response
    that is still not an assistant turn is replaced with an assistant message
    saying so, because the caller cannot use a ``ToolMessage`` at all.
    """
    result = chain.invoke(messages)
    for attempt in range(2, attempts + 1):
        if not _is_malformed(result) and not _argument_less_calls(result):
            return result
        if _is_malformed(result):
            logger.warning(
                "provider returned %s instead of an assistant turn; "
                "re-requesting (attempt %d/%d)",
                type(result).__name__, attempt, attempts,
            )
        else:
            logger.warning(
                "tool calls without arguments (%s); re-requesting (attempt %d/%d)",
                ", ".join(_argument_less_calls(result)), attempt, attempts,
            )
        result = chain.invoke(messages)
    if _is_malformed(result):
        logger.error(
            "provider kept returning %s instead of an assistant turn after %d attempts",
            type(result).__name__, attempts,
        )
        return AIMessage(content=_MALFORMED_NOTE)
    return result
