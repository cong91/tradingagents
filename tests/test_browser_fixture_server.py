"""Production fixture cleanup targets only the session that fixture starts."""

from __future__ import annotations

import os
import signal
from types import SimpleNamespace

import pytest

from tests.browser import fixture_server

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("platform", "owns_session"), [("posix", True), ("nt", False)])
def test_fixture_server_creates_its_own_posix_session(tmp_path, monkeypatch, platform, owns_session):
    calls = []
    process = SimpleNamespace(pid=321)
    monkeypatch.setattr(fixture_server, "os", SimpleNamespace(name=platform, environ=dict(os.environ)))

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr(fixture_server.subprocess, "Popen", start)
    server, logfile = fixture_server.start_production_server(tmp_path / "web", tmp_path, 4321)
    try:
        assert server is process
        (command, options), = calls
        assert command[-4:] == ["-H", "127.0.0.1", "-p", "4321"]
        assert options["start_new_session"] is owns_session
        assert options["env"]["NEXT_DIST_DIR"] == ".next"
    finally:
        logfile.close()


def test_posix_cleanup_uses_the_owned_session_id_even_after_parent_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    def unrelated_group(_pid):
        pytest.fail("cleanup must never resolve a process into the caller's group")

    monkeypatch.setattr(
        fixture_server, "os", SimpleNamespace(
            name="posix", getpgid=unrelated_group,
            killpg=lambda group, sig: calls.append((group, sig)),
        ),
    )
    fixture_server._reap_process_tree(321)
    assert calls == [(321, signal.SIGKILL)]
