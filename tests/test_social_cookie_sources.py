"""Authenticated vendor paths: Reddit via cookie, X via bearer token or cookie.

Reddit's JSON search answers 403 to anonymous clients (verified live), so a
configured session cookie is what makes score/comment counts available at all.
X rejects anonymous search outright (403 GraphQL / 401 API v2), so its vendor
must say "not configured" rather than imply nobody is talking about the ticker.
"""
import json
from unittest.mock import patch

import pytest

from tradingagents.dataflows import credentials
from tradingagents.dataflows.vendors import reddit, x_social


@pytest.fixture(autouse=True)
def no_stored_credentials():
    for provider in credentials.PROVIDERS:
        credentials.clear_provider_credentials(provider)
    yield
    for provider in credentials.PROVIDERS:
        credentials.clear_provider_credentials(provider)


def _json_response(payload):
    body = json.dumps(payload).encode()

    class Response:
        def read(self, _n=None):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Response()


@pytest.mark.unit
class TestRedditAuthenticatedPath:
    def test_no_cookie_keeps_the_rss_path(self):
        seen = {}

        def fake_rss(ticker, sub, limit, timeout, **kwargs):
            seen["path"] = "rss"
            return []

        with patch.object(reddit, "_fetch_subreddit_rss", side_effect=fake_rss):
            reddit.fetch_reddit_posts("BTC-USD", subreddits=("stocks",))
        assert seen["path"] == "rss"

    def test_a_configured_cookie_uses_the_json_endpoint(self):
        credentials.set_provider_credentials("reddit", [{"name": "reddit_session", "value": "s"}])
        seen = {}

        def fake_json(ticker, sub, limit, timeout):
            seen["path"] = "json"
            seen["ticker"] = ticker
            return []

        with patch.object(reddit, "_fetch_subreddit_json", side_effect=fake_json), \
                patch.object(reddit, "_fetch_subreddit_rss") as rss:
            reddit.fetch_reddit_posts("BTC-USD", subreddits=("stocks",))
        assert seen["path"] == "json"
        assert seen["ticker"] == "BTC"  # the pair searches its base
        rss.assert_not_called()

    def test_json_results_carry_engagement_counts(self):
        payload = {"data": {"children": [
            {"data": {"title": "BTC breakout", "created_utc": 1793000000,
                      "selftext": "volume is up", "subreddit": "stocks",
                      "score": 42, "num_comments": 7}},
        ]}}
        with patch.object(reddit, "urlopen", return_value=_json_response(payload)):
            posts = reddit._fetch_subreddit_json("BTC", "stocks", 5, 1.0)
        assert posts[0]["score"] == 42
        assert posts[0]["num_comments"] == 7

    def test_the_cookie_header_is_sent(self):
        credentials.set_provider_credentials("reddit", [{"name": "reddit_session", "value": "abc"}])
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["cookie"] = req.headers.get("Cookie")
            return _json_response({"data": {"children": []}})

        with patch.object(reddit, "urlopen", side_effect=fake_urlopen):
            reddit._fetch_subreddit_json("BTC", "stocks", 5, 1.0)
        assert seen["cookie"] == "reddit_session=abc"

    def test_a_failed_json_fetch_reports_unavailable_not_silence(self):
        credentials.set_provider_credentials("reddit", [{"name": "reddit_session", "value": "s"}])
        with patch.object(reddit, "_fetch_subreddit_json", return_value=None):
            out = reddit.fetch_reddit_posts("BTC-USD", subreddits=("stocks",))
        assert "unavailable" in out.lower()
        assert "not an absence" in out


@pytest.mark.unit
class TestXSearchTerm:
    def test_crypto_pair_searches_its_cashtag(self):
        assert x_social._search_term("BTC-USD") == "$BTC"
        assert x_social._search_term("BNB-USD") == "$BNB"

    def test_equity_passes_through(self):
        assert x_social._search_term("NVDA") == "NVDA"


@pytest.mark.unit
class TestXNotConfigured:
    def test_unconfigured_x_says_so_instead_of_implying_silence(self):
        out = x_social.fetch_x_posts("BTC-USD")
        assert "no credentials configured" in out
        assert "not an absence" in out


@pytest.mark.unit
class TestXBearerPath:
    def test_bearer_token_uses_api_v2(self, monkeypatch):
        credentials.set_provider_credentials("x", options={"bearer_token": "tok"})
        payload = {"data": [{"text": "BTC moon", "created_at": "2026-10-25T10:00:00Z",
                             "public_metrics": {"like_count": 5, "retweet_count": 2}}]}
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            return _json_response(payload)

        with patch.object(x_social, "urlopen", side_effect=fake_urlopen):
            out = x_social.fetch_x_posts("BTC-USD", limit=10)
        assert "search/recent" in seen["url"]
        assert seen["auth"] == "Bearer tok"
        assert "BTC moon" in out and "likes 5" in out


@pytest.mark.unit
class TestXGraphqlPath:
    def test_missing_query_id_is_a_configuration_error(self):
        credentials.set_provider_credentials("x", [{"name": "auth_token", "value": "a"},
                                                    {"name": "ct0", "value": "c"}])
        out = x_social.fetch_x_posts("BTC-USD")
        assert "graphql_query_id is missing" in out

    def test_missing_ct0_is_reported(self):
        credentials.set_provider_credentials(
            "x", [{"name": "auth_token", "value": "a"}],
            options={"graphql_query_id": "q1"},
        )
        out = x_social.fetch_x_posts("BTC-USD")
        assert "missing ct0" in out

    def test_graphql_timeline_is_parsed(self):
        credentials.set_provider_credentials(
            "x", [{"name": "auth_token", "value": "a"}, {"name": "ct0", "value": "c"}],
            options={"graphql_query_id": "q1"},
        )
        result = {"legacy": {
            "full_text": "BTC ripping", "created_at": "2026-10-25T10:00:00Z",
            "favorite_count": 9, "retweet_count": 1,
        }}
        entry = {"content": {"itemContent": {"tweet_results": {"result": result}}}}
        payload = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
            "instructions": [{"entries": [entry]}],
        }}}}}
        with patch.object(x_social, "urlopen", return_value=_json_response(payload)):
            out = x_social.fetch_x_posts("BTC-USD", limit=10)
        assert "BTC ripping" in out and "likes 9" in out
