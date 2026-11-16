"""Read-only forecast evaluation report."""

from fastapi import APIRouter

from server import forecast_evaluation_store
from server.contract import utc_now_iso

router = APIRouter(prefix="/api", tags=["forecast-evaluations"])


@router.get("/forecast-evaluations")
def get_forecast_evaluations(limit: int = 100, symbol: str | None = None):
    if limit < 1 or limit > 500:
        from server.contract import ApiError

        raise ApiError(400, "validation_error", '"limit" must be between 1 and 500')
    payload = forecast_evaluation_store.summary(limit=limit, symbol=symbol)
    payload["generated_at"] = utc_now_iso()
    payload["disclaimer"] = (
        "Forward market-price evaluation only; it is not realized PnL and does not change execution decisions."
    )
    return payload
