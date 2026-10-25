import json
import logging
import os
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model

logger = logging.getLogger(__name__)

# Connection-level retry for community relays that drop connections
# intermittently. The openai SDK only retries HTTP status codes; a server
# that disconnects before sending headers produces an APIConnectionError
# with no status, so the SDK gives up on the first attempt. The wait grows
# linearly because relay overload windows last seconds-to-minutes: a fixed
# half-second gap retries into the same overload and burns both attempts.
_CONNECTION_RETRIES = 2  # one initial request plus one bounded reconnect
_CONNECTION_RETRY_WAIT = 4.0
_DEFAULT_REQUEST_TIMEOUT = 60.0


class _IncompleteStreamError(RuntimeError):
    """The Responses relay ended without a successful terminal event."""

    def __init__(self, status: str | None, reason: str | None = None):
        self.retryable = reason is None
        detail = f" with reason {reason!r}" if reason is not None else ""
        super().__init__(f"Responses stream ended with status {status!r}{detail}")


def _is_connection_error(exc: Exception) -> bool:
    """Whether an exception is a network/connection failure worth retrying."""
    current = exc
    for _ in range(5):
        name = type(current).__name__
        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "timed out",
                "upstream_http2_stream_error",
                "upstream_stream_read_error",
                "upstream response stream was interrupted",
                # Relay-side overload answers (HTTP 5xx equivalents): bounded
                # retry applies the same as a dropped connection.
                "server_error",
                "overloaded",
            )
        ):
            return True
        if isinstance(current, json.JSONDecodeError) or name in (
            "APIConnectionError", "APIConnectionErrorWrapper",
            "OpenAIConnectionError", "OpenAITimeoutError", "RemoteProtocolError",
            "ConnectError", "ConnectionError", "TimeoutError",
        ):
            return True
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None or cause is current:
            break
        current = cause
    return False


def _is_retryable_stream_error(exc: Exception) -> bool:
    if isinstance(exc, _IncompleteStreamError):
        return exc.retryable
    return _is_connection_error(exc)


def _incomplete_reason(metadata: dict) -> str | None:
    details = metadata.get("incomplete_details")
    if isinstance(details, dict):
        reason = details.get("reason")
    else:
        reason = getattr(details, "reason", None)
    return str(reason) if reason is not None else None


def _stream_terminal_metadata(chunk: Any) -> tuple[str | None, str | None]:
    message = getattr(chunk, "message", None)
    metadata = getattr(message, "response_metadata", {}) or {}
    return metadata.get("status"), _incomplete_reason(metadata)


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and capability-aware binding.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling.

    ``with_structured_output`` consults the per-model capability table
    (``capabilities.get_capabilities``) to pick the method and to decide
    whether ``tool_choice`` may be sent. Models that reject ``tool_choice``
    (e.g. DeepSeek V4 and reasoner — per their official tool-calling
    guide) still bind the schema as a tool, but no ``tool_choice``
    parameter is sent.

    Provider-specific quirks beyond structured-output (e.g. DeepSeek's
    reasoning_content roundtrip) live in subclasses so this base class
    stays small.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # When the model rejects tool_choice, suppress langchain's hardcoded
        # value. The schema is still bound as a tool — exactly what
        # DeepSeek's official tool-calling examples do.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


class LocalCompatibleChatOpenAI(NormalizedChatOpenAI):
    """OpenAI-compatible client for arbitrary local servers (LM Studio, vLLM,
    llama.cpp via the generic ``openai_compatible`` provider) and community
    relays.

    Their tool-calling support varies, and many reject the object-form
    ``tool_choice`` langchain sends for function-calling structured output. Bind
    the schema as a tool but don't force tool_choice, so structured output works
    across local servers regardless of the model ID's capabilities (#1057).
    """

    def __init__(self, **kwargs: Any) -> None:
        # Community relays often abort keep-alive connections mid-pipeline
        # ("Server disconnected without sending a response" on the second and
        # later requests of a pooled connection). One fresh connection per
        # request is what survives them; local servers don't mind.
        headers = dict(kwargs.pop("default_headers", None) or {})
        headers.setdefault("Connection", "close")
        kwargs.setdefault("timeout", _DEFAULT_REQUEST_TIMEOUT)
        kwargs.setdefault("streaming", True)
        if "stream_chunk_timeout" in getattr(type(self), "model_fields", {}):
            kwargs.setdefault("stream_chunk_timeout", kwargs["timeout"])
        super().__init__(default_headers=headers, **kwargs)

    def _use_responses_api(self, payload: dict) -> bool:
        """Keep generic endpoints on Chat Completions by default, even for
        GPT-6 + tools.

        LangChain auto-selects ``/responses`` for GPT-6 when tools are bound;
        a third-party OpenAI-compatible endpoint may advertise GPT-6 models
        while only implementing Chat Completions. An explicit
        ``use_responses_api`` (from ``llm_wire_protocol=responses``) still
        wins over this default.
        """
        if isinstance(self.use_responses_api, bool):
            return self.use_responses_api
        return False

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        """Buffer each relay attempt and publish only a complete attempt."""
        responses_mode = self._use_responses_api({**kwargs, **self.model_kwargs})
        for attempt in range(_CONNECTION_RETRIES):
            chunks: list[Any] = []
            terminal_status = None
            incomplete_reason = None
            try:
                for chunk in super()._stream(*args, **kwargs):
                    chunks.append(chunk)
                    status, reason = _stream_terminal_metadata(chunk)
                    terminal_status = status or terminal_status
                    incomplete_reason = reason or incomplete_reason
                if responses_mode and terminal_status != "completed":
                    raise _IncompleteStreamError(terminal_status, incomplete_reason)
                yield from chunks
                return
            except Exception as exc:
                if (
                    not _is_retryable_stream_error(exc)
                    or attempt == _CONNECTION_RETRIES - 1
                ):
                    raise
                wait = _CONNECTION_RETRY_WAIT * (attempt + 1)
                logger.warning(
                    "relay stream attempt failed before completion "
                    "(attempt %d/%d, %s: %s); retrying in %.1fs",
                    attempt + 1,
                    _CONNECTION_RETRIES,
                    type(exc).__name__,
                    str(exc)[:200],
                    wait,
                )
                if wait:
                    import time as _time

                    _time.sleep(wait)

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """Buffer each async relay attempt and publish only a complete attempt."""
        import asyncio

        responses_mode = self._use_responses_api({**kwargs, **self.model_kwargs})
        for attempt in range(_CONNECTION_RETRIES):
            chunks: list[Any] = []
            terminal_status = None
            incomplete_reason = None
            try:
                async for chunk in super()._astream(*args, **kwargs):
                    chunks.append(chunk)
                    status, reason = _stream_terminal_metadata(chunk)
                    terminal_status = status or terminal_status
                    incomplete_reason = reason or incomplete_reason
                if responses_mode and terminal_status != "completed":
                    raise _IncompleteStreamError(terminal_status, incomplete_reason)
                for chunk in chunks:
                    yield chunk
                return
            except Exception as exc:
                if (
                    not _is_retryable_stream_error(exc)
                    or attempt == _CONNECTION_RETRIES - 1
                ):
                    raise
                wait = _CONNECTION_RETRY_WAIT * (attempt + 1)
                logger.warning(
                    "async relay stream attempt failed before completion "
                    "(attempt %d/%d, %s: %s); retrying in %.1fs",
                    attempt + 1,
                    _CONNECTION_RETRIES,
                    type(exc).__name__,
                    str(exc)[:200],
                    wait,
                )
                if wait:
                    await asyncio.sleep(wait)

    def with_structured_output(self, schema, *, method=None, **kwargs):
        resolved = method or get_capabilities(self.model_name).preferred_structured_method
        if resolved == "function_calling":
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Thinking-mode round-trip is the only DeepSeek-specific behavior that
    stays here. When DeepSeek's thinking models return a response with
    ``reasoning_content``, that field must be echoed back as part of the
    assistant message on the next turn or the API fails with HTTP 400.
    ``_create_chat_result`` captures it on receive and
    ``_get_request_payload`` re-attaches it on send.

    Tool-choice handling for V4 and reasoner — those models reject the
    ``tool_choice`` parameter — is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_), strict=False):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", []), strict=False
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax-specific overrides on top of the OpenAI-compatible client.

    M2.x reasoning models embed ``<think>...</think>`` blocks directly in
    ``message.content`` by default, which would pollute saved reports.
    Per platform.minimax.io/docs/api-reference/text-openai-api,
    ``reasoning_split=True`` redirects the thinking block into
    ``reasoning_details`` so ``content`` stays clean. It is sent via
    ``extra_body`` (not a top-level kwarg) because the openai SDK validates
    top-level params and rejects unknown ones like reasoning_split (#826).

    The flag is gated by ``ModelCapabilities.requires_reasoning_split`` so
    only M2.x reasoning models receive it; non-reasoning MiniMax endpoints
    (Coding Plan, MiniMax-Text-01) never see it.

    Tool-choice handling for M2.x — those models accept only the string
    enum ``{"none", "auto"}`` and reject langchain's function-spec dict —
    is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if get_capabilities(self.model_name).requires_reasoning_split:
            # Pass via extra_body, not as a top-level kwarg: the openai SDK
            # (>=1.56) validates top-level params against Completions.create
            # and rejects unknown ones like reasoning_split (#826). extra_body
            # is forwarded into the request body untouched.
            extra_body = payload.setdefault("extra_body", {})
            extra_body.setdefault("reasoning_split", True)
        return payload


# Kwargs forwarded from user config to ChatOpenAI
# OpenRouter's documented ceiling for the request-level "models" list.
_OPENROUTER_MAX_MODELS = 3

_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "temperature", "max_tokens",
    "api_key", "callbacks", "http_client", "http_async_client",
    "use_responses_api",
)

# OpenAI's ``reasoning_effort`` is only accepted by reasoning models — GPT-5 and
# later, and the o-series. Non-reasoning models (gpt-4.1, gpt-4o, ...) 400 with
# "Unsupported parameter: 'reasoning.effort' is not supported with this model".
# Drop the kwarg for those rather than crash the run.
_OPENAI_REASONING_MODEL = re.compile(r"^(?:gpt-(?:[5-9]|[1-9]\d)|o[1-9])(?:[.-]|$)")


def _supports_reasoning_effort(model: str) -> bool:
    """Whether the (native OpenAI) model accepts ``reasoning_effort``."""
    return bool(_OPENAI_REASONING_MODEL.match(model.lower().strip()))


@dataclass(frozen=True)
class ProviderSpec:
    """Declarative config for one OpenAI-compatible provider.

    The OpenAI-compatible family (OpenAI, xAI, DeepSeek, Qwen, GLM, MiniMax,
    OpenRouter, Ollama, and any user endpoint) all speak the same Chat
    Completions API and differ only by these fields — so one row here replaces
    the former per-provider base-URL dict, auth handling, and client-class
    branches. Native Anthropic / Google use their own clients (genuinely
    different APIs) and are intentionally NOT in this registry.

    The API-key env var stays in ``api_key_env.PROVIDER_API_KEY_ENV`` (the single
    source consulted by both this client and the CLI prompt); only behavior that
    is provider-specific (base URL, key optionality, wire-format quirks via
    ``chat_class``) lives here.
    """

    chat_class: type = NormalizedChatOpenAI   # provider quirks live in the subclass
    base_url: str | None = None            # default endpoint (None -> SDK default)
    base_url_env: str | None = None        # env var that overrides base_url (e.g. OLLAMA_BASE_URL)
    key_optional: bool = False                # don't require/prompt; send a placeholder if unset
    placeholder_key: str = "EMPTY"            # sent when no key is available (keyless local servers)
    require_base_url: bool = False            # error if no base_url is resolved (generic endpoint)
    use_responses_api: bool = False           # native OpenAI Responses API


# Single source of truth for the OpenAI-compatible provider family. Dual-region
# providers (qwen/glm/minimax) keep separate endpoints because international and
# China accounts cannot share credentials (#758).
OPENAI_COMPATIBLE_PROVIDERS: dict[str, ProviderSpec] = {
    "openai":     ProviderSpec(use_responses_api=True),
    "xai":        ProviderSpec(base_url="https://api.x.ai/v1"),
    "deepseek":   ProviderSpec(base_url="https://api.deepseek.com", chat_class=DeepSeekChatOpenAI),
    "qwen":       ProviderSpec(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "qwen-cn":    ProviderSpec(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":        ProviderSpec(base_url="https://api.z.ai/api/paas/v4/"),
    "glm-cn":     ProviderSpec(base_url="https://open.bigmodel.cn/api/paas/v4/"),
    "minimax":    ProviderSpec(base_url="https://api.minimax.io/v1", chat_class=MinimaxChatOpenAI),
    "minimax-cn": ProviderSpec(base_url="https://api.minimaxi.com/v1", chat_class=MinimaxChatOpenAI),
    "openrouter": ProviderSpec(base_url="https://openrouter.ai/api/v1"),
    "mistral":    ProviderSpec(base_url="https://api.mistral.ai/v1"),
    "kimi":       ProviderSpec(base_url="https://api.moonshot.ai/v1"),
    "groq":       ProviderSpec(base_url="https://api.groq.com/openai/v1"),
    "nvidia":     ProviderSpec(base_url="https://integrate.api.nvidia.com/v1"),
    "ollama":     ProviderSpec(base_url="http://localhost:11434/v1", base_url_env="OLLAMA_BASE_URL",
                               key_optional=True, placeholder_key="ollama",
                               chat_class=LocalCompatibleChatOpenAI),
    # Generic endpoint: user supplies base_url; key optional (keyless local).
    "openai_compatible": ProviderSpec(
        require_base_url=True, key_optional=True, chat_class=LocalCompatibleChatOpenAI
    ),
}


def is_openai_compatible(provider: str) -> bool:
    """Whether ``provider`` is served by the OpenAI-compatible registry."""
    return provider.lower() in OPENAI_COMPATIBLE_PROVIDERS


def _is_native_openai_base_url(base_url: str | None) -> bool:
    """True when ``base_url`` is unset or points at api.openai.com.

    The Responses API (/v1/responses) only exists on native OpenAI. A custom
    base_url on the ``openai`` provider (a proxy, gateway, or local server)
    speaks only Chat Completions, so the Responses API must stay off there even
    though the provider spec enables it (#1024).
    """
    if not base_url:
        return True
    if "://" not in base_url:
        base_url = "https://" + base_url
    host = urlparse(base_url).hostname or ""
    return host == "api.openai.com" or host.endswith(".openai.com")


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return a configured ChatOpenAI instance, driven by the provider registry."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(self.provider)
        chat_cls = NormalizedChatOpenAI

        if spec is not None:
            chat_cls = spec.chat_class

            # base_url precedence: explicit client base_url (carries the config /
            # TRADINGAGENTS_LLM_BACKEND_URL value) > provider env override (e.g.
            # OLLAMA_BASE_URL) > provider default. None means use the SDK default.
            env_base_url = os.environ.get(spec.base_url_env) if spec.base_url_env else None
            base_url = self.base_url or env_base_url or spec.base_url
            if spec.require_base_url and not base_url:
                raise ValueError(
                    f"Provider '{self.provider}' requires a base_url. Set it via "
                    "backend_url / TRADINGAGENTS_LLM_BACKEND_URL to your endpoint, "
                    "e.g. http://localhost:8000/v1 (vLLM) or http://localhost:1234/v1 "
                    "(LM Studio)."
                )
            if base_url:
                llm_kwargs["base_url"] = base_url

            # API key: required unless key_optional; keyless local servers get a
            # placeholder. The env-var name is the single source in api_key_env.
            api_key_env = get_api_key_env(self.provider)
            api_key = os.environ.get(api_key_env) if api_key_env else None
            if api_key:
                llm_kwargs["api_key"] = api_key
            elif spec.key_optional:
                llm_kwargs["api_key"] = spec.placeholder_key
            elif api_key_env:
                raise ValueError(
                    f"API key for provider '{self.provider}' is not set. "
                    f"Please set the {api_key_env} environment variable "
                    f"(e.g. add {api_key_env}=your_key to your .env file)."
                )

            # The Responses API only exists on native OpenAI; if the user points
            # the openai provider at a custom base_url (proxy/gateway/local), it
            # only speaks Chat Completions, so keep Responses off there (#1024).
            if spec.use_responses_api and _is_native_openai_base_url(base_url):
                llm_kwargs["use_responses_api"] = True
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "reasoning_effort" and not _supports_reasoning_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        # OpenRouter model fallbacks: the request's "models" list is tried in
        # order (primary first) when an upstream is rate-limited or down.
        # OpenRouter 400s on more than 3 entries, so the chain is truncated.
        fallbacks = self.kwargs.get("fallback_models")
        if self.provider == "openrouter" and fallbacks:
            chain = [self.model] + [m for m in fallbacks if m != self.model]
            if len(chain) > _OPENROUTER_MAX_MODELS:
                logger.warning(
                    "OpenRouter accepts at most %d models per request; dropping %s",
                    _OPENROUTER_MAX_MODELS, chain[_OPENROUTER_MAX_MODELS:],
                )
                chain = chain[:_OPENROUTER_MAX_MODELS]
            llm_kwargs["extra_body"] = {"models": chain}

        llm_kwargs["streaming"] = True
        if "stream_chunk_timeout" in getattr(chat_cls, "model_fields", {}):
            llm_kwargs.setdefault(
                "stream_chunk_timeout",
                llm_kwargs.get("timeout", _DEFAULT_REQUEST_TIMEOUT),
            )

        # The subclass (provider quirks) comes from the registry spec.
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
