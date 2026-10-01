"""Multi-provider tiers: quick/deep may run on different providers.

The per-tier keys (``quick_provider``/``deep_provider`` and the matching
``*_backend_url``) default to the global ``llm_provider`` / ``backend_url``
when unset; env rows apply through ``_apply_env_overrides`` like any other
key. API keys themselves resolve per provider via
``llm_clients.api_key_env.PROVIDER_API_KEY_ENV`` and are not touched here.
"""

import pytest

from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.graph.trading_graph import _tier_llm_settings
from tradingagents.llm_clients.factory import build_llm_kwargs


@pytest.mark.unit
def test_tier_settings_fall_back_to_global_provider():
    config = dict(DEFAULT_CONFIG)
    assert _tier_llm_settings(config, "quick") == ("openai", None)
    assert _tier_llm_settings(config, "deep") == ("openai", None)


@pytest.mark.unit
def test_tier_settings_use_per_tier_overrides():
    config = dict(
        DEFAULT_CONFIG,
        llm_provider="openai",
        backend_url="https://relay.example/v1",
        quick_provider="glm",
        deep_provider="anthropic",
        deep_backend_url="https://anthropic-mirror.example",
    )
    assert _tier_llm_settings(config, "quick") == ("glm", "https://relay.example/v1")
    assert _tier_llm_settings(config, "deep") == (
        "anthropic",
        "https://anthropic-mirror.example",
    )


@pytest.mark.unit
def test_tier_backend_url_falls_back_independently(monkeypatch):
    """A tier may override only the provider (keep global URL) or only the URL."""
    config = dict(
        DEFAULT_CONFIG,
        llm_provider="openai",
        backend_url="https://relay.example/v1",
        quick_provider="deepseek",
    )
    assert _tier_llm_settings(config, "quick") == (
        "deepseek",
        "https://relay.example/v1",
    )


@pytest.mark.unit
def test_env_overrides_apply_to_tier_keys(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_QUICK_PROVIDER", "glm")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_PROVIDER", "anthropic")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_BACKEND_URL", "https://mirror.example")
    config = _apply_env_overrides(dict(DEFAULT_CONFIG))
    assert config["quick_provider"] == "glm"
    assert config["deep_provider"] == "anthropic"
    assert config["deep_backend_url"] == "https://mirror.example"


@pytest.mark.unit
def test_build_llm_kwargs_per_tier_provider():
    """Thinking knobs key off the tier's provider, not the global one."""
    config = {
        "llm_provider": "openai",
        "anthropic_effort": "high",
        "openai_reasoning_effort": "medium",
        "google_thinking_level": "high",
        "temperature": 0.2,
    }
    deep_kwargs = build_llm_kwargs(config, "anthropic")
    assert deep_kwargs == {"effort": "high", "temperature": 0.2}
    quick_kwargs = build_llm_kwargs(config, "openai")
    assert quick_kwargs == {"reasoning_effort": "medium", "temperature": 0.2}


@pytest.mark.unit
def test_build_llm_kwargs_wire_protocol_only_for_openai_compatible():
    """`responses` applies to the OpenAI-compatible family; native APIs skip it."""
    config = {"llm_provider": "anthropic", "llm_wire_protocol": "responses"}
    assert "use_responses_api" not in build_llm_kwargs(config, "anthropic")
    assert "use_responses_api" in build_llm_kwargs(config, "openai_compatible")
    assert "use_responses_api" in build_llm_kwargs(config, "openai")


@pytest.mark.unit
def test_build_llm_kwargs_without_provider_arg_unchanged():
    """Legacy single-argument call keeps the global-provider behavior."""
    config = {"llm_provider": "google", "google_thinking_level": "low"}
    assert build_llm_kwargs(config) == {"thinking_level": "low"}
