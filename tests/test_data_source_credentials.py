"""Session cookies for the sentiment vendors: import, encrypt, store, read back.

A cookie export is a live bearer secret, so the contract these tests pin is:
normalize the operator's export into ``{name: value}``, store only ciphertext,
and never return a cookie value from a read.
"""
import json

import pytest

from server import credential_vault, data_source_credentials
from tradingagents.dataflows import credentials


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    """Each test gets its own key and an empty in-process store."""
    monkeypatch.setenv("TRADINGAGENTS_CREDENTIAL_KEY_PATH", str(tmp_path / "credential.key"))
    for provider in credentials.PROVIDERS:
        credentials.clear_provider_credentials(provider)
    yield
    for provider in credentials.PROVIDERS:
        credentials.clear_provider_credentials(provider)


@pytest.mark.unit
class TestNormalizeCookiePayload:
    def test_cookie_editor_export_is_folded_into_name_value(self):
        export = [
            {"name": "auth_token", "value": "abc", "domain": ".x.com", "expirationDate": 1},
            {"name": "ct0", "value": "def", "domain": ".x.com"},
        ]
        assert credentials.normalize_cookie_payload(export) == {
            "auth_token": "abc",
            "ct0": "def",
        }

    def test_plain_map_is_accepted(self):
        assert credentials.normalize_cookie_payload({"reddit_session": "s"}) == {
            "reddit_session": "s"
        }

    def test_wrapped_payload_is_unwrapped(self):
        payload = {"cookies": [{"name": "ct0", "value": "x"}]}
        assert credentials.normalize_cookie_payload(payload) == {"ct0": "x"}

    def test_entries_without_a_name_or_value_are_dropped(self):
        export = [{"name": "", "value": "x"}, {"name": "ok", "value": "y"}, {"name": "no-value"}]
        assert credentials.normalize_cookie_payload(export) == {"ok": "y"}

    def test_an_unusable_payload_is_refused(self):
        with pytest.raises(ValueError):
            credentials.normalize_cookie_payload("not a cookie export")


@pytest.mark.unit
class TestCookieHeader:
    def test_header_joins_every_cookie(self):
        credentials.set_provider_credentials("reddit", [{"name": "a", "value": "1"},
                                                        {"name": "b", "value": "2"}])
        assert credentials.cookie_header("reddit") == "a=1; b=2"

    def test_unconfigured_provider_has_no_header(self):
        assert credentials.cookie_header("x") is None
        assert credentials.is_configured("x") is False


@pytest.mark.unit
class TestVault:
    def test_ciphertext_does_not_contain_the_cookie_value(self):
        token = credential_vault.encrypt_json({"cookies": {"auth_token": "super-secret"}})
        assert "super-secret" not in token
        assert credential_vault.decrypt_json(token)["cookies"]["auth_token"] == "super-secret"

    def test_the_key_is_created_once_and_reused(self, tmp_path):
        first = credential_vault.key_path()
        credential_vault.encrypt_json({"cookies": {"a": "1"}})
        assert first.exists()
        key_before = first.read_bytes()
        credential_vault.encrypt_json({"cookies": {"a": "2"}})
        assert first.read_bytes() == key_before

    def test_a_rotated_key_reports_a_reread_not_a_crash(self, tmp_path, monkeypatch):
        token = credential_vault.encrypt_json({"cookies": {"a": "1"}})
        monkeypatch.setenv(
            "TRADINGAGENTS_CREDENTIAL_KEY_PATH", str(tmp_path / "different.key")
        )
        with pytest.raises(ValueError, match="re-import"):
            credential_vault.decrypt_json(token)


@pytest.mark.unit
class TestStore:
    def test_saving_pushes_the_cookies_into_the_engine(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        summary = data_source_credentials.save_credentials(
            "reddit", [{"name": "reddit_session", "value": "s"}],
        )
        assert summary["provider"] == "reddit"
        assert summary["cookie_count"] == 1
        # The engine must serve the new cookies immediately, without a restart.
        assert credentials.cookie_header("reddit") == "reddit_session=s"

    def test_a_read_never_returns_a_cookie_value(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        data_source_credentials.save_credentials(
            "x", [{"name": "auth_token", "value": "secret-value"}],
            options={"graphql_query_id": "abc123"},
        )
        summary = data_source_credentials.summary_for("x")
        serialized = json.dumps(summary, ensure_ascii=False)
        assert "secret-value" not in serialized
        assert "auth_token" in summary["cookie_names"]
        assert summary["option_keys"] == ["graphql_query_id"]

    def test_a_bearer_token_counts_as_a_configured_secret_option(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        summary = data_source_credentials.save_credentials(
            "x", None, options={"bearer_token": "tok-SECRET-VALUE-123"},
        )
        assert summary["secret_options"] == ["bearer_token"]
        assert "tok-SECRET-VALUE-123" not in json.dumps(summary)

    def test_empty_credentials_are_refused(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        with pytest.raises(ValueError, match="no cookies"):
            data_source_credentials.save_credentials("reddit", [])

    def test_unknown_provider_is_refused(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        with pytest.raises(ValueError, match="unknown provider"):
            data_source_credentials.save_credentials("myspace", [{"name": "a", "value": "b"}])

    def test_an_unsupported_option_is_refused(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        with pytest.raises(ValueError, match="not supported"):
            data_source_credentials.save_credentials(
                "reddit", [{"name": "a", "value": "b"}], options={"bearer_token": "t"},
            )

    def test_summaries_list_every_supported_provider(self, monkeypatch):
        monkeypatch.setattr(data_source_credentials, "db", _FakeDb())
        items = data_source_credentials.summaries()
        assert {item["provider"] for item in items} == set(credentials.PROVIDERS)
        assert all(item["configured"] is False for item in items)


class _FakeDb:
    """In-memory stand-in for server.postgres (same session/read context shape)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def _conn(self):
        rows = self.rows

        class Conn:
            def execute(self, sql, params=()):
                normalized = " ".join(sql.split())
                if normalized.startswith("INSERT INTO data_source_credentials"):
                    provider, secret, updated_at, updated_by = params
                    rows[provider] = {
                        "provider": provider, "secret": secret,
                        "updated_at": updated_at, "updated_by": updated_by,
                    }
                    return _Cursor(1)
                if normalized.startswith("SELECT provider, secret, updated_at, updated_by"):
                    if "WHERE provider" in normalized:
                        row = rows.get(params[0])
                        return _Cursor(0, [row] if row else [])
                    return _Cursor(0, list(rows.values()))
                if normalized.startswith("SELECT secret FROM data_source_credentials"):
                    row = rows.get(params[0])
                    return _Cursor(0, [{"secret": row["secret"]}] if row else [])
                if normalized.startswith("DELETE FROM data_source_credentials"):
                    removed = rows.pop(params[0], None)
                    return _Cursor(1 if removed else 0)
                raise AssertionError(f"unexpected SQL: {normalized[:80]}")

        return Conn()

    def session(self):
        return _Ctx(self._conn())

    def read(self):
        return _Ctx(self._conn())


class _Cursor:
    def __init__(self, rowcount, rows=()):
        self.rowcount = rowcount
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False
