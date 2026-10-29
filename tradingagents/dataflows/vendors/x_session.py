"""Bounded twscrape searches with a cookie account held only in memory."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from datetime import timezone
from uuid import uuid4

from twscrape import API, AccountsPool, NoAccountError
from twscrape.account import Account
from twscrape.logger import logger as scraper_logger
from twscrape.queue_client import XClIdGenStore


class CookiePool(AccountsPool):
    """Supply one vault session without twscrape's plaintext SQLite persistence."""

    def __init__(self, cookies: dict[str, str]):
        super().__init__(raise_when_no_account=True)
        self.account = Account(
            username=f"tradingagents-{uuid4().hex}", password="_", email="_", email_password="_",
            user_agent="@chrome", active=True, cookies=dict(cookies),
        )
        self._available = True

    async def get_for_queue_or_wait(self, queue: str) -> Account:
        if not self._available or not self.account.active:
            raise NoAccountError("X session expired or rate-limited; re-import cookies or retry later")
        self._available = False
        return self.account

    async def unlock(self, username: str, queue: str, req_count: int = 0) -> None:
        self._available = True

    async def lock_until(self, username: str, queue: str, unlock_at: int, req_count: int = 0) -> None:
        self._available = False

    async def mark_inactive(self, username: str, error_msg: str | None) -> None:
        self.account.active = False


async def _search(cookies: dict[str, str], query: str, limit: int) -> list[dict]:
    pool = CookiePool(cookies)
    api = API(pool=pool, debug=False)
    tweets = []
    try:
        async with aclosing(api.search(query, limit=limit)) as results:
            async for tweet in results:
                created = tweet.date
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                tweets.append({
                    "text": tweet.rawContent,
                    "created_at": created.astimezone(timezone.utc).isoformat(),
                    "likes": tweet.likeCount,
                    "reposts": tweet.retweetCount,
                    "url": tweet.url,
                })
                if len(tweets) >= limit:
                    break
        return tweets
    finally:
        XClIdGenStore.items.pop(pool.account.username, None)
        pool.account.cookies.clear()


async def _bounded_search(cookies: dict[str, str], query: str, limit: int, timeout: float) -> list[dict]:
    return await asyncio.wait_for(_search(cookies, query, limit), timeout=timeout)


def search_posts(cookies: dict[str, str], query: str, *, limit: int, timeout: float) -> list[dict]:
    """Fetch parsed posts; auth, rate limits, and timeouts remain explicit failures."""
    # Upstream diagnostics may include response details. Only the vendor's
    # sanitized status is allowed into the application logs or analyst prompt.
    scraper_logger.disable("twscrape")
    os.environ["TWS_TELEMETRY"] = "0"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded_search(cookies, query, limit, timeout))
    # The public vendor is synchronous, including when invoked by an async host.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="x-search") as executor:
        return executor.submit(asyncio.run, _bounded_search(cookies, query, limit, timeout)).result()
