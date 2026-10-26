"""X (Twitter) cashtag/keyword search for the sentiment analyst.

Two authenticated paths, chosen by what the operator stored:

* **``bearer_token`` (official API v2)** — ``/2/tweets/search/recent``. Stable,
  documented, rate-limited by the plan; needs no cookies.
* **Session cookies (``auth_token`` + ``ct0``)** — the internal GraphQL search
  the web client uses. No official quota, but the query id rotates with each X
  deploy, so the operator supplies it as ``graphql_query_id`` and a stale id is
  reported as an explicit configuration error rather than as "no tweets".

Nothing here is anonymous: X rejects unauthenticated search outright (403 on
GraphQL, 401 on the API), which is why the caller must configure credentials
before this source contributes anything.

Like the Reddit vendor, every failure returns a placeholder string — never an
exception — and an unobserved window is never reported as silence.
"""

from __future__ import annotations

import http.client
import json
import logging
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tradingagents.dataflows import credentials
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.symbols import crypto_base, futures_base

logger = logging.getLogger(__name__)

_API_SEARCH = "https://api.x.com/2/tweets/search/recent?{qs}"
_GRAPHQL_SEARCH = "https://x.com/i/api/graphql/{query_id}/SearchTimeline?{qs}"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# Cookies the web client requires together; one without the other is rejected.
_REQUIRED_COOKIES = ("auth_token", "ct0")

_MAX_BYTES = 5 * 1024 * 1024


def _search_term(ticker: str) -> str:
    """The cashtag/keyword to search for a pipeline ticker.

    Crypto reaches this pipeline as a Yahoo pair (``BTC-USD``) and X discussion
    is cashtagged by base (``$BTC``), so a pair searches its base.
    """
    base = crypto_base(ticker) or futures_base(ticker)
    return f"${base}" if base else ticker


def _created_at(tweet: dict) -> datetime | None:
    raw = tweet.get("created_at") or tweet.get("createdAt")
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_capped(resp) -> bytes:
    data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise http.client.HTTPException(
            f"X response exceeded {_MAX_BYTES} bytes; refusing to parse"
        )
    return data


def _search_via_api(term: str, limit: int, timeout: float) -> list[dict] | None:
    """Official API v2 recent search; returns None when the request failed."""
    qs = urlencode({
        "query": f"{term} -is:retweet lang:en",
        "max_results": max(10, min(limit, 100)),
        "tweet.fields": "created_at,public_metrics",
    })
    req = Request(_API_SEARCH.format(qs=qs), headers={
        "User-Agent": _UA,
        "Authorization": f"Bearer {credentials.options_for('x').get('bearer_token')}",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(_read_capped(resp).decode("utf-8", "replace"))
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("X API search failed for %s: %s", term, exc)
        return None

    tweets = (payload.get("data") or []) if isinstance(payload, dict) else []
    return [{
        "text": tweet.get("text") or "",
        "created_at": tweet.get("created_at"),
        "likes": (tweet.get("public_metrics") or {}).get("like_count"),
        "reposts": (tweet.get("public_metrics") or {}).get("retweet_count"),
    } for tweet in tweets]


def _search_via_graphql(term: str, limit: int, timeout: float) -> tuple[list[dict] | None, str | None]:
    """Cookie-based GraphQL search; returns ``(tweets, config_error)``."""
    options = credentials.options_for("x")
    query_id = options.get("graphql_query_id")
    if not query_id:
        return None, (
            "X session cookies are configured but graphql_query_id is missing; "
            "X rotates that id with each deploy, so it must be supplied"
        )
    cookies = credentials.cookies_for("x")
    missing = [name for name in _REQUIRED_COOKIES if not cookies.get(name)]
    if missing:
        return None, (
            f"X session cookies are missing {', '.join(missing)}; "
            "export them from a logged-in x.com session"
        )

    variables = json.dumps({
        "rawQuery": term,
        "count": max(10, min(limit, 50)),
        "querySource": "typed_query",
        "product": "Latest",
    }, separators=(",", ":"))
    features = json.dumps({
        "rweb_video_screen_enabled": False,
        "payments_enabled": False,
        "profile_label_improvements_pcf_label_in_post_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "verified_phone_label_enabled": False,
    }, separators=(",", ":"))
    qs = urlencode({"variables": variables, "features": features})
    req = Request(_GRAPHQL_SEARCH.format(query_id=quote(str(query_id)), qs=qs), headers={
        "User-Agent": _UA,
        "Accept": "application/json",
        "Cookie": credentials.cookie_header("x") or "",
        # The web client always sends these alongside the cookies; X rejects the
        # request without the bearer that ships in its public bundle.
        "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "X-Csrf-Token": cookies.get("ct0", ""),
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Auth-Type": "OAuth2Session",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(_read_capped(resp).decode("utf-8", "replace"))
    except HTTPError as exc:
        # 403 with a valid session almost always means the query id rotated.
        detail = f"HTTP {exc.code}"
        if exc.code in (403, 404):
            detail += " — graphql_query_id is probably stale; re-copy it from x.com"
        logger.warning("X GraphQL search failed for %s: %s", term, detail)
        return None, detail
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("X GraphQL search failed for %s: %s", term, exc)
        return None, None

    tweets = []
    instructions = (
        ((payload.get("data") or {}).get("search_by_raw_query") or {})
        .get("search_timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    ) if isinstance(payload, dict) else []
    for instruction in instructions:
        for entry in instruction.get("entries") or []:
            content = entry.get("content") or {}
            result = ((content.get("itemContent") or {}).get("tweet_results") or {}).get("result") or {}
            legacy = result.get("legacy") or {}
            text = legacy.get("full_text")
            if not text:
                continue
            tweets.append({
                "text": text,
                "created_at": legacy.get("created_at"),
                "likes": legacy.get("favorite_count"),
                "reposts": legacy.get("retweet_count"),
            })
            if len(tweets) >= limit:
                return tweets, None
    return tweets, None


def fetch_x_posts(
    ticker: str,
    *,
    limit: int = 20,
    timeout: float = 15.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Recent X posts mentioning ``ticker`` as a formatted plaintext block."""
    if not credentials.is_configured("x"):
        return (
            "<X unavailable: no credentials configured; add a bearer token or a "
            "logged-in session cookie export on the Cấu hình screen. This is not "
            "an absence of discussion>"
        )

    term = _search_term(ticker)
    config_error = None
    if credentials.options_for("x").get("bearer_token"):
        tweets = _search_via_api(term, limit, timeout)
    else:
        tweets, config_error = _search_via_graphql(term, limit, timeout)

    if tweets is None:
        detail = f" ({config_error})" if config_error else ""
        return f"<X unavailable: search request failed for {term}{detail}; this is not an absence of discussion>"

    if start_date and end_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        windowed = [tweet for tweet in tweets if in_window(_created_at(tweet), start_dt, end_dt)]
    else:
        windowed = tweets

    if not windowed:
        gap = None
        if start_date and end_date:
            gap = coverage_gap(
                (_created_at(tweet) for tweet in tweets), start_date, end_date,
                "X search", f"discussion of {term}",
            )
        return gap or f"<no X posts found mentioning {term}>"

    lines = [f"{len(windowed)} recent X posts mentioning {term}:"]
    for tweet in windowed[:limit]:
        created = _created_at(tweet)
        stamp = created.strftime("%Y-%m-%d %H:%M") if created else "?"
        text = (tweet.get("text") or "").replace("\n", " ").strip()
        if len(text) > 240:
            text = text[:240] + "…"
        engagement = ""
        if tweet.get("likes") is not None or tweet.get("reposts") is not None:
            engagement = f" (likes {tweet.get('likes', '?')}, reposts {tweet.get('reposts', '?')})"
        lines.append(f"  [{stamp}] {text}{engagement}")
    return "\n".join(lines)
