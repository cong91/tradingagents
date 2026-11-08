"""Offline checks for read-only Binance USD-M Futures account identity."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from uuid import uuid4

import ccxt
import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from tradingagents.execution import futures_identity
from tradingagents.execution.futures_identity import (
    IdentityComparison,
    compare_futures_account_identity,
    read_futures_account_identity,
)

pytestmark = pytest.mark.unit

_ALIAS = "A1B2C3D4"
_API_KEY = "test-api-key-not-for-logging"
_API_SECRET = "test-secret-not-for-logging"
_PROFILE_URLS = {
    "demo": ("https://demo-api.binance.com", "demo-fapi.binance.com"),
    "production": ("https://api.binance.com", "fapi.binance.com"),
}


@pytest.fixture(autouse=True)
def _zero_identity_read_interval(monkeypatch):
    monkeypatch.setattr(futures_identity, "_IDENTITY_READ_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(futures_identity, "_LAST_IDENTITY_READ", 0.0)


class _OfflineBridge:
    """Bridge test double exposing a real configured ccxt Binance client."""

    exchange_id = "binance"

    def __init__(self, client, profile):
        base_url, _ = _PROFILE_URLS[profile]
        self.config = {"exec_base_url": base_url}
        self._client = client

    def client(self):
        return self._client


def _client(profile, response, *, authenticated=True):
    client = ccxt.binance({
        "apiKey": _API_KEY if authenticated else "",
        "secret": _API_SECRET if authenticated else "",
        "enableRateLimit": False,
    })
    if profile == "demo":
        client.enable_demo_trading(True)
    calls = []
    response_box = [response]

    def fake_fetch(url, method="GET", headers=None, body=None):
        calls.append((url, method, headers, body))
        return response_box[0]

    client.fetch = fake_fetch
    return client, calls, response_box


def _valid_response(alias=_ALIAS):
    return [
        {"accountAlias": alias, "asset": "USDT", "balance": "100.0"},
        {"accountAlias": alias, "asset": "USDC", "balance": "25.0"},
    ]


def _expected_digest(profile, alias):
    payload = "\0".join((
        "tradingagents:futures-account-identity:v1",
        "binance",
        profile,
        "usds_futures",
        alias,
    )).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_reads_signed_v3_balance_from_configured_authenticated_client_without_leaks():
    client, calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")

    identity = read_futures_account_identity(bridge)

    assert identity is not None
    assert identity.exchange == "binance"
    assert identity.profile == "demo"
    assert identity.account_type == "usds_futures"
    assert identity.identity_hash == _expected_digest("demo", _ALIAS)
    assert len(calls) == 1
    url, method, headers, body = calls[0]
    assert url.startswith("https://demo-fapi.binance.com/fapi/v3/balance?")
    assert "signature=" in url
    assert "timestamp=" in url
    assert method == "GET"
    assert headers["X-MBX-APIKEY"] == _API_KEY
    assert body is None
    rendered = repr(identity)
    assert _ALIAS not in rendered
    assert _API_KEY not in rendered
    assert _API_SECRET not in rendered


@pytest.mark.parametrize("profile", ("demo", "production"))
def test_signed_v3_route_matches_selected_profile(profile):
    client, calls, _ = _client(profile, _valid_response())
    bridge = _OfflineBridge(client, profile)

    identity = read_futures_account_identity(bridge)

    assert identity is not None
    assert identity.profile == profile
    assert len(calls) == 1
    host = _PROFILE_URLS[profile][1]
    assert calls[0][0].startswith(f"https://{host}/fapi/v3/balance?")


@pytest.mark.parametrize("response", (
    [],
    {},
    [{"asset": "USDT"}],
    [{"accountAlias": _ALIAS}, {"asset": "USDC"}],
    [{"accountAlias": _ALIAS}, {"accountAlias": "OTHER123"}],
    [None],
))
def test_empty_malformed_missing_or_ambiguous_balance_is_unknown(response):
    client, _, _ = _client("production", response)

    assert read_futures_account_identity(_OfflineBridge(client, "production")) is None


@pytest.mark.parametrize("alias", ("", " ", " padded ", "bad\nalias", "x" * 257, 123, None))
def test_invalid_alias_is_unknown(alias):
    client, _, _ = _client("production", [{"accountAlias": alias}])

    assert read_futures_account_identity(_OfflineBridge(client, "production")) is None


def test_same_account_alias_across_asset_rows_is_consistent_evidence():
    client, _, _ = _client("production", _valid_response())

    identity = read_futures_account_identity(_OfflineBridge(client, "production"))

    assert identity is not None
    assert identity.identity_hash == _expected_digest("production", _ALIAS)


def test_same_alias_is_stable_and_alias_change_is_mismatch():
    first_client, _, _ = _client("production", _valid_response())
    second_client, _, _ = _client("production", _valid_response())
    changed_client, _, _ = _client("production", _valid_response("Z9Y8X7W6"))
    expected = read_futures_account_identity(_OfflineBridge(first_client, "production"))
    assert expected is not None

    assert compare_futures_account_identity(
        _OfflineBridge(second_client, "production"), expected
    ) is IdentityComparison.MATCH
    assert compare_futures_account_identity(
        _OfflineBridge(changed_client, "production"), expected
    ) is IdentityComparison.MISMATCH


def test_same_alias_on_demo_and_production_is_a_mismatch():
    demo_client, _, _ = _client("demo", _valid_response())
    production_client, _, _ = _client("production", _valid_response())
    expected = read_futures_account_identity(_OfflineBridge(demo_client, "demo"))
    assert expected is not None

    assert compare_futures_account_identity(
        _OfflineBridge(production_client, "production"), expected
    ) is IdentityComparison.MISMATCH


def test_mismatched_client_route_fails_closed_without_request():
    client, calls, _ = _client("demo", _valid_response())
    client.urls["api"]["fapiPrivateV3"] = "https://fapi.binance.com/fapi/v3"

    assert read_futures_account_identity(_OfflineBridge(client, "demo")) is None
    assert calls == []


@pytest.mark.parametrize("route", [
    "https://demo-fapi.binance.com:444/fapi/v3",
    "https://demo-fapi.binance.com:not-a-port/fapi/v3",
])
def test_invalid_private_port_is_rejected_before_request(route):
    client, calls, _ = _client("demo", _valid_response())
    client.urls["api"]["fapiPrivateV3"] = route

    assert read_futures_account_identity(_OfflineBridge(client, "demo")) is None
    assert calls == []


@pytest.mark.parametrize("base_url", [
    "https://demo-api.binance.com:444",
    "https://demo-api.binance.com:not-a-port",
])
def test_invalid_configured_port_is_rejected_before_request(base_url):
    client, calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    bridge.config["exec_base_url"] = base_url

    assert read_futures_account_identity(bridge) is None
    assert calls == []


@pytest.mark.parametrize("unsafe", ["tls", "verbose", "debug", "auth", "cookies"])
def test_unsafe_identity_transport_fails_without_request(monkeypatch, unsafe):
    client, calls, _ = _client("demo", _valid_response())
    if unsafe == "tls":
        client.verify = False
    elif unsafe == "verbose":
        client.verbose = True
    elif unsafe == "debug":
        monkeypatch.setattr(client.logger, "isEnabledFor", lambda level: level == logging.DEBUG)
    elif unsafe == "auth":
        client.session.auth = ("user", "password")
    else:
        client.session.cookies.set("name", "value")

    assert read_futures_account_identity(_OfflineBridge(client, "demo")) is None
    assert calls == []


def test_redirect_is_not_followed_with_api_key(monkeypatch):
    client, _, _ = _client("demo", _valid_response())
    del client.fetch
    requests_sent = []

    class RedirectAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            requests_sent.append((request.url, request.headers.get("X-MBX-APIKEY")))
            response = requests.Response()
            response.status_code = 302 if len(requests_sent) == 1 else 200
            response.headers = CaseInsensitiveDict({"Location": "https://other.example/collect"})
            response._content = b"[]"
            response.url = request.url
            response.request = request
            return response

    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: RedirectAdapter())
    original_session = client.session

    assert read_futures_account_identity(_OfflineBridge(client, "demo")) is None
    assert len(requests_sent) == 1
    assert requests_sent[0][1] == _API_KEY
    assert client.session is original_session


def test_transient_client_route_change_cannot_redirect_signed_request(monkeypatch):
    client, calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    original_reader = futures_identity._identity_reader
    original_fetch = client.fetch
    original_route = client.urls["api"]["fapiPrivateV3"]

    def reader_during_route_change(actual_client):
        reader = original_reader(actual_client)
        actual_client.urls["api"]["fapiPrivateV3"] = "https://other.example/fapi/v3"

        def restore_after_request(*args, **kwargs):
            try:
                return original_fetch(*args, **kwargs)
            finally:
                actual_client.urls["api"]["fapiPrivateV3"] = original_route

        reader.fetch = restore_after_request
        return reader

    monkeypatch.setattr(futures_identity, "_identity_reader", reader_during_route_change)

    read_futures_account_identity(bridge)

    assert len(calls) <= 1
    assert not calls or calls[0][0].startswith("https://demo-fapi.binance.com/fapi/v3/balance?")


def test_route_change_during_client_copy_cannot_redirect_request(monkeypatch):
    client, calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    original_copy = futures_identity.copy.copy
    original_fetch = client.fetch
    original_route = client.urls["api"]["fapiPrivateV3"]

    def copy_during_route_change(actual_client):
        reader = original_copy(actual_client)
        actual_client.urls["api"]["fapiPrivateV3"] = "https://other.example/fapi/v3"

        def restore_after_request(*args, **kwargs):
            try:
                return original_fetch(*args, **kwargs)
            finally:
                actual_client.urls["api"]["fapiPrivateV3"] = original_route

        reader.fetch = restore_after_request
        return reader

    monkeypatch.setattr(futures_identity.copy, "copy", copy_during_route_change)

    try:
        assert read_futures_account_identity(bridge) is None
        assert calls == []
    finally:
        client.urls["api"]["fapiPrivateV3"] = original_route


def test_concurrent_identity_reads_are_serialized():
    client, _calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    first_in_request = threading.Event()
    second_in_request = threading.Event()
    release_first = threading.Event()
    requests = []
    results = []

    def fetch(*args, **kwargs):
        requests.append(1)
        if len(requests) == 1:
            first_in_request.set()
            release_first.wait(timeout=5)
        else:
            second_in_request.set()
        return _valid_response()

    client.fetch = fetch
    workers = [
        threading.Thread(target=lambda: results.append(read_futures_account_identity(bridge)))
        for _ in range(2)
    ]
    workers[0].start()
    try:
        assert first_in_request.wait(timeout=5)
        workers[1].start()
        assert not second_in_request.wait(timeout=0.3)
    finally:
        release_first.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2 and all(result is not None for result in results)
    assert len(requests) == 2


def test_identity_reads_share_original_client_rate_limit_state():
    client, calls, _ = _client("demo", _valid_response())
    client.enableRateLimit = True
    client.rateLimit = 0
    client.lastRestRequestTimestamp = 1
    bridge = _OfflineBridge(client, "demo")

    assert read_futures_account_identity(bridge) is not None
    assert client.lastRestRequestTimestamp == 1
    assert read_futures_account_identity(bridge) is not None
    assert client.lastRestRequestTimestamp == 1
    assert len(calls) == 2


def test_identity_reads_apply_a_conservative_minimum_interval(monkeypatch):
    client, calls, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    clock = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(futures_identity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(futures_identity.time, "sleep", sleep)
    monkeypatch.setattr(futures_identity, "_IDENTITY_READ_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(futures_identity, "_LAST_IDENTITY_READ", 0.0)

    assert read_futures_account_identity(bridge) is not None
    assert read_futures_account_identity(bridge) is not None

    assert sleeps == [pytest.approx(1.0)]
    assert len(calls) == 2


def test_client_changed_after_identity_request_is_unknown():
    first_client, _, _ = _client("demo", _valid_response())
    second_client, _, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(first_client, "demo")
    original_fetch = first_client.fetch

    def replace_client(*args, **kwargs):
        result = original_fetch(*args, **kwargs)
        bridge._client = second_client
        return result

    first_client.fetch = replace_client

    assert read_futures_account_identity(bridge) is None


@pytest.mark.parametrize("change", ["api_key", "secret", "route", "profile"])
def test_identity_context_changed_after_request_is_unknown(change):
    client, _, _ = _client("demo", _valid_response())
    bridge = _OfflineBridge(client, "demo")
    original_fetch = client.fetch

    def change_context(*args, **kwargs):
        result = original_fetch(*args, **kwargs)
        if change == "api_key":
            client.apiKey = "different-test-key"
        elif change == "secret":
            client.secret = "different-test-secret"
        elif change == "route":
            client.urls["api"]["fapiPrivateV3"] = "https://fapi.binance.com/fapi/v3"
        else:
            bridge.config["exec_base_url"] = "https://api.binance.com"
        return result

    client.fetch = change_context

    assert read_futures_account_identity(bridge) is None


def test_missing_credentials_fails_closed_without_request():
    client, calls, _ = _client("production", _valid_response(), authenticated=False)

    assert read_futures_account_identity(_OfflineBridge(client, "production")) is None
    assert calls == []


def test_non_binance_client_fails_closed_without_request():
    class _WrongBridge(_OfflineBridge):
        exchange_id = "other"

    client, calls, _ = _client("production", _valid_response())

    assert read_futures_account_identity(_WrongBridge(client, "production")) is None
    assert calls == []


def test_comparison_with_unknown_current_evidence_fails_closed():
    client, _, response_box = _client("production", _valid_response())
    bridge = _OfflineBridge(client, "production")
    expected = read_futures_account_identity(bridge)
    assert expected is not None
    response_box[0] = [{"asset": "USDT"}]

    assert compare_futures_account_identity(bridge, expected) is IdentityComparison.UNKNOWN


def test_migration_is_additive_nullable_and_not_backfilled():
    root = Path(__file__).parents[1]
    migration_path = root / "server" / "migrations" / "0005_futures_account_identity.sql"
    sql = migration_path.read_text(encoding="utf-8").lower()

    assert "alter table position_states" in sql
    assert "alter table bracket_recovery_tasks" in sql
    assert sql.count("account_identity_hash text") == 2
    assert "where account_identity_hash is not null" in sql
    assert "update position_states" not in sql
    assert "update bracket_recovery_tasks" not in sql
    from server import postgres

    assert [migration.version for migration in postgres._load_migrations()] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.integration
def test_fresh_postgres_upgrade_preserves_unbound_rows(monkeypatch):
    dsn = os.environ.get("TRADINGAGENTS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TRADINGAGENTS_TEST_DATABASE_URL is not set")

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    database = conninfo_to_dict(dsn).get("dbname", "")
    if not (
        database == "test"
        or database.startswith("test_")
        or database.endswith(("_test", "_tests"))
    ):
        raise RuntimeError("TRADINGAGENTS_TEST_DATABASE_URL must use a dedicated test database")

    from server import postgres

    schema = f"test_identity_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    scoped_dsn = make_conninfo(dsn, options=f"-csearch_path={schema}")
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", scoped_dsn)
    try:
        migrations = postgres._load_migrations()
        with psycopg.connect(scoped_dsn) as conn:
            conn.execute(
                "CREATE TABLE schema_migrations (version integer PRIMARY KEY, "
                "name text NOT NULL, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for migration in migrations[:4]:
                conn.execute(migration.sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            conn.execute(
                "INSERT INTO position_states(position_key, revision, state, updated_at) "
                "VALUES ('legacy-position', 1, '{}'::jsonb, 'now')"
            )
            conn.execute(
                "INSERT INTO bracket_recovery_tasks(task_id, symbol, entry_order_id, close_side, "
                "quantity, legs, updated_at) VALUES ('legacy-task', 'BTCUSDT', '123', 'sell', "
                "1, '[]'::jsonb, 'now')"
            )

        assert postgres.migrate() == ["0005_futures_account_identity.sql"]
        with psycopg.connect(scoped_dsn) as conn:
            position = conn.execute(
                "SELECT account_identity_exchange, account_identity_profile, "
                "account_identity_type, account_identity_hash FROM position_states "
                "WHERE position_key = 'legacy-position'"
            ).fetchone()
            recovery = conn.execute(
                "SELECT account_identity_exchange, account_identity_profile, "
                "account_identity_type, account_identity_hash FROM bracket_recovery_tasks "
                "WHERE task_id = 'legacy-task'"
            ).fetchone()
            assert position == (None, None, None, None)
            assert recovery == (None, None, None, None)
            assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 5
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
