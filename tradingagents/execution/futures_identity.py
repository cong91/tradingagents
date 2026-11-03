"""Read-only Binance USD-M Futures account identity evidence.

A signed balance response can identify an account only when every returned
balance row has the same valid ``accountAlias``. Binance documents the field as
an account alias, but does not establish its uniqueness namespace, Demo support,
or stability across credential rotation. The digest is pseudonymous storage,
not a secret or protection against offline guessing of a low-entropy alias.
This module does not bind legacy records or perform persistence/mutations.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import ccxt
import requests

from tradingagents.execution.exchange_client import _binance_profile

_ALIAS_MAX_LENGTH = 256
_ALIAS_INVALID = re.compile(r"[\x00-\x1f\x7f]")
_IDENTITY_DOMAIN = "tradingagents:futures-account-identity:v1"
_ACCOUNT_TYPE = "usds_futures"
_IDENTITY_READ_LOCK = threading.Lock()
_IDENTITY_READ_INTERVAL_SECONDS = 1.0
_LAST_IDENTITY_READ = 0.0


class _NoRedirectSession(requests.Session):
    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise requests.HTTPError("Binance identity redirect refused")
        return response


@dataclass(frozen=True, slots=True)
class FuturesAccountIdentity:
    """Credential-free digest and namespace for one observed Futures account."""

    exchange: str
    profile: str
    account_type: str
    identity_hash: str


class IdentityComparison(Enum):
    """Comparison result for persisted binding against current client evidence."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


def _valid_alias(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _ALIAS_MAX_LENGTH
        and value == value.strip()
        and value.isprintable()
        and _ALIAS_INVALID.search(value) is None
    )


def _identity_hash(profile: str, alias: str) -> str:
    payload = "\0".join((
        _IDENTITY_DOMAIN,
        "binance",
        profile,
        _ACCOUNT_TYPE,
        alias,
    )).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _matches_profile_route(client: Any, profile: str) -> bool:
    expected_host = "demo-fapi.binance.com" if profile == "demo" else "fapi.binance.com"
    urls = getattr(client, "urls", None)
    api_urls = urls.get("api") if isinstance(urls, dict) else None
    route_url = api_urls.get("fapiPrivateV3") if isinstance(api_urls, dict) else None
    try:
        parsed_route = urlparse(str(route_url or ""))
        return (
            parsed_route.scheme == "https"
            and parsed_route.hostname == expected_host
            and parsed_route.port in (None, 443)
            and parsed_route.path == "/fapi/v3"
            and parsed_route.username is None
            and parsed_route.password is None
            and not parsed_route.query
            and not parsed_route.fragment
        )
    except ValueError:
        return False


def _configured_profile_and_client(bridge: Any) -> tuple[str, Any] | None:
    if getattr(bridge, "exchange_id", None) != "binance":
        return None
    config = getattr(bridge, "config", None)
    if not isinstance(config, dict):
        return None
    try:
        base_url = config.get("exec_base_url")
        profile = _binance_profile(base_url)
        configured_url = urlparse(str(base_url))
        if configured_url.port not in (None, 443):
            return None
        client = bridge.client()
    except (AttributeError, TypeError, ValueError):
        return None
    except Exception:
        return None

    if type(client) is not ccxt.binance:
        return None
    if not getattr(client, "apiKey", None) or not getattr(client, "secret", None):
        return None
    if getattr(client, "verify", None) is not True or getattr(client, "verbose", False) is not False:
        return None
    logger = getattr(client, "logger", None)
    if logger is not None and (
        not callable(getattr(logger, "isEnabledFor", None)) or logger.isEnabledFor(logging.DEBUG)
    ):
        return None

    if not _matches_profile_route(client, profile):
        return None

    api = getattr(client, "api", None)
    route = api.get("fapiPrivateV3") if isinstance(api, dict) else None
    get_routes = route.get("get") if isinstance(route, dict) else None
    if not isinstance(get_routes, dict) or "balance" not in get_routes:
        return None
    if not callable(getattr(client, "fapiPrivateV3GetBalance", None)):
        return None
    return profile, client


def _identity_reader(client: Any) -> Any | None:
    session = getattr(client, "session", None)
    if not isinstance(session, requests.Session):
        return None
    if session.auth is not None or session.cookies or session.hooks.get("response"):
        return None
    transport = _NoRedirectSession()
    transport.headers.clear()
    transport.headers.update(session.headers)
    transport.trust_env = session.trust_env
    transport.proxies.update(session.proxies)
    transport.verify = session.verify
    transport.cert = session.cert
    reader = copy.copy(client)
    reader.session = transport
    reader.options = copy.deepcopy(client.options)
    reader.urls = copy.deepcopy(client.urls)
    return reader


def read_futures_account_identity(bridge: Any) -> FuturesAccountIdentity | None:
    """Read identity from the configured client's signed Futures V3 balance.

    Returns ``None`` on any missing, malformed, ambiguous, unauthenticated, or
    profile-mismatched evidence. The alias is neither returned nor logged.
    """
    global _LAST_IDENTITY_READ
    selected = _configured_profile_and_client(bridge)
    if selected is None:
        return None
    profile, client = selected
    credentials = (client.apiKey, client.secret)
    reader = _identity_reader(client)
    if reader is None or not _matches_profile_route(reader, profile):
        return None

    try:
        with _IDENTITY_READ_LOCK:
            elapsed = time.monotonic() - _LAST_IDENTITY_READ
            if elapsed < _IDENTITY_READ_INTERVAL_SECONDS:
                time.sleep(_IDENTITY_READ_INTERVAL_SECONDS - elapsed)
            _LAST_IDENTITY_READ = time.monotonic()
            rows = reader.fapiPrivateV3GetBalance()
        current = _configured_profile_and_client(bridge)
    except Exception:
        return None
    if (
        current is None or current[0] != profile or current[1] is not client
        or (client.apiKey, client.secret) != credentials
    ):
        return None
    if not isinstance(rows, list) or not rows:
        return None

    alias: str | None = None
    for row in rows:
        if not isinstance(row, dict) or "accountAlias" not in row:
            return None
        value = row["accountAlias"]
        if not _valid_alias(value):
            return None
        if alias is None:
            alias = value
        elif value != alias:
            return None

    if alias is None:
        return None
    return FuturesAccountIdentity(
        exchange="binance",
        profile=profile,
        account_type=_ACCOUNT_TYPE,
        identity_hash=_identity_hash(profile, alias),
    )


def compare_futures_account_identity(
    bridge: Any,
    expected: FuturesAccountIdentity,
) -> IdentityComparison:
    """Compare current signed client evidence with a previously stored binding.

    Callers must require ``MATCH`` before any account-scoped mutation. Unknown
    evidence is never treated as a match, and no legacy binding is inferred.
    """
    if not isinstance(expected, FuturesAccountIdentity):
        return IdentityComparison.UNKNOWN
    observed = read_futures_account_identity(bridge)
    if observed is None:
        return IdentityComparison.UNKNOWN
    return (
        IdentityComparison.MATCH
        if observed == expected
        else IdentityComparison.MISMATCH
    )
