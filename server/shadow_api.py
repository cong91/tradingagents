"""Read-only shadow observation API: GET /api/shadow (plan §6, §5D).

An aggregate over the ``shadow_*`` tables only. No POST/PUT/DELETE, no state
change, no provider call, no order: an empty table answers ``unavailable`` with
the reason instead of "0% agreement". Every row keeps its calibration label, so
a sample ratio is never rendered as a probability, and a retrospective row is
never presented as a live forecast.
"""

from fastapi import APIRouter

from server import shadow_store
from server.contract import ApiError, utc_now_iso

router = APIRouter(prefix="/api", tags=["shadow"])

# A page of observations is a screen of detail, not an export. Bounding it here
# means a caller cannot ask the store for the whole table with one request.
MAX_LIMIT = 200


def _checked_limit(limit: int) -> int:
    """A positive, bounded page size, or a 400 that names the caller's mistake.

    Without this, ``?limit=0`` reached PostgreSQL as ``LIMIT 0`` and ``?limit=-1``
    as ``LIMIT -1``, which surfaced as a 500 ``internal_error`` — a server fault
    message for a client-side input error.
    """
    if limit <= 0:
        raise ApiError(
            400, "validation_error", "limit must be a positive integer",
            {"limit": limit},
        )
    if limit > MAX_LIMIT:
        raise ApiError(
            400, "validation_error", f"limit must be at most {MAX_LIMIT}",
            {"limit": limit, "max": MAX_LIMIT},
        )
    return limit


@router.get("/shadow")
def get_shadow(limit: int = 20, symbol: str | None = None, provider: str | None = None):
    """Shadow observations: totals, per-provider counts, evaluations, recent rows."""
    page = _checked_limit(limit)
    payload = shadow_store.summary(limit=page)
    if symbol or provider:
        payload["recent"] = shadow_store.forecast_rows(
            limit=page, symbol=symbol, provider=provider,
        )
    payload["generated_at"] = utc_now_iso()
    payload["read_only"] = True
    payload["disclaimer"] = (
        "Observations only: the runtime's orders, risk limits and protection never "
        "read these rows. A sample ratio is not a calibrated probability. Research "
        "framework — not financial advice."
    )
    return payload
