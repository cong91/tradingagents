"""FastAPI entry point for the TradingAgents control panel.

Run from the repository root (the ``web`` extra supplies fastapi/uvicorn):

    .venv\\Scripts\\python.exe -m uvicorn server.main:app --port 8000

The CSRF guard (contract §0) protects every state-changing method: a hostile
page in the same browser can only fire ``fetch(..., {mode: "no-cors"})`` at
localhost, which is limited to CORS-safelisted content types — so POST/PUT/
DELETE must speak ``application/json`` and may not carry a foreign ``Origin``
or ``Sec-Fetch-Site: cross-site``. GET and SSE never change state and are
exempt. This is not authentication: bind beyond 127.0.0.1 only after adding a
token (contract §0, §11.12).
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request

from server import (
    approvals,
    audit,
    autoloop,
    daily,
    db,
    health,
    history,
    job_store,
    paths,
    performance,
    portfolio,
    runs,
    settings,
    watchlist,
)
from server.contract import error_response, install_error_handlers

# Origins allowed to drive state-changing requests: the server itself, the
# contract's control-panel origin, and the Next.js dev server that proxies /api.
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8420",
    "http://localhost:8420",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
}

_STATE_CHANGING_METHODS = {"POST", "PUT", "DELETE"}


def _apply_operator_config() -> None:
    """Load the operator's arming overrides (server/data/operator_config.json)
    into the process config at startup.

    The shipped engine defaults stay fail-closed (tests pin them); an operator
    arms gates like ``exec_auto_confirm`` through this local file instead of
    editing engine code. Malformed or unreadable content is ignored — the
    loop simply stays off.
    """
    import json

    from tradingagents.dataflows.config import set_config

    try:
        data = json.loads(paths.OPERATOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        set_config({k: v for k, v in data.items() if not k.startswith("_")})


def _open_state_db() -> None:
    """Open the API-layer database and close out what the last process left running.

    A job or run recorded as active belongs to a thread that no longer exists
    after a restart, and the worker lock is derived from that status — so
    without this reconciliation a crashed job would answer 409 to every new one
    forever, with nothing in the UI able to show or clear it. Reconciliation
    writes an audit line per orphan, so the operator can see it happened.
    """
    db.connect().close()  # creates the file and the schema
    daily.reconcile()  # each reconciled orphan writes its own audit line
    runs.reconcile()
    job_store.reconcile_futures_cycles()
    with suppress(Exception):
        autoloop.recover_pending_brackets()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    with suppress(OSError):  # an unreadable data dir must not keep the server down
        _apply_operator_config()
    with suppress(OSError, sqlite3.Error):  # a broken database must not keep the server down
        _open_state_db()
    watchlist.ensure_seeded()
    with suppress(OSError):  # unreadable data dir must not keep the server down; the loop stays off
        autoloop.ensure_started()
    yield


app = FastAPI(title="TradingAgents Control Panel", version="0.1.0", lifespan=lifespan)
install_error_handlers(app)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    if request.method in _STATE_CHANGING_METHODS:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return error_response(
                415, "unsupported_media_type", "Content-Type: application/json is required"
            )
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            return error_response(
                403, "csrf_rejected", f"origin {origin!r} is not allowed", {"origin": origin}
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return error_response(
                403, "csrf_rejected", "cross-site requests are not allowed"
            )
    return await call_next(request)


for _router in (
    settings.router,
    watchlist.router,
    runs.router,
    daily.router,
    portfolio.router,
    approvals.router,
    audit.router,
    history.router,
    health.router,
    performance.router,
    autoloop.router,
):
    app.include_router(_router)
