"""Provider labels, endpoints, auth modes, and tier model options for the API."""

from __future__ import annotations

from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "google": "Google",
    "anthropic": "Anthropic",
    "chatgpt": "ChatGPT (Plus or Pro plan)",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "qwen-cn": "Qwen China",
    "glm": "GLM",
    "glm-cn": "GLM China",
    "minimax": "MiniMax",
    "minimax-cn": "MiniMax China",
    "openrouter": "OpenRouter",
    "mistral": "Mistral",
    "kimi": "Kimi (Moonshot)",
    "groq": "Groq",
    "nvidia": "NVIDIA NIM",
    "azure": "Azure OpenAI",
    "bedrock": "Amazon Bedrock",
    "ollama": "Ollama",
    "openai_compatible": "OpenAI-compatible",
}

_DEFAULT_BACKEND_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/",
    "chatgpt": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "qwen-cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm": "https://api.z.ai/api/paas/v4/",
    "glm-cn": "https://open.bigmodel.cn/api/paas/v4/",
    "minimax": "https://api.minimax.io/v1",
    "minimax-cn": "https://api.minimaxi.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "ollama": "http://localhost:11434/v1",
}

_BACKEND_URL_ENVS = {
    "azure": "AZURE_OPENAI_ENDPOINT",
    "ollama": "OLLAMA_BASE_URL",
}


def _auth_mode(provider: str) -> str:
    if provider == "chatgpt":
        return "oauth"
    if provider == "bedrock":
        return "aws_credential_chain"
    if provider == "ollama":
        return "none"
    if provider == "openai_compatible":
        return "optional_api_key"
    return "api_key"


def providers() -> dict[str, dict]:
    """Return the provider-keyed catalog used by the control-panel API."""
    result = {}
    for provider, label in _PROVIDER_LABELS.items():
        options = MODEL_OPTIONS.get(provider, {
            "quick": [("Custom model ID", "custom")],
            "deep": [("Custom model ID", "custom")],
        })
        result[provider] = {
            "id": provider,
            "label": label,
            "models": {
                tier: [{"label": name, "id": model} for name, model in options[tier]]
                for tier in ("quick", "deep")
            },
            "default_backend_url": _DEFAULT_BACKEND_URLS.get(provider),
            "requires_backend_url": provider == "openai_compatible",
            "auth_mode": _auth_mode(provider),
            "credential_env_var": (
                "AWS_BEARER_TOKEN_BEDROCK" if provider == "bedrock"
                else get_api_key_env(provider)
            ),
            "credential_supported": provider not in {"chatgpt", "bedrock", "ollama"},
            "credential_required": provider not in {"chatgpt", "bedrock", "ollama", "openai_compatible"},
        }
    return result


def provider_ids() -> tuple[str, ...]:
    """Canonical provider identifiers accepted by LLM credential endpoints."""
    return tuple(_PROVIDER_LABELS)


def get_provider(provider: str) -> dict | None:
    """One provider's metadata, or ``None`` for an unknown identifier."""
    return providers().get(str(provider).strip().lower())
