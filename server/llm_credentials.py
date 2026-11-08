"""Encrypted persistence and metadata for tier-specific LLM API keys."""

from __future__ import annotations

import logging
import os
from typing import Any

from server import credential_vault, postgres as db
from server.contract import utc_now_iso
from tradingagents.llm_clients import provider_metadata, runtime_credentials
from tradingagents.llm_clients.api_key_env import get_api_key_env

logger = logging.getLogger(__name__)
_TIERS = ("quick", "deep")


def _check_provider(provider: str) -> str:
    key = str(provider).strip().lower()
    if key not in provider_metadata.provider_ids():
        raise ValueError(f"unknown provider {provider!r}")
    return key


def _check_tier(tier: str) -> str:
    key = str(tier).strip().lower()
    if key not in _TIERS:
        raise ValueError("tier must be 'quick' or 'deep'")
    return key


def _credential_support(provider: str) -> dict:
    metadata = provider_metadata.get_provider(provider)
    if metadata is None or not metadata["credential_supported"]:
        raise ValueError(f"provider {provider!r} does not accept a stored API key")
    return metadata


def _mask(api_key: str | None) -> tuple[str | None, str | None]:
    if not api_key:
        return None, None
    suffix = api_key[-4:]
    return f"••••{suffix}", suffix


def _stored_row(tier: str, provider: str) -> dict | None:
    with db.read() as conn:
        return conn.execute(
            "SELECT secret, updated_at FROM llm_credentials WHERE tier = %s AND provider = %s",
            (tier, provider),
        ).fetchone()


def _decrypt(row: dict | None) -> str | None:
    if row is None:
        return None
    try:
        payload = credential_vault.decrypt_json(row["secret"])
    except ValueError:
        return None
    api_key = payload.get("api_key") if isinstance(payload, dict) else None
    return api_key if isinstance(api_key, str) and api_key else None


def save_credential(
    tier: str,
    provider: str,
    api_key: Any,
    *,
    updated_by: str | None = None,
) -> dict:
    """Validate, encrypt and persist one provider key for one model tier."""
    tier_key = _check_tier(tier)
    provider_key = _check_provider(provider)
    _credential_support(provider_key)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")
    secret_value = api_key.strip()
    secret = credential_vault.encrypt_json({"api_key": secret_value})
    with db.session() as conn:
        conn.execute(
            """INSERT INTO llm_credentials (tier, provider, secret, updated_at, updated_by)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (tier, provider) DO UPDATE SET
                   secret = excluded.secret,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            (tier_key, provider_key, secret, utc_now_iso(), updated_by),
        )
    runtime_credentials.set_credential(tier_key, provider_key, secret_value)
    return credential_summary(tier_key, provider_key)


def delete_credential(tier: str, provider: str) -> bool:
    """Remove one stored key; environment/ambient authentication can resume."""
    tier_key = _check_tier(tier)
    provider_key = _check_provider(provider)
    _credential_support(provider_key)
    with db.session() as conn:
        cursor = conn.execute(
            "DELETE FROM llm_credentials WHERE tier = %s AND provider = %s",
            (tier_key, provider_key),
        )
    runtime_credentials.clear_credential(tier_key, provider_key)
    return cursor.rowcount == 1


def _ambient_configured(provider: str) -> bool:
    if provider == "chatgpt":
        from tradingagents.llm_clients import chatgpt_auth

        return chatgpt_auth.signed_in_as() is not None
    # AWS can supply credentials from profiles, web identity, container/task
    # roles, or instance roles; probing the SDK chain here could make a metadata
    # request, so report the ambient mechanism as available but unverifiable.
    return provider == "bedrock"


def credential_summary(tier: str, provider: str) -> dict:
    """Metadata for one tier/provider pair; raw key values never leave here."""
    tier_key = _check_tier(tier)
    provider_key = _check_provider(provider)
    metadata = provider_metadata.get_provider(provider_key)
    if not metadata["credential_supported"]:
        if provider_key == "bedrock" and os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            source = "env"
            configured = True
        elif _ambient_configured(provider_key):
            source = "ambient"
            configured = True
        else:
            source = "none"
            configured = False
        return {
            "tier": tier_key,
            "provider": provider_key,
            "supported": False,
            "required": metadata["credential_required"],
            "env_var": metadata["credential_env_var"],
            "configured": configured,
            "source": source,
            "masked": None,
            "last4": None,
            "updated_at": None,
            "decryptable": False,
        }

    row = _stored_row(tier_key, provider_key)
    stored = _decrypt(row)
    env_var = get_api_key_env(provider_key)
    env_key = os.environ.get(env_var) if env_var else None
    if stored:
        effective, source = stored, "vault"
    elif env_key:
        effective, source = env_key, "env"
    elif metadata["auth_mode"] in {"aws_credential_chain", "oauth"}:
        effective, source = None, "ambient"
    else:
        effective, source = None, "none"
    masked, last4 = _mask(effective)
    return {
        "tier": tier_key,
        "provider": provider_key,
        "supported": True,
        "required": metadata["credential_required"],
        "env_var": env_var,
        "configured": bool(effective),
        "source": source,
        "masked": masked,
        "last4": last4,
        "updated_at": row["updated_at"] if stored and row else None,
        "decryptable": row is None or stored is not None,
    }


def credential_summaries() -> list[dict]:
    """Return metadata-only rows for every tier/provider pair."""
    return [
        credential_summary(tier, provider)
        for tier in _TIERS
        for provider in provider_metadata.provider_ids()
    ]


def provider_catalog() -> dict:
    """Provider-keyed metadata, effective tier settings and credential status."""
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients.factory import tier_provider

    config = get_config()
    catalog = provider_metadata.providers()
    for provider, metadata in catalog.items():
        metadata["credential"] = {
            tier: credential_summary(tier, provider) for tier in _TIERS
        }

    tiers = {}
    for tier in _TIERS:
        provider = tier_provider(config, tier)
        model = config.get(f"{tier}_think_llm")
        if provider == str(config.get("llm_provider", "")).lower():
            backend_url = config.get(f"{tier}_think_backend_url") or config.get("backend_url")
        else:
            backend_url = config.get(f"{tier}_think_backend_url")
        metadata = catalog.get(provider)
        if metadata and backend_url is None:
            backend_url = metadata["default_backend_url"]
            if provider == "ollama":
                backend_url = os.environ.get("OLLAMA_BASE_URL") or backend_url
        tiers[tier] = {
            "provider": provider,
            "model": model,
            "backend_url": backend_url,
        }
    return {"generated_at": utc_now_iso(), "providers": catalog, "tiers": tiers}


def push_to_engine() -> list[str]:
    """Load decryptable vault keys at startup; return failed tier/provider ids."""
    runtime_credentials.clear_all()
    failures: list[str] = []
    try:
        with db.read() as conn:
            rows = conn.execute(
                "SELECT tier, provider, secret FROM llm_credentials"
            ).fetchall()
    except Exception:
        logger.warning("LLM credentials not loaded from PostgreSQL")
        return failures

    for row in rows:
        label = f"{row['tier']}/{row['provider']}"
        try:
            tier = _check_tier(row["tier"])
            provider = _check_provider(row["provider"])
            _credential_support(provider)
            payload = credential_vault.decrypt_json(row["secret"])
            api_key = payload.get("api_key") if isinstance(payload, dict) else None
            if not isinstance(api_key, str) or not api_key.strip():
                raise ValueError("stored credential payload is invalid")
            runtime_credentials.set_credential(tier, provider, api_key)
        except ValueError:
            failures.append(label)
    return failures
