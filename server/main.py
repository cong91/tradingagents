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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from server import approvals, audit, daily, health, history, portfolio, runs, settings, watchlist
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    watchlist.ensure_seeded()
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
):
    app.include_router(_router)
