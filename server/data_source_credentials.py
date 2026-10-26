"""Persistence for third-party session cookies (Reddit, X).

The control panel is the only writer: it validates and encrypts the operator's
cookie export (``credential_vault``), stores the ciphertext, and pushes the
decrypted set into the engine's in-process store
(``tradingagents.dataflows.credentials``) so the vendors can read it. Reads
return names and metadata only — never a cookie value.
"""

from __future__ import annotations

import logging
from typing import Any

from server import credential_vault, postgres as db
from server.contract import utc_now_iso
from tradingagents.dataflows.credentials import (
    PROVIDERS,
    normalize_cookie_payload,
    set_provider_credentials,
)

logger = logging.getLogger(__name__)

# Option keys a provider accepts beside its cookies. X needs a search mode;
# Reddit only needs the cookies themselves.
_OPTION_KEYS = {
    "reddit": (),
    "x": ("bearer_token", "graphql_query_id"),
}

# Option names that carry a secret and must never be echoed back.
_SECRET_OPTION_KEYS = {"bearer_token"}


def _check_provider(provider: str) -> str:
    key = str(provider).strip().lower()
    if key not in PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    return key


def _clean_options(provider: str, options: dict | None) -> dict[str, Any]:
    """Keep only the allowlisted option keys, dropping blanks."""
    if not options:
        return {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    allowed = _OPTION_KEYS.get(provider, ())
    cleaned: dict[str, Any] = {}
    for key, value in options.items():
        if key not in allowed:
            raise ValueError(f"option {key!r} is not supported for {provider}")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        cleaned[key] = value.strip() if isinstance(value, str) else value
    return cleaned


def save_credentials(
    provider: str,
    cookies: Any,
    *,
    options: dict | None = None,
    updated_by: str | None = None,
) -> dict:
    """Validate, encrypt and store one provider's credentials; return its summary."""
    key = _check_provider(provider)
    normalized = normalize_cookie_payload(cookies) if cookies else {}
    cleaned_options = _clean_options(key, options)
    if not normalized and not cleaned_options:
        raise ValueError("no cookies or options were supplied")

    payload = {"cookies": normalized, "options": cleaned_options}
    secret = credential_vault.encrypt_json(payload)
    with db.session() as conn:
        conn.execute(
            """INSERT INTO data_source_credentials (provider, secret, updated_at, updated_by)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider) DO UPDATE SET
                   secret = excluded.secret,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            (key, secret, utc_now_iso(), updated_by),
        )
    # The engine must serve the new cookies on the next analysis without a
    # restart: the operator expects "save" to take effect immediately.
    set_provider_credentials(key, normalized, cleaned_options)
    return summary_for(key) or {"provider": key, "configured": True}


def load_credentials(provider: str) -> dict | None:
    """Decrypted ``{"cookies": ..., "options": ...}`` for a provider, or None."""
    key = _check_provider(provider)
    with db.read() as conn:
        row = conn.execute(
            "SELECT secret FROM data_source_credentials WHERE provider = %s", (key,)
        ).fetchone()
    if row is None:
        return None
    return credential_vault.decrypt_json(row["secret"])


def delete_credentials(provider: str) -> bool:
    from tradingagents.dataflows.credentials import clear_provider_credentials

    key = _check_provider(provider)
    with db.session() as conn:
        cursor = conn.execute(
            "DELETE FROM data_source_credentials WHERE provider = %s", (key,)
        )
    clear_provider_credentials(key)
    return cursor.rowcount == 1


def _summarize_row(provider: str, secret: str, updated_at: str, updated_by: str | None) -> dict:
    """Metadata only: cookie names are safe, cookie values never leave the process."""
    try:
        payload = credential_vault.decrypt_json(secret)
        decryptable = True
    except ValueError:
        payload, decryptable = {}, False
    cookies = payload.get("cookies") or {}
    options = payload.get("options") or {}
    return {
        "provider": provider,
        "configured": True,
        "decryptable": decryptable,
        "cookie_count": len(cookies),
        "cookie_names": sorted(cookies),
        "option_keys": sorted(options),
        "secret_options": sorted(key for key in options if key in _SECRET_OPTION_KEYS),
        "updated_at": updated_at,
        "updated_by": updated_by,
        "supported_options": list(_OPTION_KEYS.get(provider, ())),
    }


def summary_for(provider: str) -> dict | None:
    key = _check_provider(provider)
    with db.read() as conn:
        row = conn.execute(
            "SELECT provider, secret, updated_at, updated_by "
            "FROM data_source_credentials WHERE provider = %s",
            (key,),
        ).fetchone()
    return None if row is None else _summarize_row(**row)


def _unconfigured(provider: str) -> dict:
    return {
        "provider": provider,
        "configured": False,
        "decryptable": False,
        "cookie_count": 0,
        "cookie_names": [],
        "option_keys": [],
        "secret_options": [],
        "updated_at": None,
        "updated_by": None,
        "supported_options": list(_OPTION_KEYS.get(provider, ())),
    }


def summaries() -> list[dict]:
    """Every supported provider, configured or not, so the UI can offer all of them.

    The table is created by migration 0004 at startup, so a missing table here
    means the screen is being served before the schema is in place. That reads
    as "nothing configured" rather than a 500: the Cấu hình screen must still
    render, and saving would surface the real error.
    """
    try:
        with db.read() as conn:
            rows = {
                row["provider"]: row
                for row in conn.execute(
                    "SELECT provider, secret, updated_at, updated_by FROM data_source_credentials"
                ).fetchall()
            }
    except Exception as exc:  # table absent or database unreachable
        logger.warning("data-source credential summaries unavailable: %s", exc)
        return [_unconfigured(provider) for provider in PROVIDERS]

    return [
        _summarize_row(**rows[provider]) if provider in rows else _unconfigured(provider)
        for provider in PROVIDERS
    ]


def push_to_engine() -> list[str]:
    """Install every decryptable credential set into the engine; return failures.

    Called at server startup: without it the engine would read cookies only from
    a save made in the same process lifetime. A database that cannot be read
    here must not keep the server down — the migrations step already fails
    startup when PostgreSQL is truly unavailable — so this degrades to "no
    cookies installed" and each vendor falls back to its anonymous path.
    """
    failures: list[str] = []
    try:
        with db.read() as conn:
            rows = conn.execute(
                "SELECT provider, secret FROM data_source_credentials"
            ).fetchall()
    except Exception as exc:  # table missing or database unreachable at startup
        logger.warning("data-source credentials not loaded: %s", exc)
        return failures
    for row in rows:
        try:
            payload = credential_vault.decrypt_json(row["secret"])
        except ValueError:
            failures.append(row["provider"])
            continue
        try:
            set_provider_credentials(
                row["provider"], payload.get("cookies"), payload.get("options"),
            )
        except ValueError:
            failures.append(row["provider"])
    return failures
