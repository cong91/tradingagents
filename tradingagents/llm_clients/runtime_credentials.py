"""In-process LLM credential resolution for tier-specific providers.

Vault entries are installed here by the control-panel server at startup and
when an operator saves or deletes a credential. The resolver never writes
credentials to ``os.environ``; provider clients receive an explicit key only
when one is available for the selected tier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from .api_key_env import get_api_key_env

CredentialSource = Literal["vault", "env", "ambient", "none"]

# A credential is keyed by the tier that consumes it and the provider that
# serves that tier. This allows quick and deep tiers to use different accounts.
_credentials: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class ResolvedCredential:
    """The effective credential source; ``value`` is kept in-process only."""

    value: str | None
    source: CredentialSource


def _normalize_tier(tier: str) -> str:
    key = str(tier).strip().lower()
    if key not in {"quick", "deep"}:
        raise ValueError("tier must be 'quick' or 'deep'")
    return key


def _normalize_provider(provider: str) -> str:
    key = str(provider).strip().lower()
    if not key:
        raise ValueError("provider is required")
    return key


def set_credential(tier: str, provider: str, api_key: str) -> None:
    """Install one decrypted vault credential for immediate new runs."""
    tier_key = _normalize_tier(tier)
    provider_key = _normalize_provider(provider)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")
    _credentials[(tier_key, provider_key)] = api_key


def clear_credential(tier: str, provider: str) -> None:
    """Remove one vault credential; provider env/ambient auth can resume."""
    _credentials.pop((_normalize_tier(tier), _normalize_provider(provider)), None)


def clear_all() -> None:
    """Clear all installed vault credentials before a startup reload."""
    _credentials.clear()


def stored_credential(tier: str, provider: str) -> str | None:
    """Return the installed vault value for internal resolver/storage use."""
    return _credentials.get((_normalize_tier(tier), _normalize_provider(provider)))


def resolve_credential(provider: str, tier: str) -> ResolvedCredential:
    """Resolve ``vault > provider env > provider ambient`` without env writes."""
    tier_key = _normalize_tier(tier)
    provider_key = _normalize_provider(provider)
    vault_key = _credentials.get((tier_key, provider_key))
    if vault_key:
        return ResolvedCredential(vault_key, "vault")

    env_var = get_api_key_env(provider_key)
    if env_var:
        env_key = os.environ.get(env_var)
        if env_key:
            return ResolvedCredential(env_key, "env")

    if provider_key in {"chatgpt", "bedrock"}:
        return ResolvedCredential(None, "ambient")
    return ResolvedCredential(None, "none")


def provider_api_key(provider: str, tier: str) -> str | None:
    """Return only the effective key value for a provider client."""
    return resolve_credential(provider, tier).value
