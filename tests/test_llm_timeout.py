"""Bounded LLM request timeout and relay retry behavior."""

from __future__ import annotations

import importlib
import json
import math
import socket

import pytest

import tradingagents.default_config as default_config_module
from tradingagents.llm_clients import openai_client
from tradingagents.llm_clients.factory import (
    _coerce_timeout,
    build_llm_kwargs,
    create_llm_client,
)

_ORIGINAL_SOCKET_CONNECT = socket.socket.connect


@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [(1, 1.0), (90, 90.0), ("12.5", 12.5)])
def test_coerce_timeout_accepts_finite_positive_numbers(value, expected):
    assert _coerce_timeout(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, -1, "0", "-2", math.inf, -math.inf, math.nan, True, "slow"])
def test_coerce_timeout_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="llm_timeout"):
        _coerce_timeout(value)


@pytest.mark.unit
def test_build_llm_kwargs_forwards_timeout_when_configured():
    assert build_llm_kwargs({"llm_provider": "openai_compatible", "llm_timeout": "45"})["timeout"] == 45.0
    assert "timeout" not in build_llm_kwargs({"llm_provider": "openai_compatible"})


@pytest.mark.unit
def test_generic_client_has_bounded_default_and_honors_override():
    default_client = create_llm_client(
        provider="openai_compatible", model="m", base_url="http://localhost:8000/v1"
    ).get_llm()
    custom_client = create_llm_client(
        provider="openai_compatible", model="m", base_url="http://localhost:8000/v1", timeout=30
    ).get_llm()

    assert default_client.request_timeout == 60.0
    assert default_client.streaming is True
    assert default_client.stream_chunk_timeout == 60.0
    assert custom_client.request_timeout == 30
    assert custom_client.streaming is True
    assert custom_client.stream_chunk_timeout == 30


@pytest.mark.unit
def test_connection_retry_budget_is_bounded():
    assert openai_client._CONNECTION_RETRIES == 2
    # Linear backoff: relay overload windows outlast a fixed sub-second gap.
    assert openai_client._CONNECTION_RETRY_WAIT > 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("request timed out"),
        ValueError("upstream_http2_stream_error: upstream failed"),
        ValueError("upstream_stream_read_error: response interrupted"),
    ],
)
def test_stream_transport_errors_are_retryable(error):
    assert openai_client._is_connection_error(error) is True


@pytest.mark.unit
@pytest.mark.parametrize("error", [
    json.JSONDecodeError("empty relay event", "", 0),
    ValueError("bad_response_status_code"),
])
def test_stream_error_retry_classification(error):
    expected = isinstance(error, json.JSONDecodeError)
    assert openai_client._is_connection_error(error) is expected


@pytest.mark.unit
def test_wrapped_json_decode_error_is_retryable():
    outer = RuntimeError("relay parser failed")
    outer.__cause__ = json.JSONDecodeError("truncated", "{", 1)
    assert openai_client._is_connection_error(outer) is True


@pytest.mark.unit
def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_LLM_TIMEOUT", "45")
    reloaded = importlib.reload(default_config_module)
    try:
        assert reloaded.DEFAULT_CONFIG["llm_timeout"] == 45.0
    finally:
        monkeypatch.delenv("TRADINGAGENTS_LLM_TIMEOUT", raising=False)
        importlib.reload(default_config_module)


def _relay_model():
    from tradingagents.llm_clients.openai_client import LocalCompatibleChatOpenAI

    return LocalCompatibleChatOpenAI(
        model="relay-model",
        base_url="https://relay.example/v1",
        api_key="test-key",
        use_responses_api=True,
        streaming=True,
        max_retries=0,
    )


def _relay_chunk(content, *, status=None, incomplete_details=None):
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    metadata = {}
    if status is not None:
        metadata["status"] = status
    if incomplete_details is not None:
        metadata["incomplete_details"] = incomplete_details
    return ChatGenerationChunk(
        message=AIMessageChunk(content=content, response_metadata=metadata)
    )


@pytest.mark.unit
def test_invoke_retries_json_decode_error_and_discards_partial_stream(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _relay_chunk("partial response")
            raise json.JSONDecodeError("empty relay event", "", 0)
        yield _relay_chunk("complete response", status="completed")

    monkeypatch.setattr(ChatOpenAI, "_stream", stream)
    response = _relay_model().invoke("Analyze market conditions")

    assert calls == 2
    assert response.content == "complete response"


@pytest.mark.unit
def test_invoke_retries_connection_drop_before_first_chunk(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("connection dropped before headers")
        yield _relay_chunk("complete response", status="completed")

    monkeypatch.setattr(ChatOpenAI, "_stream", stream)

    response = _relay_model().invoke("Analyze market conditions")

    assert calls == 2
    assert response.content == "complete response"


@pytest.mark.unit
def test_invoke_discards_partial_stream_and_retries_connection_drop(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _relay_chunk("partial response")
            raise ConnectionError("upstream_stream_read_error")
        yield _relay_chunk("complete response", status="completed")

    monkeypatch.setattr(ChatOpenAI, "_stream", stream)

    response = _relay_model().invoke("Analyze market conditions")

    assert calls == 2
    assert response.content == "complete response"


@pytest.mark.unit
def test_invoke_retries_responses_incomplete_without_terminal_reason(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _relay_chunk("", status="incomplete")
            return
        yield _relay_chunk("complete response", status="completed")

    monkeypatch.setattr(ChatOpenAI, "_stream", stream)

    response = _relay_model().invoke("Analyze market conditions")

    assert calls == 2
    assert response.content == "complete response"


@pytest.mark.unit
def test_invoke_does_not_retry_responses_incomplete_from_output_limit(monkeypatch):
    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        yield _relay_chunk(
            "partial response",
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
        )

    monkeypatch.setattr(ChatOpenAI, "_stream", stream)

    with pytest.raises(openai_client._IncompleteStreamError):
        _relay_model().invoke("Analyze market conditions")

    assert calls == 1


@pytest.mark.unit
def test_ainvoke_discards_partial_stream_and_retries_connection_drop(monkeypatch):
    import asyncio

    from langchain_openai import ChatOpenAI

    monkeypatch.setattr(socket.socket, "connect", _ORIGINAL_SOCKET_CONNECT)
    monkeypatch.setattr(openai_client, "_CONNECTION_RETRY_WAIT", 0.0)
    calls = 0

    async def stream(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield _relay_chunk("partial response")
            raise ConnectionError("upstream_stream_read_error")
        yield _relay_chunk("complete response", status="completed")

    monkeypatch.setattr(ChatOpenAI, "_astream", stream)

    response = asyncio.run(_relay_model().ainvoke("Analyze market conditions"))

    assert calls == 2
    assert response.content == "complete response"

