"""Turn one provider attempt into one storable observation row.

The recorder owns the honesty rules that must not be re-derived per provider:

- a request must be a contiguous, ascending, single-timeframe series of CLOSED
  candles, all of them closing at or before ``as_of`` — the forming bar and any
  candle after the decision moment are refused, so a row can never be built on
  look-ahead;
- ``generation_kind`` is decided HERE, by comparing ``prediction_created_at``
  with the decision moment: a forecast made at/after the decision is
  ``forward``; a replay that runs later is ``retrospective`` and says so;
- ``outcome_group`` is derived from the cycle's recorded execution status, never
  guessed: an executed cycle is not a sample of the whole universe and the three
  groups are kept apart;
- the calibration label always travels with the numbers, and a deterministic
  rule stores no band, dispersion or sample ratio;
- a failed attempt stores its status and reason with empty paths — a forecast is
  never padded to make a row look complete.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from tradingagents.dataflows.market_data import Candle, timeframe_ms
from tradingagents.shadow.provider import (
    CALIBRATION_NOTES,
    DISTRIBUTION_RULE,
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    OUTCOME_ABSTAINED,
    OUTCOME_EXECUTED,
    OUTCOME_REJECTED,
    STATUS_OK,
    ForecastAttempt,
    ForecastProvider,
    ForecastRequest,
)


class RequestError(ValueError):
    """The candle series cannot honestly be used for an observation."""


def merge_candles(*batches: list[Candle] | tuple[Candle, ...]) -> tuple[Candle, ...]:
    """Union of candle sources by (timeframe, open time); mismatches raise.

    The worker sees the same bar twice (the cycle snapshot's own candle is also
    in any venue history it reads) and so does a replay that joins an export to
    a snapshot. Collapsing an identical duplicate is not interpolation — the two
    copies agree on every price — but a disagreement means the decision's own
    prices cannot be reconstructed, so it is refused rather than resolved by
    preferring one source.

    The key carries the timeframe because a 4h bar and a 1h bar share an open
    time without being the same bar; comparing them would refuse a correct series.
    """
    by_key: dict[tuple[str, int], Candle] = {}
    origin: dict[tuple[str, int], int] = {}
    for index, batch in enumerate(batches):
        for candle in batch:
            key = (candle.timeframe, candle.open_time)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = candle
                origin[key] = index
                continue
            if (existing.open, existing.high, existing.low, existing.close) != (
                candle.open, candle.high, candle.low, candle.close,
            ):
                raise RequestError(
                    f"candle sources {origin[key]} and {index} disagree about the "
                    f"{candle.timeframe} candle at {candle.open_time}; the prices the "
                    "decision saw cannot be reconstructed"
                )
    return tuple(by_key[key] for key in sorted(by_key))


def validate_candles(candles: list[Candle] | tuple[Candle, ...], timeframe: str,
                     as_of: datetime) -> tuple[Candle, ...]:
    """A contiguous closed series ending at or before ``as_of``; else raise.

    Every rejection reason is explicit because each one would otherwise become
    silent look-ahead or a fabricated bar: a forming candle, a mix of
    timeframes, an overlap/duplicate, a gap, or a candle that closes after the
    moment the forecast is supposed to have been made.
    """
    if as_of.tzinfo is None:
        raise RequestError("as_of must be timezone-aware")
    if not candles:
        raise RequestError("no closed candles were supplied")
    duration = timeframe_ms(timeframe)
    cutoff_ms = int(as_of.astimezone(UTC).timestamp() * 1000)
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    checked: list[Candle] = []
    for candle in ordered:
        if not candle.is_closed:
            raise RequestError("a forming candle was supplied; only closed candles may be used")
        if candle.timeframe != timeframe:
            raise RequestError(
                f"mixed timeframes: expected {timeframe}, got {candle.timeframe}"
            )
        if candle.close_time - candle.open_time != duration:
            raise RequestError("a candle's duration does not match the timeframe")
        if candle.close_time > cutoff_ms:
            raise RequestError(
                "a candle closes after as_of; the observation may not look ahead"
            )
        if checked and candle.open_time < checked[-1].close_time:
            raise RequestError("duplicate or overlapping candle")
        if checked and candle.open_time != checked[-1].close_time:
            raise RequestError("the closed-candle series has a gap")
        checked.append(candle)
    return tuple(checked)


def moment_from_last_close(candles: list[Candle] | tuple[Candle, ...]) -> datetime | None:
    """The earliest moment a decision on these candles could have existed.

    Used only where a recorded cycle carries no timestamp at all: the decision
    cannot predate the close of the newest candle it looked at. It is conservative
    by construction — a series cut at this moment never includes a candle that
    closed after it — so deriving it cannot smuggle in look-ahead.
    """
    if not candles:
        return None
    newest = max(candles, key=lambda candle: candle.close_time)
    return datetime.fromtimestamp(newest.close_time / 1000, UTC)


def outcome_group_for(execution_status: str | None) -> tuple[str, str]:
    """Map the cycle's recorded execution status to an observation group.

    ``executed`` means the runtime actually filled; everything the runtime
    refused keeps its own reason but lands in ``rejected``; a cycle that never
    produced a proposal (no signal, market/confirmation rejection) is
    ``abstained`` — it was not evaluated against a trade the bot wanted to take.
    An unknown status is not folded into a group: it becomes ``abstained`` with
    the raw value recorded as the reason, never invented as ``executed``.
    """
    if execution_status == "executed":
        return OUTCOME_EXECUTED, "execution_status=executed"
    if execution_status in ("risk_rejected", "not_executed", "awaiting_approval"):
        return OUTCOME_REJECTED, f"execution_status={execution_status}"
    if execution_status in (None, "", "no_signal", "market_rejected", "confirmation_rejected"):
        return OUTCOME_ABSTAINED, f"execution_status={execution_status or 'none'}"
    return OUTCOME_ABSTAINED, f"execution_status={execution_status}"


def calibration_note(label: str) -> str:
    """The note that must travel with a calibration label.

    A provider is a plugin: its label comes from outside this package, so an
    unknown one must not raise a bare ``KeyError`` in the middle of building a
    row (a provider bug would then look like a worker crash). The row still
    records the label it was given; the note says the package has no text for it,
    which is itself the honest statement.
    """
    note = CALIBRATION_NOTES.get(label)
    if note is not None:
        return note
    return (
        f"the provider declared calibration label {label!r}, which this package "
        "does not define; treat the numbers as uncalibrated model output"
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _samples_for(kind: str, attempt: ForecastAttempt, ok: bool) -> dict[str, Any]:
    """What the row may claim about the distribution behind the verdict.

    A failed attempt claims nothing. A deterministic rule keeps its own inputs
    under ``rule`` — never ``direction_ratio``/``band``/``dispersion``, keys a
    reader would take as model statistics.
    """
    if not ok:
        return {}
    if kind == DISTRIBUTION_RULE:
        return {"rule": dict(attempt.samples or {})}
    return dict(attempt.samples or {})


def build_observation(
    *,
    provider: ForecastProvider,
    request: ForecastRequest,
    attempt: ForecastAttempt,
    prediction_created_at: datetime,
    decision_moment: datetime,
    cycle_id: str | None,
    execution_status: str | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``shadow_forecasts`` row; ``validate_candles`` ran before this call.

    ``generation_kind`` separates a FORWARD observation from a retrospective
    replay by the only test that matters for honesty: had the thing being
    predicted already started happening? A worker that observes a cycle minutes
    after it closed is forward — the first predicted bar has not closed, so no
    part of the outcome was knowable. A run that catches up hours later, when
    some of the predicted bars have already realized, is retrospective and says
    so: its accuracy figure would otherwise be a memory test, not a prediction.
    """
    group, group_reason = outcome_group_for(execution_status)
    ok = attempt.status == STATUS_OK
    generated = prediction_created_at.astimezone(UTC)
    decision = decision_moment.astimezone(UTC)
    # The forecast predicts the bars AFTER the last observed candle, so the first
    # bar whose outcome it predicts closes one full timeframe after that candle's
    # own close. Creating the observation before then reveals nothing about the
    # outcome; creating it later means part of the answer was already on the tape.
    first_realized_close = datetime.fromtimestamp(
        (request.candles[-1].close_time + timeframe_ms(request.timeframe)) / 1000, UTC,
    )
    generation_kind = (
        GENERATION_FORWARD if generated <= first_realized_close else GENERATION_RETROSPECTIVE
    )
    params = {**(request.params or {}), **(attempt.params or {})}
    return {
        "forecast_id": str(uuid.uuid4()),
        "cycle_id": cycle_id,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "horizon_hours": request.horizon_hours,
        "provider": provider.provider_id,
        "model_revision": provider.model_revision,
        "source_revision": provider.source_revision,
        "artifact_hash": provider.artifact_hash,
        "data_open_time": _iso(datetime.fromtimestamp(request.data_open_time / 1000, UTC)),
        "prediction_created_at": _iso(generated),
        "generation_kind": generation_kind,
        "outcome_group": group,
        "outcome_reason": group_reason,
        "sample_count": len(attempt.paths) if ok else 0,
        "per_path_sample_count": 1,
        "distribution_kind": provider.distribution_kind,
        "calibration_label": provider.calibration_label,
        "calibration_note": calibration_note(provider.calibration_label),
        "direction": attempt.direction if ok else None,
        # A deterministic rule has no distribution: no band, no dispersion, no
        # ratio. Its own diagnostic values are kept under ``rule`` so nothing in
        # the row can be read as a model sample aggregate.
        "samples": _samples_for(provider.distribution_kind, attempt, ok),
        "paths": [path.as_json() for path in attempt.paths] if ok else [],
        "status": attempt.status,
        "reason": attempt.reason,
        "seed": request.seed,
        "deadline_seconds": provider.deadline_seconds,
        "duration_ms": attempt.duration_ms,
        "params": params,
        "provenance": {
            "as_of": _iso(decision),
            "reference_close": request.reference_close,
            "candles": len(request.candles),
            "first_open_time": _iso(
                datetime.fromtimestamp(request.candles[0].open_time / 1000, UTC)
            ),
            "last_close_time": _iso(
                datetime.fromtimestamp(request.candles[-1].close_time / 1000, UTC)
            ),
            "first_realized_close": _iso(first_realized_close),
            "generation_kind_basis": (
                "prediction created no later than the first predicted bar's close"
                if generation_kind == GENERATION_FORWARD
                else (
                    "prediction created after the first predicted bar closed; "
                    "part of the outcome was already known (late replay)"
                )
            ),
            **(provenance or {}),
        },
    }


__all__ = [
    "RequestError",
    "build_observation",
    "calibration_note",
    "merge_candles",
    "moment_from_last_close",
    "outcome_group_for",
    "validate_candles",
]
