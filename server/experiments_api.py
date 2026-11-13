"""Read-only experiment registry API: GET /api/experiments (plan §4).

An aggregate over the experiment store only. No POST/PUT/DELETE, no state
change, no LLM call: an empty registry answers ``unavailable`` with a reason.
Comparison verdicts pass through verbatim — including
``insufficient_evidence_*`` — and the disclaimer rides along, because an
experiment comparison is never a winner claim or financial advice.
"""

from fastapi import APIRouter

from server import experiment_store
from server.contract import utc_now_iso

router = APIRouter(prefix="/api", tags=["experiments"])


@router.get("/experiments")
def get_experiments(limit: int = 50, offset: int = 0):
    """The registry summary: registered runs and their comparison verdicts."""
    payload = experiment_store.summary(limit=limit, offset=offset)
    payload["generated_at"] = utc_now_iso()
    payload["disclaimer"] = (
        "Experiment comparisons are simulated, paired and explicit about "
        "insufficient evidence; they are never a winner claim or a promotion. "
        "Research framework — not financial advice."
    )
    return payload


@router.get("/experiments/{run_id}")
def get_experiment_run(run_id: str):
    """One run's registered manifest, per-repeat rows and comparisons."""
    payload = experiment_store.run_results(run_id)
    payload["generated_at"] = utc_now_iso()
    payload["disclaimer"] = (
        "Experiment comparisons are simulated, paired and explicit about "
        "insufficient evidence; they are never a winner claim or a promotion. "
        "Research framework — not financial advice."
    )
    return payload
