"""X search through the stored session, without operator-supplied API identifiers."""

import pytest

from tradingagents.dataflows import credentials
from tradingagents.dataflows.vendors import x_social


@pytest.fixture(autouse=True)
def stored_x_session():
    credentials.clear_provider_credentials("x")
    yield
    credentials.clear_provider_credentials("x")


def _cookies():
    credentials.set_provider_credentials("x", {"auth_token": "secret-auth", "ct0": "secret-csrf"})


@pytest.mark.unit
def test_unconfigured_search_is_unavailable():
    result = x_social.fetch_x_posts("BTC-USD")
    assert "no credentials configured" in result
    assert "not an absence" in result


@pytest.mark.unit
@pytest.mark.parametrize("ticker,term", [("BTC-USD", "$BTC"), ("BNB-USD", "$BNB"), ("NVDA", "NVDA")])
def test_search_uses_symbol_cashtag(ticker, term):
    assert x_social._search_term(ticker) == term


@pytest.mark.unit
def test_only_saved_cookies_are_needed_for_twscrape(monkeypatch):
    _cookies()
    seen = {}

    def search(cookies, query, *, limit, timeout):
        seen.update(cookies=cookies, query=query, limit=limit, timeout=timeout)
        return [{"text": "ZEC on-chain discussion", "created_at": "2026-10-26T10:00:00+00:00",
                 "likes": 9, "reposts": 2, "url": "https://x.com/research/status/123"}]

    monkeypatch.setattr(x_social, "search_posts", search, raising=False)
    result = x_social.fetch_x_posts("ZEC-USD", limit=5, timeout=2)
    assert seen["cookies"] == {"auth_token": "secret-auth", "ct0": "secret-csrf"}
    assert seen["query"] == "$ZEC -filter:retweets"
    assert seen["limit"] == 5 and seen["timeout"] == 2
    assert "ZEC on-chain discussion" in result
    assert "likes 9, reposts 2" in result
    assert "https://x.com/research/status/123" in result
    assert "secret-auth" not in result and "secret-csrf" not in result


@pytest.mark.unit
def test_missing_cookie_does_not_start_search(monkeypatch):
    credentials.set_provider_credentials("x", {"auth_token": "secret-auth"})
    monkeypatch.setattr(x_social, "search_posts", lambda *a, **k: pytest.fail("search started"), raising=False)
    result = x_social.fetch_x_posts("BTC-USD")
    assert "missing ct0" in result
    assert "not an absence" in result


@pytest.mark.unit
def test_dates_are_sent_to_search_and_filtered_in_utc(monkeypatch):
    _cookies()
    seen = {}

    def search(cookies, query, **kwargs):
        seen["query"] = query
        return [
            {"text": "before", "created_at": "2026-10-19T23:59:59Z"},
            {"text": "within", "created_at": "2026-10-21T01:00:00+07:00"},
            {"text": "last second", "created_at": "2026-10-21T23:59:59Z"},
            {"text": "future", "created_at": "2026-10-22T00:00:00Z"},
            {"text": "undated", "created_at": None},
        ]

    monkeypatch.setattr(x_social, "search_posts", search, raising=False)
    result = x_social.fetch_x_posts("BTC-USD", start_date="2026-10-20", end_date="2026-10-21")
    assert "since:2026-10-20 until:2026-10-22" in seen["query"]
    assert "within" in result and "last second" in result
    assert "before" not in result and "future" not in result and "undated" not in result


@pytest.mark.unit
def test_transport_failure_never_becomes_no_posts_or_leaks_secrets(monkeypatch, caplog):
    _cookies()

    def search(*args, **kwargs):
        raise RuntimeError("request rejected Cookie=secret-auth; ct0=secret-csrf")

    monkeypatch.setattr(x_social, "search_posts", search, raising=False)
    result = x_social.fetch_x_posts("BTC-USD")
    assert "unavailable" in result and "not an absence" in result
    assert "no X posts" not in result
    assert "secret-auth" not in result + caplog.text
    assert "secret-csrf" not in result + caplog.text


@pytest.mark.unit
def test_completed_empty_search_reports_no_matching_posts(monkeypatch):
    _cookies()
    monkeypatch.setattr(x_social, "search_posts", lambda *a, **k: [], raising=False)
    assert "no X posts found" in x_social.fetch_x_posts("BTC-USD")


@pytest.mark.unit
def test_unobserved_historical_window_remains_a_coverage_gap(monkeypatch):
    _cookies()
    monkeypatch.setattr(x_social, "search_posts", lambda *a, **k: [], raising=False)
    result = x_social.fetch_x_posts("BTC-USD", start_date="2020-01-01", end_date="2020-01-02")
    assert "unavailable" in result and "not an absence" in result


@pytest.mark.unit
@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"timeout": 0}, {"timeout": float("nan")},
                                   {"start_date": "bad", "end_date": "2026-10-26"},
                                   {"start_date": "2026-10-27", "end_date": "2026-10-26"}])
def test_invalid_search_arguments_do_not_fetch(monkeypatch, kwargs):
    _cookies()
    monkeypatch.setattr(x_social, "search_posts", lambda *a, **k: pytest.fail("search started"), raising=False)
    assert "unavailable" in x_social.fetch_x_posts("BTC-USD", **kwargs)
