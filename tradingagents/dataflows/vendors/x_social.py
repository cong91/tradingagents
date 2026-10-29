"""X cashtag search using the operator's saved session cookies via twscrape."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from twscrape import NoAccountError

from tradingagents.dataflows import credentials
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.symbols import crypto_base, futures_base
from tradingagents.dataflows.vendors.x_session import search_posts

logger = logging.getLogger(__name__)
_REQUIRED_COOKIES = ("auth_token", "ct0")


def _search_term(ticker: str) -> str:
    base = crypto_base(ticker) or futures_base(ticker)
    return f"${base}" if base else ticker


def _created_at(tweet: dict) -> datetime | None:
    raw = tweet.get("created_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_x_posts(
    ticker: str,
    *,
    limit: int = 20,
    timeout: float = 30.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Recent posts as dated evidence; inaccessible sources never imply silence."""
    if not credentials.is_configured("x"):
        return (
            "<X unavailable: no credentials configured; add a logged-in session "
            "cookie export on the Cấu hình screen. This is not an absence of discussion>"
        )
    cookies = credentials.cookies_for("x")
    missing = [key for key in _REQUIRED_COOKIES if not cookies.get(key, "").strip()]
    if missing:
        return (
            f"<X unavailable: session cookies are missing {', '.join(missing)}; "
            "re-import a logged-in x.com session. This is not an absence of discussion>"
        )
    term = _search_term(ticker)
    query = f"{term} -filter:retweets"
    try:
        if limit < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("limit and timeout must be positive")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
        if start_dt and end_dt and start_dt > end_dt:
            raise ValueError("start_date is after end_date")
        if start_dt:
            query += f" since:{start_dt:%Y-%m-%d}"
        if end_dt:
            query += f" until:{end_dt + timedelta(days=1):%Y-%m-%d}"
    except (ValueError, TypeError):
        return "<X unavailable: invalid search limit, timeout, or date window; this is not an absence of discussion>"
    try:
        tweets = search_posts(cookies, query, limit=min(limit, 50), timeout=timeout)
    except Exception as exc:
        # Exception text can contain upstream URLs, payloads, and session data.
        logger.warning("X search failed for %s (%s)", term, type(exc).__name__)
        if isinstance(exc, TimeoutError):
            detail = "search timed out; retry later"
        elif isinstance(exc, NoAccountError):
            detail = "session expired, rate-limited, or temporarily unavailable; retry later or re-import cookies"
        else:
            detail = f"search request failed ({type(exc).__name__}); retry later"
        return f"<X unavailable: {detail}; this is not an absence of discussion>"

    windowed = [
        tweet for tweet in tweets
        if not (start_dt and end_dt) or in_window(_created_at(tweet), start_dt, end_dt)
    ]
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
        stamp = created.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if created else "?"
        text = (tweet.get("text") or "").replace("\n", " ").strip()
        if len(text) > 1000:
            text = text[:1000] + "…"
        engagement = f" (likes {tweet.get('likes', '?')}, reposts {tweet.get('reposts', '?')})"
        source = f"\n    {tweet['url']}" if tweet.get("url") else ""
        lines.append(f"  [{stamp}] {text}{engagement}{source}")
    return "\n".join(lines)
