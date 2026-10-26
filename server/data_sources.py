"""GET/PUT/DELETE /api/data-sources — session cookies for Reddit and X.

The operator pastes a browser cookie export (JSON) and the server stores it
encrypted (``server/credential_vault.py``). Reads never return a cookie value,
only counts and names, so the same rule that governs the API-key card applies
here: a secret that leaves the server is a secret that leaked.

PUT is a bounded write like every other control-plane mutation: JSON body, the
CSRF guard already in front of it, and an audit line per save.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server import credential_vault, data_source_credentials
from server.contract import ApiError, json_body, utc_now_iso

router = APIRouter(prefix="/api", tags=["data-sources"])


@router.get("/data-sources")
def get_data_sources():
    """Configured state per provider; cookie values never appear."""
    return {
        "generated_at": utc_now_iso(),
        "providers": data_source_credentials.summaries(),
        "key_path": str(credential_vault.key_path()),
    }


@router.put("/data-sources/{provider}")
async def put_data_source(provider: str, request: Request):
    """Store one provider's cookie export.

    Body: ``{"cookies": <export>, "options": {...}, "actor": "..."}``. ``cookies``
    accepts Cookie-Editor / EditThisCookie exports (a list of ``{name, value}``
    objects), a ``{name: value}`` map, or either wrapped in ``{"cookies": ...}``.
    """
    from server import audit as audit_log

    body = await json_body(request)
    cookies = body.get("cookies", body)
    options = body.get("options")
    actor = body.get("actor")

    try:
        data_source_credentials.save_credentials(
            provider, cookies, options=options, updated_by=actor,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_credentials", str(exc)) from exc

    audit_log.record("data_source_credentials_updated", provider=provider)
    return {
        "generated_at": utc_now_iso(),
        "providers": data_source_credentials.summaries(),
        "key_path": str(credential_vault.key_path()),
    }


@router.delete("/data-sources/{provider}")
def delete_data_source(provider: str):
    """Remove a provider's stored cookies; the vendor falls back to anonymous."""
    from server import audit as audit_log

    try:
        removed = data_source_credentials.delete_credentials(provider)
    except ValueError as exc:
        raise ApiError(404, "unknown_provider", str(exc)) from exc

    audit_log.record("data_source_credentials_removed", provider=provider)
    return {
        "generated_at": utc_now_iso(),
        "removed": removed,
        "providers": data_source_credentials.summaries(),
        "key_path": str(credential_vault.key_path()),
    }
