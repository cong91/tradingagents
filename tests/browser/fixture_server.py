"""Build and serve isolated production Next.js fixtures for browser tests."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

_NO_API_REWRITE_CONFIG = """import type { NextConfig } from \"next\";

const nextConfig: NextConfig = {
  distDir: process.env.NEXT_DIST_DIR || \".next\",
};

export default nextConfig;
"""


def _reap_process_tree(pid: int) -> None:
    """Kill only the process tree started by this fixture."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/pid", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return

    import signal

    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _fixture_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        **os.environ,
        "NEXT_TELEMETRY_DISABLED": "1",
        # Every build lives under its own temporary fixture root.
        "NEXT_DIST_DIR": ".next",
    }
    if overrides:
        environment.update(overrides)
    environment["NEXT_DIST_DIR"] = ".next"
    return environment


def build_fixture_root(
    web: Path,
    root: Path,
    *,
    layout_source: str,
    pages: Mapping[str, str],
    env_overrides: Mapping[str, str] | None = None,
) -> None:
    """Materialise and build a production Next fixture in ``root``.

    The generated config deliberately has no rewrites. Fixture pages intercept
    known API calls in the browser; every other ``/api`` path remains local and
    returns the fixture server's own 404 response.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(web / "components", root / "components")
    shutil.copytree(web / "lib", root / "lib")
    (root / "app").mkdir()
    shutil.copy(web / "postcss.config.mjs", root / "postcss.config.mjs")
    shutil.copy(web / "app" / "globals.css", root / "app" / "globals.css")
    (root / "next.config.ts").write_text(_NO_API_REWRITE_CONFIG, encoding="utf-8")

    modules = root / "node_modules"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(modules), str(web / "node_modules")],
            check=True,
            capture_output=True,
            encoding="utf-8",
        )
    else:
        modules.symlink_to(web / "node_modules", target_is_directory=True)

    (root / "package.json").write_text('{"private":true}', encoding="utf-8")
    (root / "tsconfig.json").write_text(
        "{\"compilerOptions\":{\"target\":\"ES2017\",\"lib\":[\"dom\",\"dom.iterable\",\"esnext\"],"
        "\"jsx\":\"react-jsx\",\"module\":\"esnext\",\"moduleResolution\":\"bundler\","
        "\"strict\":true,\"noEmit\":true,\"skipLibCheck\":true,\"esModuleInterop\":true,"
        "\"paths\":{\"@/*\":[\"./*\"]}}}",
        encoding="utf-8",
    )

    (root / "app" / "layout.tsx").write_text(layout_source, encoding="utf-8")
    for relative_path, source in pages.items():
        destination = root / "app" / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")

    build = subprocess.run(
        ["node", str(web / "node_modules/next/dist/bin/next"), "build", "--webpack"],
        cwd=root,
        env=_fixture_environment(env_overrides),
        capture_output=True,
        encoding="utf-8",
        timeout=900,
    )
    if build.returncode != 0:
        raise RuntimeError(
            "next build failed in the browser fixture:\n"
            f"{build.stdout}\n{build.stderr}"
        )


def start_production_server(
    web: Path,
    root: Path,
    port: int,
    *,
    env_overrides: Mapping[str, str] | None = None,
):
    """Serve a built fixture with ``next start`` and return its owned log."""
    logfile = (root / "next.log").open("w+", encoding="utf-8")
    server = subprocess.Popen(
        [
            "node",
            str(web / "node_modules/next/dist/bin/next"),
            "start",
            "-H",
            "127.0.0.1",
            "-p",
            str(port),
        ],
        cwd=root,
        env=_fixture_environment(env_overrides),
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
    )
    return server, logfile


def wait_for_server(server: subprocess.Popen, url: str, deadline_seconds: float = 120) -> None:
    """Wait for a fixture root to answer, with a bounded startup deadline."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"the Next fixture server exited early:\n{_server_log(server)}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError(f"Next fixture startup timed out:\n{_server_log(server)}")


def _server_log(server: subprocess.Popen) -> str:
    logfile = getattr(server, "stdout", None)
    if logfile is None or logfile.closed:
        return "(server log unavailable)"
    logfile.flush()
    position = logfile.tell()
    logfile.seek(0)
    content = logfile.read()
    logfile.seek(position)
    return content


def stop_production_server(server: subprocess.Popen, logfile) -> None:
    """Stop the owned Next process tree and close its log."""
    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _reap_process_tree(server.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            server.wait(timeout=15)
    if not logfile.closed:
        logfile.close()


__all__ = [
    "build_fixture_root",
    "start_production_server",
    "stop_production_server",
    "wait_for_server",
]
