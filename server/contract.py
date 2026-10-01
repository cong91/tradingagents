"""Wire-contract primitives for the control-panel API.

One place for the pieces every endpoint shares: the unified error envelope
(docs/ui-api-contract.md §0.1), ``generated_at`` timestamps, and JSON body
reading. Endpoint modules build on this; ``main.py`` installs the handlers.
"""

import json
from datetime import datetime, timezone

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def utc_now_iso() -> str:
    """UTC now as ISO-8601 with a ``Z`` suffix, the contract's timestamp form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope(code, message, details))


class ApiError(Exception):
    """Raise from any handler to emit the contract's error envelope."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


async def json_body(request: Request) -> dict:
    """Read the request body as a JSON object; an empty body reads as ``{}``.

    Content-Type is already enforced by the CSRF guard in ``main.py``; this
    only rejects bodies that are not valid JSON objects.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "validation_error", "request body is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError(400, "validation_error", "request body must be a JSON object")
    return parsed


def install_error_handlers(app) -> None:
    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        code = "not_found" if exc.status_code == 404 else "http_error"
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        return error_response(400, "validation_error", "invalid query or path parameters")

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception):
        return error_response(500, "internal_error", f"unexpected server error: {type(exc).__name__}")
