"""LLM provider catalog and tier-specific API-key management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from server import llm_credentials
from server.contract import ApiError, json_body, utc_now_iso

router = APIRouter(prefix="/api", tags=["llm-credentials"])


@router.get("/llm-config")
def get_llm_config():
    """Provider catalog and current quick/deep settings, with masked key state."""
    return llm_credentials.provider_catalog()


@router.get("/llm-credentials")
def get_llm_credentials():
    """Credential metadata per tier/provider; raw API keys are never returned."""
    return {
        "generated_at": utc_now_iso(),
        "credentials": llm_credentials.credential_summaries(),
    }


@router.put("/llm-credentials/{tier}/{provider}")
async def put_llm_credential(tier: str, provider: str, request: Request):
    """Encrypt and save one API key. Body: ``{api_key, actor?}``."""
    from server import audit as audit_log

    body = await json_body(request)
    api_key = body.get("api_key")
    actor = body.get("actor")
    if actor is not None and not isinstance(actor, str):
        raise ApiError(422, "invalid_credentials", "actor must be a string")
    try:
        summary = llm_credentials.save_credential(
            tier, provider, api_key, updated_by=actor,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_credentials", str(exc)) from exc

    audit_log.record(
        "llm_credential_updated", tier=summary["tier"], provider=summary["provider"],
    )
    return {"generated_at": utc_now_iso(), **summary}


@router.delete("/llm-credentials/{tier}/{provider}")
async def delete_llm_credential(tier: str, provider: str, request: Request):
    """Delete one stored key. Body is ``{}``; env/ambient auth may then resume."""
    from server import audit as audit_log

    await json_body(request)
    try:
        removed = llm_credentials.delete_credential(tier, provider)
        summary = llm_credentials.credential_summary(tier, provider)
    except ValueError as exc:
        raise ApiError(404, "unknown_credential", str(exc)) from exc

    audit_log.record(
        "llm_credential_removed", tier=summary["tier"], provider=summary["provider"],
    )
    return {
        "generated_at": utc_now_iso(),
        "tier": summary["tier"],
        "provider": summary["provider"],
        "removed": removed,
        "configured": summary["configured"],
        "source": summary["source"],
    }
