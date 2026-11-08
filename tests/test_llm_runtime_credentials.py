"""Unit coverage for tier-scoped LLM credential resolution."""

import pytest

from tradingagents.llm_clients import provider_metadata, runtime_credentials

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_runtime_credentials():
    runtime_credentials.clear_all()
    yield
    runtime_credentials.clear_all()


def test_vault_credential_wins_over_provider_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    runtime_credentials.set_credential("quick", "openai", "vault-quick-key")

    resolved = runtime_credentials.resolve_credential("openai", "quick")

    assert resolved.value == "vault-quick-key"
    assert resolved.source == "vault"


def test_credentials_are_scoped_by_tier(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    runtime_credentials.set_credential("quick", "openai", "vault-quick-key")

    deep = runtime_credentials.resolve_credential("openai", "deep")

    assert deep.value == "env-openai-key"
    assert deep.source == "env"


def test_provider_metadata_exposes_models_without_credentials():
    catalog = provider_metadata.providers()

    assert catalog["openai"]["models"]["quick"]
    assert catalog["openai"]["models"]["deep"]
    assert "credential" not in catalog["openai"]
    assert catalog["openai"]["credential_env_var"] == "OPENAI_API_KEY"


def test_keyless_provider_resolves_to_ambient_source():
    resolved = runtime_credentials.resolve_credential("bedrock", "deep")

    assert resolved.value is None
    assert resolved.source == "ambient"
