"""Read-only outcome-ledger API: GET /api/outcomes (§ plan 2, ask "API consumer").

An aggregate over the ledger store only. No POST/PUT/DELETE, no state change,
no venue call: an empty or uningested ledger answers ``unavailable`` with the
reason — never a fabricated zero. The old decision-score endpoints
(``/api/backtest`` hit-rate/mean-alpha) keep their own semantics untouched;
this surface reports reconciled ledger data only.
"""

from fastapi import APIRouter

from server import outcome_store
from server.contract import utc_now_iso

router = APIRouter(prefix="/api", tags=["outcomes"])


@router.get("/outcomes")
def get_outcomes(window_start: str | None = None, window_end: str | None = None):
    """The ledger summary: positions, per-currency totals, coverage rows.

    ``funding_unknown_currencies`` lists currencies whose funding rows are
    absent — their net is not claimed there. Coverage warnings and statuses
    (including ``unsupported``) pass through verbatim.
    """
    payload = outcome_store.summary(window_start=window_start, window_end=window_end)
    payload["generated_at"] = utc_now_iso()
    payload["disclaimer"] = (
        "Reconciled ledger data only; missing data is reported as missing, "
        "never as zero. Research framework — not financial advice."
    )
    return payload
