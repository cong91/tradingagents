"""Availability guard for tests that render web components through Node.

The Python test matrix must pass on machines without the web toolchain: a
missing node_modules reads as an environment skip, the same way live-API
tests skip without keys and PostgreSQL tests skip without a test DSN.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
_REQUIRED_MODULES = ("typescript", "react", "react-dom")


def require_web_renderer() -> Path:
    """Skip the calling test unless node and the web modules are installed."""
    if shutil.which("node") is None:
        pytest.skip("node is unavailable; the web renderer cannot run")
    missing = [
        name for name in _REQUIRED_MODULES
        if not (WEB_ROOT / "node_modules" / name).is_dir()
    ]
    if missing:
        pytest.skip(
            f"web/node_modules is missing {', '.join(missing)}; run npm install in web/"
        )
    return WEB_ROOT
