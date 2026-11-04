"""Shared ASGI client, server state, and network guard for server API tests."""

import socket
import weakref
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from server import daily as daily_mod, runs as runs_mod
from server.main import app
from tradingagents.dataflows.config import set_config


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests.

    The ASGI transport itself never opens a socket, but creating the asyncio
    portal's event loop on Windows builds a loopback self-pipe via
    ``socket.socketpair``. That internal pair is whitelisted by marking the
    sockets it returns; every other ``connect`` — a stray vendor/LLM call —
    still refuses, keeping the no-network guarantee.
    """
    if request.node.get_closest_marker("integration"):
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def marked_socketpair(*args, **kwargs):
        # Windows socketpair() performs its own loopback handshake through
        # socket.connect, so the refusal must step aside for its duration,
        # then re-arm. Only the pair it returns is whitelisted afterwards.
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


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


@pytest.fixture(autouse=True)
def _server_state(tmp_path, monkeypatch, postgres_schema):
    """Point all server state at tmp and start every test with an empty registry.

    ``watchlist._save_state`` deliberately syncs ``DEFAULT_CONFIG
    ["exec_watchlist"]`` (process-global, so the in-process daily runner sees
    the same list), which would leak into later test files
    (test_execution_daily reads the default) — snapshot and restore it.
    """
    from tradingagents.default_config import DEFAULT_CONFIG

    saved_watchlist = DEFAULT_CONFIG.get("exec_watchlist")
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})
    set_config({"exec_live": False})
    yield
    DEFAULT_CONFIG["exec_watchlist"] = saved_watchlist


@pytest.fixture()
def client(_server_state):
    with TestClient(app) as test_client:
        yield test_client
