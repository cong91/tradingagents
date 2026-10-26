"""In-process store for third-party session cookies (Reddit, X).

The control panel owns persistence — PostgreSQL, encrypted at rest — and pushes
the decrypted credential sets into this module at startup and after every save,
so the vendors read cookies without a database dependency. A CLI run has no
server, so every provider reads as unconfigured and each vendor falls back to
its anonymous path.

Cookie payloads arrive as browser-extension exports, which use several shapes;
``normalize_cookie_payload`` folds them into ``{name: value}`` once, at the
boundary, so no vendor has to know which extension the operator used.
"""

from __future__ import annotations

from typing import Any

# Providers the control panel can store credentials for. Kept small on purpose:
# a provider accepted here but unread by any vendor is a silent no-op.
PROVIDERS = ("reddit", "x")

# provider -> {"cookies": {name: value}, "options": {key: value}}
_credentials: dict[str, dict[str, Any]] = {}


def normalize_cookie_payload(payload: Any) -> dict[str, str]:
    """Fold a browser cookie export into ``{name: value}``.

    Accepted shapes: a list of cookie objects (Cookie-Editor / EditThisCookie
    export ``[{"name": ..., "value": ...}, ...]``), a plain ``{name: value}``
    map, or either of those wrapped in ``{"cookies": ...}``. Domain, expiry and
    path fields are dropped so an export's extra keys never reach a header.
    """
    if isinstance(payload, dict) and "cookies" in payload:
        payload = payload["cookies"]

    items: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        items = [(str(name), str(value)) for name, value in payload.items() if value is not None]
    elif isinstance(payload, (list, tuple)):
        for entry in payload:
            if isinstance(entry, dict):
                name, value = entry.get("name"), entry.get("value")
                if name and value is not None:
                    items.append((str(name), str(value)))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                items.append((str(entry[0]), str(entry[1])))
    else:
        raise ValueError(
            "cookies must be a list of {name, value} objects or a {name: value} map"
        )

    return {name.strip(): value for name, value in items if name.strip()}


def set_provider_credentials(
    provider: str, cookies: Any = None, options: dict | None = None,
) -> None:
    """Install one provider's credentials; an empty set clears that provider."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown credential provider {provider!r}")
    normalized = normalize_cookie_payload(cookies) if cookies else {}
    entry: dict[str, Any] = {}
    if normalized:
        entry["cookies"] = normalized
    if options:
        entry["options"] = {str(key): value for key, value in options.items()}
    if entry:
        _credentials[provider] = entry
    else:
        _credentials.pop(provider, None)


def clear_provider_credentials(provider: str) -> None:
    _credentials.pop(provider, None)


def cookies_for(provider: str) -> dict[str, str]:
    return dict((_credentials.get(provider) or {}).get("cookies") or {})


def options_for(provider: str) -> dict[str, Any]:
    """Non-cookie settings stored beside the cookies (API tokens, query ids)."""
    return dict((_credentials.get(provider) or {}).get("options") or {})


def cookie_header(provider: str) -> str | None:
    """``Cookie:`` header value for ``provider``, or None when unconfigured."""
    cookies = cookies_for(provider)
    if not cookies:
        return None
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def is_configured(provider: str) -> bool:
    return provider in _credentials
