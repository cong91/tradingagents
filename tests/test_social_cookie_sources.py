"""Reddit authenticated source: cookie search preserves engagement and failures."""
import json
from unittest.mock import patch

import pytest

from tradingagents.dataflows import credentials
from tradingagents.dataflows.vendors import reddit


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
