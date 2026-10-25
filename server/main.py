"""FastAPI entry point for the TradingAgents control panel.

Run from the repository root (the ``web`` extra supplies fastapi/uvicorn):

    .venv\\Scripts\\python.exe -m uvicorn server.main:app --port 8000

Storage is PostgreSQL: ``TRADINGAGENTS_DATABASE_URL`` is required and the
versioned migrations under ``server/migrations`` apply at startup before the
API serves a request. There is no SQLite fallback.

The CSRF guard (contract §0) protects every state-changing method: a hostile
page in the same browser can only fire ``fetch(..., {mode: "no-cors"})`` at
localhost, which is limited to CORS-safelisted content types — so POST/PUT/
DELETE must speak ``application/json`` and may not carry a foreign ``Origin``
or ``Sec-Fetch-Site: cross-site``. GET and SSE never change state and are
exempt. This is not authentication: bind beyond 127.0.0.1 only after adding a
token (contract §0, §11.12).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request

from server import (
    approvals,
    audit,
    autoloop,
    bracket_monitor,
    control_store,
    daily,
    health,
    history,
    job_store,
    operator_config,
    performance,
    portfolio,
    position_monitor,
    postgres,
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

logger = logging.getLogger(__name__)


def _apply_operator_config() -> None:
    """Apply the operator's stored overrides from PostgreSQL.

    The shipped engine defaults stay fail-closed (tests pin them); an operator
    opens gates like ``exec_auto_confirm`` by storing an override through
    ``PUT /api/operator-config`` (or the Cấu hình screen), which writes
    ``control_operator_settings``. Called after migrations so the table exists.

    Only the allowlisted keys in ``server/operator_config.py`` are applied; a
    stored row for an unknown key is ignored rather than injected into config.
    """
    from server.operator_config import _ALLOWED
    from tradingagents.dataflows.config import set_config

    stored = control_store.load_operator_settings()
    applied = {key: value for key, value in stored.items() if key in _ALLOWED}
    if applied:
        set_config(applied)
        logger.info("applied %d operator override(s) from PostgreSQL", len(applied))


def _open_state_db() -> None:
    """Migrate PostgreSQL, then close out what the last process left running.

    A job or run recorded as active belongs to a thread that no longer exists
    after a restart, and the worker lock is derived from that status — so
    without this reconciliation a crashed job would answer 409 to every new one
    forever, with nothing in the UI able to show or clear it. Reconciliation
    writes an audit line per orphan, so the operator can see it happened.

    PostgreSQL is required: there is no silent SQLite fallback. A missing or
    unreachable database fails startup instead of serving unpersisted state.
    """
    postgres.migrate()
    logger.info("PostgreSQL migrations are up to date")
    daily.reconcile()  # each reconciled orphan writes its own audit line
    runs.reconcile()
    job_store.reconcile_futures_cycles()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Migrations first: operator overrides live in PostgreSQL, so the table has
    # to exist before they can be read back into the process config.
    _open_state_db()
    _apply_operator_config()
    watchlist.ensure_seeded()
    with suppress(OSError):  # unreadable data dir must not keep the server down; the loop stays off
        autoloop.ensure_started()
    bracket_monitor.start()
    position_monitor.start()
    try:
        yield
    finally:
        autoloop.stop()
        position_monitor.stop()
        bracket_monitor.stop()


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
    operator_config.router,
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
