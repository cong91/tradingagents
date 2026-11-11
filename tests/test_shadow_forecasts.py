"""S1: shadow observations — closed candles only, honest persistence, evaluation.

The tests here pin the observation contract itself (plan §6.1-§6.3): a row is
built only from closed candles that had already closed at the decision moment,
the calibration label says "sample ratio, not probability", a failed attempt is
stored as that failure, a deterministic rule carries no band, and an evaluation
is claimed only when the horizon actually realized.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.shadow.evaluation import evaluate_forecast, realized_candles
from tradingagents.shadow.filters import FilterProvider
from tradingagents.shadow.provider import (
    CALIBRATION_RULE,
    CALIBRATION_SAMPLE_RATIO,
    DISTRIBUTION_MODEL,
    DISTRIBUTION_RULE,
    GENERATION_FORWARD,
    GENERATION_RETROSPECTIVE,
    OUTCOME_ABSTAINED,
    OUTCOME_EXECUTED,
    OUTCOME_REJECTED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    ForecastAttempt,
    ForecastRequest,
    PathSummary,
)
from tradingagents.shadow.recorder import (
    RequestError,
    build_observation,
    outcome_group_for,
    validate_candles,
)

pytestmark = pytest.mark.unit

START_MS = 1_700_000_000_000
HOUR_MS = 3_600_000
SYMBOL = "BTC/USDT:USDT"


def _candle(index: int, *, close: float = 100.0, timeframe: str = "1h",
            is_closed: bool = True, open_time: int | None = None) -> Candle:
    step = HOUR_MS if timeframe == "1h" else 4 * HOUR_MS
    opened = START_MS + index * step if open_time is None else open_time
    return Candle(
        symbol=SYMBOL, timeframe=timeframe, open_time=opened,
        close_time=opened + step, open=close - 0.5, high=close + 1.0,
        low=close - 1.0, close=close, volume=10.0 + index, is_closed=is_closed,
    )


def _series(count: int = 8, *, start: float = 100.0) -> list[Candle]:
    return [_candle(index, close=start + index) for index in range(count)]


def _as_of(index_after_last: int = 0) -> datetime:
    """The moment right after candle ``index_after_last`` closed."""
    return datetime.fromtimestamp(
        (START_MS + (index_after_last + 1) * HOUR_MS) / 1000, UTC
    )


class _StubModel:
    """A provider shaped like the model one, with no torch anywhere."""

    provider_id = "kronos"
    model_revision = "stub@deadbeef"
    source_revision = "stub-rev"
    artifact_hash = "f" * 64
    distribution_kind = DISTRIBUTION_MODEL
    calibration_label = CALIBRATION_SAMPLE_RATIO
    deadline_seconds = 30.0

    def __init__(self, attempt: ForecastAttempt) -> None:
        self._attempt = attempt
        self.requests: list[ForecastRequest] = []

    def forecast(self, request: ForecastRequest) -> ForecastAttempt:
        self.requests.append(request)
        return self._attempt


def _attempt(reference: float = 107.0, *, bars: int = 4, **kwargs) -> ForecastAttempt:
    """Three paths over a 4-bar horizon (the fixture horizon), one per direction mix."""

    def path(step: float, direction: str) -> PathSummary:
        closes = tuple(reference + step * (index + 1) for index in range(bars))
        highs = tuple(value + 1.0 for value in closes)
        lows = tuple(value - 1.0 for value in closes)
        return PathSummary(
            end_close=closes[-1],
            path_low=min(lows),
            path_high=max(highs),
            direction=direction,
            closes=closes, highs=highs, lows=lows,
        )

    paths = (path(0.5, "up"), path(-0.5, "down"), path(0.25, "up"))
    return ForecastAttempt(
        status=STATUS_OK, paths=paths, direction="up",
        samples={
            "sample_count": 3, "per_path_sample_count": 1,
            "direction_ratio": {"up": 2 / 3, "down": 1 / 3, "flat": 0.0},
            "band": {"low": reference - 2.0, "high": reference + 3.0},
            "dispersion": {"stdev": 1.0, "spread_ratio": 0.01},
        },
        duration_ms=12.0, params={"pred_len": bars, **kwargs},
    )


def _request(candles=None, *, horizon: int = 4, seed: int = 7) -> ForecastRequest:
    series = tuple(candles if candles is not None else _series())
    return ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=horizon,
        candles=series, as_of=_as_of(len(series) - 1).isoformat(), seed=seed,
    )


def _observation(attempt: ForecastAttempt, *, provider=None, request=None,
                 prediction_created_at: datetime | None = None,
                 decision_moment: datetime | None = None,
                 execution_status: str | None = "executed") -> dict:
    request = request or _request()
    return build_observation(
        provider=provider or _StubModel(attempt),
        request=request,
        attempt=attempt,
        prediction_created_at=prediction_created_at or _as_of(len(request.candles) - 1),
        decision_moment=decision_moment or _as_of(len(request.candles) - 1),
        cycle_id="futures:BTC/USDT:USDT:t",
        execution_status=execution_status,
        provenance={"cycle_id": "futures:BTC/USDT:USDT:t"},
    )


# --- closed candles only -----------------------------------------------------


def test_forming_candle_is_refused():
    series = _series()
    series[-1] = _candle(len(series) - 1, is_closed=False)
    with pytest.raises(RequestError, match="forming candle"):
        validate_candles(series, "1h", _as_of(len(series) - 1))


def test_candle_closing_after_as_of_is_refused():
    series = _series()
    with pytest.raises(RequestError, match="may not look ahead"):
        validate_candles(series, "1h", _as_of(len(series) - 2))


def test_mixed_timeframes_are_refused():
    series = _series(4) + [_candle(4, timeframe="4h")]
    with pytest.raises(RequestError, match="mixed timeframes"):
        validate_candles(series, "1h", _as_of(10))


def test_gap_in_the_series_is_refused():
    series = _series(6)
    del series[3]
    with pytest.raises(RequestError, match="gap"):
        validate_candles(series, "1h", _as_of(6))


def test_duplicate_candle_is_refused():
    series = _series(6)
    series.insert(3, series[2])
    with pytest.raises(RequestError, match="duplicate or overlapping"):
        validate_candles(series, "1h", _as_of(6))


def test_valid_series_is_returned_sorted_and_closed():
    series = list(reversed(_series(6)))
    checked = validate_candles(series, "1h", _as_of(6))
    assert [candle.open_time for candle in checked] == sorted(
        candle.open_time for candle in series
    )
    assert all(candle.is_closed for candle in checked)


# --- honesty of the stored row ----------------------------------------------


def test_model_observation_carries_the_sample_ratio_label():
    row = _observation(_attempt())
    assert row["calibration_label"] == CALIBRATION_SAMPLE_RATIO
    assert "not a calibrated probability" in row["calibration_note"]
    assert row["distribution_kind"] == DISTRIBUTION_MODEL
    assert row["sample_count"] == 3
    assert row["per_path_sample_count"] == 1
    # The denominator is stated next to the ratio, so "2 of 3" cannot be read
    # as a probability.
    assert row["samples"]["direction_ratio"]["up"] == pytest.approx(2 / 3)
    assert row["samples"]["sample_count"] == 3


def test_forward_versus_retrospective_is_decided_by_the_first_realized_bar():
    """A worker runs after the decision; what makes it forward is that no
    predicted bar had closed yet when the observation was created."""
    decision = _as_of(7)
    # The series ends with candle 7 (closing at START+8h). The first PREDICTED bar
    # therefore closes at START+9h, and that is the last moment at which nothing
    # about the outcome is known.
    last_close = datetime.fromtimestamp((START_MS + 8 * HOUR_MS) / 1000, UTC)
    first_realized_close = last_close + timedelta(hours=1)
    at_cutoff = _observation(
        _attempt(), prediction_created_at=first_realized_close, decision_moment=decision,
    )
    twenty_minutes_earlier = _observation(
        _attempt(), prediction_created_at=first_realized_close - timedelta(minutes=20),
        decision_moment=decision,
    )
    two_hours_later = _observation(
        _attempt(), prediction_created_at=first_realized_close + timedelta(hours=2),
        decision_moment=decision,
    )
    assert at_cutoff["generation_kind"] == GENERATION_FORWARD
    # Still forward: the first predicted bar has not closed, so nothing about the
    # outcome was knowable yet — a worker observing a cycle minutes after it
    # finished is not a replay.
    assert twenty_minutes_earlier["generation_kind"] == GENERATION_FORWARD
    assert two_hours_later["generation_kind"] == GENERATION_RETROSPECTIVE
    assert "already known" in two_hours_later["provenance"]["generation_kind_basis"]
    assert two_hours_later["provenance"]["first_realized_close"] == (
        first_realized_close.isoformat()
    )


def test_failed_attempt_stores_its_status_and_no_paths():
    row = _observation(ForecastAttempt(status=STATUS_UNAVAILABLE, reason="no interpreter"))
    assert row["status"] == STATUS_UNAVAILABLE
    assert row["reason"] == "no interpreter"
    assert row["paths"] == []
    assert row["sample_count"] == 0
    assert row["direction"] is None
    assert row["samples"] == {}


def test_timeout_is_its_own_status_not_a_generic_error():
    row = _observation(ForecastAttempt(status=STATUS_TIMEOUT, reason="deadline"))
    assert row["status"] == STATUS_TIMEOUT
    assert row["paths"] == []


def test_deterministic_rule_row_has_no_band_or_dispersion():
    provider = FilterProvider("donchian_breakout")
    request = _request()
    attempt = provider.forecast(request)
    row = build_observation(
        provider=provider, request=request, attempt=attempt,
        prediction_created_at=_as_of(7), decision_moment=_as_of(7),
        cycle_id="c", execution_status="executed",
    )
    assert row["distribution_kind"] == DISTRIBUTION_RULE
    assert row["calibration_label"] == CALIBRATION_RULE
    samples = row["samples"]
    assert "band" not in samples
    assert "dispersion" not in samples
    assert "direction_ratio" not in samples
    # Its own diagnostics stay under a key that cannot be mistaken for a ratio.
    assert set(samples) == {"rule"}
    assert row["paths"] == []
    assert row["sample_count"] == 0


def test_outcome_groups_keep_executed_rejected_and_abstained_apart():
    assert outcome_group_for("executed")[0] == OUTCOME_EXECUTED
    assert outcome_group_for("risk_rejected")[0] == OUTCOME_REJECTED
    assert outcome_group_for("awaiting_approval")[0] == OUTCOME_REJECTED
    assert outcome_group_for("not_executed")[0] == OUTCOME_REJECTED
    assert outcome_group_for(None)[0] == OUTCOME_ABSTAINED
    assert outcome_group_for("market_rejected")[0] == OUTCOME_ABSTAINED
    # An unknown status is never promoted to "executed".
    group, reason = outcome_group_for("something_new")
    assert group == OUTCOME_ABSTAINED
    assert "something_new" in reason


def test_observation_records_the_cutoff_and_the_reference_close():
    request = _request()
    row = _observation(_attempt(), request=request)
    assert row["provenance"]["reference_close"] == pytest.approx(request.reference_close)
    assert row["provenance"]["candles"] == len(request.candles)
    cutoff = datetime.fromisoformat(row["data_open_time"])
    last = request.candles[-1]
    assert int(cutoff.timestamp() * 1000) == last.open_time
    assert row["provenance"]["last_close_time"] == datetime.fromtimestamp(
        last.close_time / 1000, UTC
    ).isoformat()


def test_missing_cycle_provenance_is_recorded_as_missing_not_invented():
    request = _request()
    row = build_observation(
        provider=_StubModel(_attempt()), request=request, attempt=_attempt(),
        prediction_created_at=_as_of(7), decision_moment=_as_of(7),
        cycle_id="c", execution_status="executed",
        provenance={"git_sha": None, "git_sha_note": "the cycle row recorded no git revision"},
    )
    assert row["provenance"]["git_sha"] is None
    assert "recorded no git revision" in row["provenance"]["git_sha_note"]


# --- baseline filters --------------------------------------------------------


def test_filter_providers_share_the_model_contract():
    for name in ("donchian_breakout", "htf_direction_volume", "momentum_zscore"):
        provider = FilterProvider(name)
        assert provider.provider_id == f"filter:{name}"
        attempt = provider.forecast(_request())
        assert attempt.status in (STATUS_OK, STATUS_ERROR)
        assert attempt.direction in (None, "up", "down", "flat")


def test_unknown_filter_name_is_refused():
    with pytest.raises(ValueError, match="unknown shadow filter"):
        FilterProvider("not_a_filter")


def test_donchian_breakout_reads_a_level_that_existed_before_the_candle():
    series = _series(6, start=100.0)
    series[-1] = _candle(5, close=200.0)
    attempt = FilterProvider("donchian_breakout").forecast(
        ForecastRequest(
            symbol=SYMBOL, timeframe="1h", horizon_hours=4, candles=tuple(series),
            as_of=_as_of(5).isoformat(), seed=1,
        )
    )
    assert attempt.direction == "up"
    # The channel excludes the newest candle: the upper bound is candle 4's high.
    assert attempt.samples["upper"] == pytest.approx(series[-2].high)


def test_filter_refuses_a_series_it_cannot_read():
    attempt = FilterProvider("donchian_breakout").forecast(
        ForecastRequest(
            symbol=SYMBOL, timeframe="1h", horizon_hours=4,
            candles=(_candle(0),), as_of=_as_of(0).isoformat(), seed=1,
        )
    )
    assert attempt.status == STATUS_ERROR
    assert "at least" in attempt.reason


# --- evaluation -------------------------------------------------------------


def _realized_after(cutoff_index: int, count: int = 4, *, close_fn=None) -> list[Candle]:
    """Closed candles strictly after ``cutoff_index``, contiguous."""
    return [
        _candle(cutoff_index + 1 + offset,
                close=(close_fn(offset) if close_fn else 110.0 + offset))
        for offset in range(count)
    ]


def test_evaluation_is_unavailable_until_the_horizon_realizes():
    row = _observation(_attempt())
    candles = _realized_after(7, count=2)  # only 2 of 4 hours
    evaluation = evaluate_forecast(row, candles)
    assert evaluation["status"] == "unavailable"
    assert "not fully realized" in evaluation["reason"]
    assert evaluation["realized_direction_hit"] is None


def test_evaluation_scores_direction_band_and_stop_first():
    row = _observation(_attempt())
    # The fixture's band is [reference-2, reference+3] = [105, 110]; coverage asks
    # where the horizon ENDED, so the last close is what decides it.
    inside = _realized_after(7, count=4, close_fn=lambda offset: 106.0 + offset)
    evaluation = evaluate_forecast(row, inside, stop=104.0, target=120.0, side="long")
    assert evaluation["status"] == "ok"
    assert evaluation["realized_direction"] == "up"
    assert evaluation["realized_direction_hit"] is True
    assert evaluation["band_coverage"] is True
    # stop_first_rate is about the MODEL's own paths, not the realized one and not
    # the realized levels: of the three sampled paths only the falling one
    # (105.5 .. 104) reaches a level. It reaches the 104 STOP, so the rate is 1/1
    # over the paths that resolved the question — a path that touches neither
    # level is excluded from the denominator (it is not "target first" evidence).
    assert evaluation["stop_first_rate"] == pytest.approx(1.0)
    assert evaluation["realized"]["stop_first_measured_paths"] == 1
    assert evaluation["realized"]["band_available"] is True

    outside = _realized_after(7, count=4, close_fn=lambda offset: 112.0 + offset)
    assert evaluate_forecast(row, outside)["band_coverage"] is False


def test_stop_first_rate_counts_the_stop_when_a_path_hits_both():
    # Paths whose own bars straddle both levels without opening past either:
    # worst-first (the same policy the backtest uses) counts the stop, and the
    # ambiguity is published instead of resolved silently.
    wide = PathSummary(
        end_close=110.0, path_low=100.0, path_high=125.0, direction="up",
        closes=(110.0, 110.0, 110.0, 110.0),
        highs=(125.0, 125.0, 125.0, 125.0),
        lows=(100.0, 100.0, 100.0, 100.0),
    )
    row = _observation(ForecastAttempt(
        status=STATUS_OK, paths=(wide,), direction="up",
        samples={"sample_count": 1, "band": {"low": 100.0, "high": 125.0}},
    ))
    candles = _realized_after(7, count=4, close_fn=lambda offset: 110.0)
    evaluation = evaluate_forecast(row, candles, stop=104.0, target=120.0, side="long")
    assert evaluation["status"] == "ok"
    assert evaluation["stop_first_rate"] == pytest.approx(1.0)
    assert evaluation["realized"]["stop_first_ambiguity_count"] > 0


def _path(highs: tuple[float, ...], lows: tuple[float, ...],
          closes: tuple[float, ...]) -> dict:
    return {"highs": list(highs), "lows": list(lows), "closes": list(closes)}


def test_stop_first_denominator_excludes_paths_that_touch_neither_level():
    """F3: a path that never reached a level is not "target first" evidence."""
    never_touches = _path((101.0, 101.0, 101.0, 101.0),
                          (99.5, 99.5, 99.5, 99.5),
                          (100.0, 100.0, 100.0, 100.0))
    hits_stop = _path((101.0, 101.0, 101.0, 101.0),
                      (95.0, 95.0, 95.0, 95.0),
                      (100.0, 96.0, 96.0, 96.0))
    row = _observation(ForecastAttempt(
        status=STATUS_OK, paths=(), direction="up",
        samples={"sample_count": 2, "band": {"low": 100.0, "high": 101.0}},
    ))
    row["paths"] = [never_touches, hits_stop]
    evaluation = evaluate_forecast(
        row, _realized_after(7, count=4, close_fn=lambda offset: 100.0),
        stop=96.0, target=105.0, side="long",
    )
    # One path resolved the question and it hit the stop: 1/1, not 1/2.
    assert evaluation["stop_first_rate"] == pytest.approx(1.0)
    assert evaluation["realized"]["stop_first_measured_paths"] == 1
    assert "touched neither" in evaluation["realized"]["stop_first_semantics"]


def test_stop_first_is_unknown_when_no_path_resolved_the_question():
    """F3b: zero measured paths must be unknown, not a 0.0 rate."""
    never = _path((101.0, 101.0, 101.0, 101.0), (99.5, 99.5, 99.5, 99.5),
                  (100.0, 100.0, 100.0, 100.0))
    row = _observation(_attempt())
    row["paths"] = [never, never]
    evaluation = evaluate_forecast(
        row, _realized_after(7, count=4, close_fn=lambda offset: 100.0),
        stop=96.0, target=105.0, side="long",
    )
    assert evaluation["stop_first_rate"] is None
    assert evaluation["realized"]["stop_first_measured_paths"] == 0


def test_band_coverage_asks_about_the_endpoint_not_every_bar():
    """F2: the band is the p10..p90 of END closes, so coverage is end-point."""
    row = _observation(_attempt())
    # A wide excursion mid-path that comes back inside the band by the close.
    excursion = [
        _candle(8, close=120.0),
        _candle(9, close=106.0),
        _candle(10, close=107.0),
        _candle(11, close=107.5),
    ]
    evaluation = evaluate_forecast(row, excursion, stop=90.0, target=140.0, side="long")
    assert evaluation["status"] == "ok"
    # The forecast's band is [reference-2, reference+3] = [105, 110] and the
    # horizon ended at 107.5, inside it.
    assert evaluation["band_coverage"] is True
    # The path question is answered separately and does not pretend otherwise.
    assert evaluation["realized"]["band_held_throughout"] is False
    assert "END-OF-HORIZON" in evaluation["realized"]["band_semantics"]


def test_stop_first_is_unknown_rather_than_zero_without_paths():
    """A rule observation has no sampled path, so the rate stays unknown."""
    provider = FilterProvider("donchian_breakout")
    request = _request()
    row = build_observation(
        provider=provider, request=request, attempt=provider.forecast(request),
        prediction_created_at=_as_of(7), decision_moment=_as_of(7),
        cycle_id="c", execution_status="executed",
    )
    evaluation = evaluate_forecast(row, _realized_after(7), stop=104.0, target=120.0, side="long")
    assert evaluation["stop_first_rate"] is None


def test_evaluation_reports_a_miss_and_a_band_break():
    row = _observation(_attempt())
    candles = _realized_after(7, count=4, close_fn=lambda offset: 90.0 - offset)
    evaluation = evaluate_forecast(row, candles, stop=105.0, target=120.0, side="long")
    assert evaluation["realized_direction"] == "down"
    assert evaluation["realized_direction_hit"] is False
    assert evaluation["band_coverage"] is False


def test_rule_observation_never_claims_band_coverage():
    provider = FilterProvider("donchian_breakout")
    request = _request()
    row = build_observation(
        provider=provider, request=request, attempt=provider.forecast(request),
        prediction_created_at=_as_of(7), decision_moment=_as_of(7),
        cycle_id="c", execution_status="executed",
    )
    evaluation = evaluate_forecast(row, _realized_after(7), stop=90.0, target=200.0, side="long")
    assert evaluation["band_coverage"] is None
    assert "no percentile band" in evaluation["realized"]["band_reason"]


def test_failed_observation_is_not_evaluated():
    row = _observation(ForecastAttempt(status=STATUS_UNAVAILABLE, reason="absent"))
    evaluation = evaluate_forecast(row, _realized_after(7))
    assert evaluation["status"] == "unavailable"
    assert "no forecast to evaluate" in evaluation["reason"]


def test_net_r_is_passed_through_only_when_the_ledger_recorded_it():
    row = _observation(_attempt())
    unknown = evaluate_forecast(row, _realized_after(7))
    assert unknown["net_r"] is None
    assert "did not record" in unknown["net_r_reason"]
    known = evaluate_forecast(row, _realized_after(7), net_r=1.25)
    assert known["net_r"] == pytest.approx(1.25)
    assert "recorded a net R" in known["net_r_reason"]


def test_realized_window_requires_the_exact_cutoff_and_no_gaps():
    candles = _realized_after(7, count=4)
    window = realized_candles(
        candles, timeframe="1h", after_open_time_ms=candles[0].open_time,
        horizon_hours=4,
    )
    assert len(window) == 4
    # Starting one bar late means the realized path cannot be attributed.
    assert realized_candles(
        candles, timeframe="1h", after_open_time_ms=candles[0].open_time + HOUR_MS,
        horizon_hours=4,
    ) == ()
    gapped = list(candles)
    del gapped[2]
    assert realized_candles(
        gapped, timeframe="1h", after_open_time_ms=candles[0].open_time, horizon_hours=4,
    ) == ()


# --- record honesty ---------------------------------------------------------


def test_unknown_calibration_label_gets_a_note_instead_of_a_key_error():
    """F6: a provider is a plugin; its label must not crash row building."""
    class _OddProvider(_StubModel):
        calibration_label = "vendor_specific_label"

    row = build_observation(
        provider=_OddProvider(ForecastAttempt(status=STATUS_OK, direction="up")),
        request=_request(),
        attempt=ForecastAttempt(status=STATUS_OK, direction="up"),
        prediction_created_at=_as_of(7), decision_moment=_as_of(7),
        cycle_id="c", execution_status="executed",
    )
    assert row["calibration_label"] == "vendor_specific_label"
    assert "does not define" in row["calibration_note"]
    assert "uncalibrated" in row["calibration_note"]


def test_path_summary_names_its_extremes_honestly():
    """F8: the extremes are the path's low/high, not its lowest/highest close."""
    path = PathSummary(
        end_close=100.0, path_low=90.0, path_high=115.0, direction="flat",
        closes=(100.0, 100.0), highs=(115.0, 101.0), lows=(90.0, 99.0),
    )
    payload = path.as_json()
    assert payload["path_low"] == 90.0
    assert payload["path_high"] == 115.0
    assert "min_close" not in payload
    assert "max_close" not in payload


# --- persistence (PostgreSQL) -----------------------------------------------


def test_persistence_is_idempotent_and_keeps_the_label(postgres_schema):
    from server import shadow_store

    row = _observation(_attempt())
    assert shadow_store.record_forecast(row) is True
    assert shadow_store.record_forecast({**row, "forecast_id": "00000000-0000-0000-0000-000000000001"}) is False

    stored = shadow_store.forecast_rows(limit=5)
    assert len(stored) == 1
    assert stored[0]["calibration_label"] == CALIBRATION_SAMPLE_RATIO
    assert stored[0]["sample_count"] == 3
    samples = json.loads(stored[0]["samples"]) if isinstance(stored[0]["samples"], str) else stored[0]["samples"]
    assert samples["sample_count"] == 3
    assert stored[0]["outcome_group"] == OUTCOME_EXECUTED


def test_store_refuses_a_non_positive_limit(postgres_schema):
    """F5: a bad page size is the caller's error, not a 500 from PostgreSQL."""
    from server import shadow_store

    assert shadow_store.record_forecast(_observation(_attempt()))
    with pytest.raises(ValueError, match="limit must be positive"):
        shadow_store.forecast_rows(limit=0)
    with pytest.raises(ValueError, match="limit must be positive"):
        shadow_store.forecast_rows(limit=-1)
    with pytest.raises(ValueError, match="limit must be positive"):
        shadow_store.summary(limit=0)


def test_summary_is_unavailable_until_something_is_observed(postgres_schema):
    from server import shadow_store

    summary = shadow_store.summary()
    assert summary["status"] == "unavailable"
    assert summary["totals"] is None
    assert summary["providers"] == []


def test_summary_counts_statuses_rather_than_claiming_rates(postgres_schema):
    from server import shadow_store

    assert shadow_store.record_forecast(_observation(_attempt()))
    # A second observation needs its own data_open_time: the uniqueness key is
    # (provider, symbol, cutoff, horizon, revision), so a later cutoff is a
    # different observation rather than a duplicate of the first.
    later_candles = tuple(
        _candle(index, close=200.0 + index, open_time=START_MS + 20 * HOUR_MS + index * HOUR_MS)
        for index in range(8)
    )
    assert shadow_store.record_forecast(_observation(
        ForecastAttempt(status=STATUS_ERROR, reason="boom"),
        request=ForecastRequest(
            symbol=SYMBOL, timeframe="1h", horizon_hours=4, candles=later_candles,
            as_of="2023-11-15T06:00:00+00:00", seed=7,
        ),
    ))
    summary = shadow_store.summary()
    assert summary["status"] == "ok"
    assert summary["totals"]["rows"] == 2
    assert summary["totals"]["ok"] == 1
    assert summary["totals"]["errors"] == 1
    by_provider = {row["provider"]: row for row in summary["providers"]}
    # Counts, never a rate: one attempt each, one ok and one failed, and the
    # single successful row contributes exactly one verdict (its own direction).
    assert by_provider["kronos"]["attempts"] == {
        "rows": 2, "ok": 1, "unavailable": 0, "error": 1, "timeout": 0,
    }
    assert by_provider["kronos"]["verdicts"] == {"up": 1, "down": 0, "flat": 0}


def test_evaluation_persists_once_per_forecast(postgres_schema):
    from server import shadow_store

    row = _observation(_attempt())
    assert shadow_store.record_forecast(row)
    stored = shadow_store.forecast_rows(limit=1)[0]
    evaluation = evaluate_forecast(stored, _realized_after(7))
    assert shadow_store.record_evaluation(stored["forecast_id"], evaluation) is True
    assert shadow_store.record_evaluation(stored["forecast_id"], evaluation) is False
    after = shadow_store.forecast_rows(limit=1)[0]
    assert after["evaluation_status"] == "ok"
    assert after["realized_direction"] == "up"
    assert shadow_store.forecasts_awaiting_evaluation() == []
