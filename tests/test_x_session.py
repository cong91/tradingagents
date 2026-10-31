"""The twscrape session never persists the stored cookie or waits indefinitely."""

import asyncio
import socket
import weakref
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from twscrape import NoAccountError
from twscrape.api import API
from twscrape.db import DB
from twscrape.queue_client import XClIdGenStore

from tradingagents.dataflows.vendors import x_session

COOKIES = {"auth_token": "test-auth-secret", "ct0": "test-csrf-secret", "guest_id": "test-guest"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Allow only asyncio's Windows self-pipe; every vendor connection fails."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets = weakref.WeakSet()

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def marked_socketpair(*args, **kwargs):
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        try:
            first, second = original_socketpair(*args, **kwargs)
        finally:
            socket.socket.connect = refuse_connect
            socket.socket.connect_ex = refuse_connect_ex
        selfpipe_sockets.add(first)
        selfpipe_sockets.add(second)
        return first, second

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect_ex)
    monkeypatch.setattr(socket, "socketpair", marked_socketpair)


@pytest.mark.unit
def test_real_scraper_receives_cookie_account_without_a_database(monkeypatch):
    seen = {}

    async def refuse_database(*args, **kwargs):
        pytest.fail("twscrape wrote a cookie database")

    async def search(self, query, limit=-1, kv=None):
        account = await self.pool.get_for_queue_or_wait("SearchTimeline")
        seen.update(query=query, limit=limit, account=account, cookies=dict(account.cookies))
        yield SimpleNamespace(rawContent="Bitcoin discussion", date=datetime(2026, 10, 26, tzinfo=UTC),
                              likeCount=4, retweetCount=2, url="https://x.com/user/status/1")
        await self.pool.unlock(account.username, "SearchTimeline", 1)

    monkeypatch.setattr(DB, "__aenter__", refuse_database)
    monkeypatch.setattr(API, "search", search)
    rows = x_session.search_posts(COOKIES, "$BTC -filter:retweets", limit=3, timeout=2)
    assert seen["cookies"] == COOKIES
    assert seen["account"].cookies == {}
    assert seen["account"].password == "_" and seen["account"].active is True
    assert seen["limit"] == 3 and seen["query"] == "$BTC -filter:retweets"
    assert rows == [{"text": "Bitcoin discussion", "created_at": "2026-10-26T00:00:00+00:00",
                     "likes": 4, "reposts": 2, "url": "https://x.com/user/status/1"}]


@pytest.mark.unit
def test_rate_limited_cookie_is_not_reacquired_in_a_retry_loop(monkeypatch):
    async def search(self, query, **kwargs):
        account = await self.pool.get_for_queue_or_wait("SearchTimeline")
        await self.pool.lock_until(account.username, "SearchTimeline", 9999999999)
        await self.pool.get_for_queue_or_wait("SearchTimeline")
        yield

    monkeypatch.setattr(API, "search", search)
    with pytest.raises(NoAccountError):
        x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)


@pytest.mark.unit
def test_expired_cookie_is_not_reported_as_successful_empty_search(monkeypatch):
    async def search(self, query, **kwargs):
        account = await self.pool.get_for_queue_or_wait("SearchTimeline")
        await self.pool.mark_inactive(account.username, "Session expired")
        await self.pool.get_for_queue_or_wait("SearchTimeline")
        yield

    monkeypatch.setattr(API, "search", search)
    with pytest.raises(NoAccountError):
        x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)


@pytest.mark.unit
def test_total_search_timeout_cancels_and_closes_the_generator(monkeypatch):
    closed = []

    async def search(self, query, **kwargs):
        try:
            await asyncio.sleep(30)
            yield
        finally:
            closed.append(True)

    monkeypatch.setattr(API, "search", search)
    with pytest.raises(TimeoutError):
        x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=0.03)
    assert closed == [True]


@pytest.mark.unit
def test_sync_fetch_is_safe_when_called_from_an_async_context(monkeypatch):
    async def search(self, query, **kwargs):
        yield SimpleNamespace(rawContent="post", date=datetime.now(UTC), likeCount=0,
                              retweetCount=0, url="https://x.com/u/status/2")

    monkeypatch.setattr(API, "search", search)

    async def caller():
        return x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)

    assert asyncio.run(caller())[0]["text"] == "post"


@pytest.mark.unit
def test_search_limit_stops_a_generator_that_keeps_yielding(monkeypatch):
    closed = []

    async def search(self, query, **kwargs):
        try:
            for number in range(20):
                yield SimpleNamespace(rawContent=str(number), date=datetime.now(UTC),
                                      likeCount=0, retweetCount=0, url=f"https://x.com/u/status/{number}")
        finally:
            closed.append(True)

    monkeypatch.setattr(API, "search", search)
    assert len(x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)) == 3
    assert closed == [True]


@pytest.mark.unit
def test_signing_session_is_removed_after_failure(monkeypatch):
    usernames = []

    async def search(self, query, **kwargs):
        account = await self.pool.get_for_queue_or_wait("SearchTimeline")
        usernames.append(account.username)
        XClIdGenStore.items[account.username] = object()
        raise RuntimeError("failed")
        yield

    monkeypatch.setattr(API, "search", search)
    with pytest.raises(RuntimeError):
        x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)
    assert all(name not in XClIdGenStore.items for name in usernames)


@pytest.mark.unit
def test_upstream_telemetry_and_secret_logs_are_disabled(monkeypatch, capsys):
    from twscrape import telemetry
    from twscrape.logger import logger

    monkeypatch.delenv("TWS_TELEMETRY", raising=False)
    telemetry.reset()

    async def search(self, query, **kwargs):
        telemetry.capture("gql_request", {"operation": "SearchTimeline"})
        logger.warning("upstream cookie: {}", COOKIES["auth_token"])
        yield SimpleNamespace(rawContent="post", date=datetime.now(UTC), likeCount=0,
                              retweetCount=0, url="https://x.com/u/status/2")

    monkeypatch.setattr(API, "search", search)
    x_session.search_posts(COOKIES, "$BTC", limit=3, timeout=1)
    assert telemetry.snapshot() == []
    assert COOKIES["auth_token"] not in capsys.readouterr().err
