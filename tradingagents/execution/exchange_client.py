"""Exchange client construction and Binance venue profiles."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

_BINANCE_DEMO_HOSTS = frozenset({"demo-api.binance.com", "demo-fapi.binance.com"})
_BINANCE_PRODUCTION_HOSTS = frozenset({"api.binance.com", "fapi.binance.com"})


def _binance_profile(base_url: str | None) -> str:
    """Validate the configured Binance base URL and return its profile."""
    raw = str(base_url or "").strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.path not in ("", "/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "exec_base_url must be an HTTPS Binance API host without credentials, "
            "query parameters, or a path"
        )
    if host in _BINANCE_DEMO_HOSTS:
        return "demo"
    if host in _BINANCE_PRODUCTION_HOSTS:
        return "production"
    raise ValueError(
        "exec_base_url must be https://demo-api.binance.com or https://api.binance.com"
    )


def venue_key(bridge: Any) -> str:
    """Return a credential-free, stable venue identity for audit/state keys."""
    config = bridge.config
    parsed = urlparse(str(config.get("exec_base_url") or "").strip())
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = host
    if port is not None:
        authority = f"{authority}:{port}"
    normalized_url = f"{parsed.scheme.lower()}://{authority}" if parsed.scheme else authority
    return "|".join((
        str(bridge.exchange_id).lower(),
        normalized_url,
        str(config.get("exec_sandbox") is True),
    ))


def credential_env_names(exchange_id: str, base_url: str | None = None) -> tuple[str, str]:
    """Return credential env vars coupled to the selected venue profile."""
    upper = exchange_id.strip().upper()
    if upper == "BINANCE" and _binance_profile(base_url or "https://api.binance.com") == "demo":
        return "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET"
    return f"{upper}_API_KEY", f"{upper}_SECRET"


def _assert_binance_urls(client: Any, profile: str) -> None:
    expected = _BINANCE_DEMO_HOSTS if profile == "demo" else _BINANCE_PRODUCTION_HOSTS
    api = client.urls.get("api", {}) if isinstance(getattr(client, "urls", None), dict) else {}
    if isinstance(api, str):
        watched = [api]
    elif isinstance(api, dict):
        watched = [
            value for key, value in api.items()
            if key in {"public", "private", "fapiPublic", "fapiPrivate"}
        ]
    else:
        watched = []
    hosts = {urlparse(str(url)).hostname for url in watched}
    if not watched or not hosts.issubset(expected):
        raise RuntimeError(
            f"Binance {profile} profile resolved unexpected API hosts: {sorted(hosts)}"
        )


def _default_exchange_factory(
    exchange_id: str, sandbox: bool = False, *, base_url: str | None = None
) -> Any:
    """Build the configured ccxt client lazily; credentials from env only.

    ``exchange_id`` must name a ``ccxt.Exchange`` subclass (e.g. "binance",
    "okx"); anything else raises before any network call. ccxt is imported
    here, not at module level, so importing tradingagents.execution stays
    cheap and tests can inject fakes. For Binance, the safe mode is its Demo
    Trading API (``demo-api.binance.com``), not Spot Testnet. Other exchanges
    use ccxt's sandbox mode when available.
    """
    import ccxt

    klass = getattr(ccxt, exchange_id, None)
    if not (isinstance(klass, type) and issubclass(klass, ccxt.Exchange)):
        raise ValueError(
            f"exec_exchange_id {exchange_id!r} does not name a ccxt exchange class"
        )
    profile = None
    if exchange_id == "binance":
        profile = _binance_profile(base_url or "https://demo-api.binance.com")
    credential_url = base_url or (
        "https://demo-api.binance.com" if profile == "demo" else "https://api.binance.com"
    ) if exchange_id == "binance" else None
    key_env, secret_env = credential_env_names(exchange_id, credential_url)
    api_key = os.environ.get(key_env, "")
    secret = os.environ.get(secret_env, "")
    if exchange_id == "binance" and (not api_key or not secret):
        raise RuntimeError(f"missing exchange credentials: {key_env} and {secret_env}")
    options: dict = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
    if exchange_id == "binance":
        # Binance rejects a signed request whose timestamp is more than its
        # recvWindow ahead of server time (-1021). A host clock drifting even
        # ~1s breaks every private call (balance, orders), so let ccxt measure
        # the offset once per process and sign with the corrected clock.
        options["options"] = {"adjustForTimeDifference": True}
    client = klass(options)
    if exchange_id == "binance":
        if profile == "demo":
            enable_demo_trading = getattr(client, "enable_demo_trading", None)
            if not callable(enable_demo_trading):
                raise RuntimeError(
                    "ccxt Binance client does not support Demo Trading mode; "
                    "refusing to fall back to Spot Testnet"
                )
            enable_demo_trading(True)
        _assert_binance_urls(client, profile)
    elif sandbox:
        client.set_sandbox_mode(True)
    return client
