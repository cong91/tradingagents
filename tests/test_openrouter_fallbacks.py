"""OpenRouter: Claude structured-output method and per-tier model fallbacks."""

import pytest

from tradingagents.graph.trading_graph import _fallback_models
from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.factory import create_llm_client


@pytest.mark.unit
@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "anthropic/claude-fable-5.1"],
)
def test_claude_without_forced_tool_choice_uses_json_schema(model):
    caps = get_capabilities(model)
    assert caps.preferred_structured_method == "json_schema"
    assert caps.supports_tool_choice is False


@pytest.mark.unit
def test_claude_haiku_keeps_function_calling():
    caps = get_capabilities("anthropic/claude-haiku-5.5")
    assert caps.preferred_structured_method == "function_calling"
    assert caps.supports_tool_choice is True


@pytest.mark.unit
def test_fallback_models_parse_comma_separated_and_lists():
    assert _fallback_models({"quick_fallback_models": " a/x:free, b/y ,,"}, "quick") == ["a/x:free", "b/y"]
    assert _fallback_models({"deep_fallback_models": ["c/z"]}, "deep") == ["c/z"]
    assert _fallback_models({}, "quick") == []


@pytest.fixture
def openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


@pytest.mark.unit
def test_openrouter_sends_primary_first_models_list(openrouter_key):
    llm = create_llm_client(
        provider="openrouter", model="a/primary:free", fallback_models=["b/second:free", "a/primary:free"],
    ).get_llm()
    assert llm.extra_body == {"models": ["a/primary:free", "b/second:free"]}


@pytest.mark.unit
def test_openrouter_models_list_is_capped_at_three(openrouter_key):
    llm = create_llm_client(
        provider="openrouter", model="m0", fallback_models=["m1", "m2", "m3", "m4"],
    ).get_llm()
    assert llm.extra_body == {"models": ["m0", "m1", "m2"]}


@pytest.mark.unit
def test_fallbacks_are_ignored_for_other_providers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = create_llm_client(provider="openai", model="gpt-6-luna", fallback_models=["x"]).get_llm()
    assert not (llm.extra_body or {}).get("models")
