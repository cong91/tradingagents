"""PostgreSQL persistence contract tests for forecast evaluations."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from server import forecast_evaluation_store, futures_cycle_store, futures_cycle_telemetry

pytestmark = pytest.mark.unit


def _forecast_block() -> dict:
    now = datetime.now(UTC)
    return {
        "status": "ok", "generation_kind": "forward", "timeframe": "1h",
        "data_open_time": int((now - timedelta(hours=2)).timestamp() * 1000),
        "horizon_bars": 1, "horizon_hours": 1,
        "as_of": (now - timedelta(hours=1)).isoformat(),
        "created_at": (now - timedelta(minutes=59)).isoformat(),
        "recorded_at": now.isoformat(),
        "reference_close": 100.0, "direction": "up",
        "direction_ratio": {"up": 0.8},
        "band": {"low": 98.0, "p50": 102.0, "high": 104.0},
    }


def _row(*, cycle_id: str, status: str = "pending") -> dict:
    now = datetime.now(UTC)
    return {
        "cycle_id": cycle_id,
        "symbol": "BTC/USDT:USDT",
        "forecast_digest": "a" * 64,
        "horizon_key": "1h|1800000000000|1",
        "forecast_snapshot": _forecast_block(),
        "venue_scope": {"exchange_id": "binance", "venue_profile": "demo"},
        "status": status,
        "forecast_as_of": now - timedelta(hours=1),
        "forecast_created_at": now - timedelta(minutes=59),
        "forecast_recorded_at": now,
        "target_as_of": now + timedelta(hours=1) if status == "pending" else now,
        "timeframe": "1h",
        "profile": "small_base",
        "effective_lookback_bars": 32,
        "horizon_bars": 1,
        "horizon_hours": 1,
        "generation_kind": "forward",
        "direction": "up",
        "direction_ratio": {"up": 0.8},
        "band": {"low": 98, "p50": 102, "high": 104},
        "reference_close": 100,
        "predicted_close": 102,
        "reason": "waiting" if status == "pending" else "read failed",
        "warnings": [],
        "provenance": {},
    }


def _record_cycle(cycle_id: str, payload: dict, *, created_at: datetime, terminal: bool = True):
    four_open = created_at.isoformat()
    one_open = (created_at + timedelta(minutes=1)).isoformat()
    selected_open = int(created_at.timestamp() * 1000)
    canonical_id = futures_cycle_telemetry.canonical_cycle_id(
        symbol="BTC/USDT:USDT", four_hour_open=four_open, one_hour_open=one_open,
        timeframe="1h", open_time=selected_open,
    )
    assert futures_cycle_store.claim_futures_cycle(
        canonical_id,
        symbol="BTC/USDT:USDT",
        four_hour_open=four_open,
        one_hour_open=one_open,
        strategy_version="1",
        timeframe="1h",
        selected_open=selected_open,
        payload=payload,
    )
    if terminal:
        futures_cycle_store.update_futures_cycle(
            canonical_id, status="rejected", phase="forecast", terminal=True,
        )
    return canonical_id


def _insert_cycle(dsn, name: str, *, updated_at: str, payload: dict) -> str:
    symbol = f"{name}/USDT:USDT"
    opened = "2026-01-01T00:00:00+00:00"
    selected_open = 1_767_225_600_000
    cycle_id = futures_cycle_telemetry.canonical_cycle_id(
        symbol=symbol, four_hour_open=opened, one_hour_open=opened,
        timeframe="1h", open_time=selected_open,
    )
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """INSERT INTO futures_cycles
               (cycle_id, symbol, four_hour_open, one_hour_open, strategy_version,
                timeframe, selected_open, status, phase, payload, created_at, updated_at)
               VALUES (%s, %s, %s, %s, '1', '1h', %s, 'rejected', 'forecast', %s, %s, %s)""",
            (cycle_id, symbol, opened, opened, selected_open, Jsonb(payload), updated_at, updated_at),
        )
    return cycle_id


def test_store_upsert_is_idempotent_and_evaluated_does_not_regress(postgres_schema):
    evaluation = _row(cycle_id="cycle-eval-store")
    evaluation["evaluation_id"] = "0190f0d0-6cd1-7b90-bf1a-4e45a9c9fc11"
    forecast_evaluation_store.upsert_evaluation(evaluation)
    forecast_evaluation_store.upsert_evaluation({
        **evaluation, "status": "evaluated", "target_close": 105.0,
        "absolute_error": 3.0, "absolute_error_pct": 3 / 102 * 100,
        "directional_correct": True, "band_contains_target": False,
        "reason": None, "evaluated_at": datetime.now(UTC),
    })
    forecast_evaluation_store.upsert_evaluation({
        **evaluation, "status": "unavailable", "reason": "late retry",
    })
    rows = forecast_evaluation_store.list_evaluations(limit=10)
    assert len(rows) == 1
    assert str(rows[0]["evaluation_id"]) == evaluation["evaluation_id"]
    assert rows[0]["status"] == "evaluated"
    assert rows[0]["target_close"] == 105.0
    assert rows[0]["forecast_snapshot"]["direction"] == "up"


def test_cursor_initializes_at_enable_time_without_backfill(postgres_schema):
    old = datetime.now(UTC) - timedelta(days=1)
    _record_cycle(
        "cycle-before-evaluation-enable",
        {"kronos_forecast": _forecast_block()},
        created_at=old,
    )
    assert forecast_evaluation_store.initialize_cursor() is True
    cursor = forecast_evaluation_store.get_cursor()
    assert cursor is not None
    assert forecast_evaluation_store.initialize_cursor() is False
    assert forecast_evaluation_store.terminal_forecast_cycles_after(cursor) == []
    assert (
        "forecast timestamps are self-reported and do not independently prove pre-close persistence"
        in forecast_evaluation_store.summary()["coverage_warnings"]
    )


def test_cursor_scan_does_not_admit_pre_enable_forecast_finalized_after_cutover(postgres_schema):
    enable_time = datetime.now(UTC)
    assert forecast_evaluation_store.initialize_cursor()
    cursor = forecast_evaluation_store.get_cursor()
    assert cursor is not None
    old_forecast = _forecast_block()
    old_forecast["as_of"] = (enable_time - timedelta(hours=2)).isoformat()
    old_forecast["created_at"] = (enable_time - timedelta(hours=2)).isoformat()
    old_forecast["recorded_at"] = (enable_time - timedelta(hours=2)).isoformat()
    _record_cycle(
        "cycle-old-forecast-new-terminal",
        {"kronos_forecast": old_forecast},
        created_at=enable_time - timedelta(hours=1),
    )
    assert forecast_evaluation_store.terminal_forecast_cycles_after(cursor) == []


@pytest.mark.parametrize("batch_size", [1, 2])
def test_cursor_paginates_by_timestamp_then_cycle_id(postgres_schema, batch_size):
    cutover = "2026-11-16T00:00:00+00:00"
    stamps = {
        "EARLY": "2026-11-15T20:00:00.900000-04:00",
        "OFFSET": "2026-11-16T01:00:01+01:00",
        "UTC": "2026-11-16T00:00:01Z",
        "ROUND_DOWN": "2026-11-16T00:00:01.0000004Z",
        "ROUND_UP": "2026-11-16T00:00:00.9999999Z",
        "LATE": "2026-11-16T00:00:02+00:00",
    }
    for name, stamp in stamps.items():
        _insert_cycle(
            postgres_schema, name, updated_at=stamp,
            payload={"kronos_forecast": {"recorded_at": "2026-11-16T00:00:00.500000Z"}},
        )
    with psycopg.connect(postgres_schema) as conn:
        expected = [row[0] for row in conn.execute(
            "SELECT cycle_id FROM futures_cycles ORDER BY updated_at::timestamptz, cycle_id"
        ).fetchall()]

    assert forecast_evaluation_store.initialize_cursor()
    previous = forecast_evaluation_store.get_cursor()
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(
            "UPDATE forecast_evaluation_state SET cursor_value = %s WHERE state_key = %s",
            (f"{cutover}|{cutover}|", forecast_evaluation_store.STATE_KEY),
        )
    cursor = forecast_evaluation_store.get_cursor()
    assert previous is not None and cursor is not None
    seen = []
    for _ in range(len(stamps) + 1):
        page = forecast_evaluation_store.terminal_forecast_cycles_after(cursor, batch_size=batch_size)
        if not page:
            break
        seen.extend(row["cycle_id"] for row in page)
        assert all(row["cycle_timeframe"] == "1h" for row in page)
        assert all(row["cycle_selected_open"] == 1_767_225_600_000 for row in page)
        for row in page:
            forecast_evaluation_store.upsert_evaluation(_row(cycle_id=row["cycle_id"]), expected_cursor=cursor)
        last = page[-1]
        assert forecast_evaluation_store.advance_cursor(cursor, last["updated_at"], last["cycle_id"])
        cursor = forecast_evaluation_store.get_cursor()
    assert seen == expected
    assert len(seen) == len(set(seen))


def test_discovery_recovers_cycle_that_commits_after_cursor_advanced(postgres_schema):
    cutover = "2026-11-16T00:00:00+00:00"
    payload = {"kronos_forecast": {"recorded_at": "2026-11-16T00:00:01Z"}}
    early = _insert_cycle(
        postgres_schema, "EARLY", updated_at="2026-11-16T00:00:02Z", payload=payload,
    )
    with psycopg.connect(postgres_schema) as conn:
        conn.execute("UPDATE futures_cycles SET status = 'running' WHERE cycle_id = %s", (early,))
    later = _insert_cycle(
        postgres_schema, "LATER", updated_at="2026-11-16T00:00:04Z", payload=payload,
    )
    assert forecast_evaluation_store.initialize_cursor()
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(
            "UPDATE forecast_evaluation_state SET cursor_value = %s WHERE state_key = %s",
            (f"{cutover}|{cutover}|", forecast_evaluation_store.STATE_KEY),
        )
    cursor = forecast_evaluation_store.get_cursor()
    with psycopg.connect(postgres_schema) as delayed:
        delayed.execute(
            "UPDATE futures_cycles SET status = 'rejected' WHERE cycle_id = %s", (early,),
        )
        [visible] = forecast_evaluation_store.terminal_forecast_cycles_after(cursor, batch_size=1)
        assert visible["cycle_id"] == later
        forecast_evaluation_store.upsert_evaluation(_row(cycle_id=later), expected_cursor=cursor)
        assert forecast_evaluation_store.advance_cursor(cursor, visible["updated_at"], later)
        cursor = forecast_evaluation_store.get_cursor()
        delayed.commit()
    [recovered] = forecast_evaluation_store.terminal_forecast_cycles_after(cursor, batch_size=1)
    assert recovered["cycle_id"] == early


def test_scan_cutover_matches_python_microseconds_and_explicit_offsets(postgres_schema):
    cutover = "2026-11-16T00:00:00.123457+00:00"
    samples = {
        "BEFORE": "2026-11-16T00:00:00.1234569Z",
        "BEFORE_OFFSET": "2026-11-16T01:00:00.1234569+01:00",
        "EXACT": "2026-11-16T00:00:00.123457Z",
        "AFTER": "2026-11-16T00:00:00.1234579Z",
        "AFTER_OFFSET": "2026-11-16T01:00:00.1234579+01:00",
    }
    ids = {
        name: _insert_cycle(
            postgres_schema, name, updated_at="2026-11-16T00:00:10Z",
            payload={"kronos_forecast": {"recorded_at": recorded}},
        )
        for name, recorded in samples.items()
    }
    cursor = f"{cutover}|{cutover}|"
    rows = forecast_evaluation_store.terminal_forecast_cycles_after(cursor)
    assert {row["cycle_id"] for row in rows} == {
        ids["EXACT"], ids["AFTER"], ids["AFTER_OFFSET"],
    }

    # The scorer truncates to six digits; a retry at the same cutover must
    # retain exactly the samples admitted by the initial scan.
    for row in rows:
        recorded = datetime.fromisoformat(row["payload"]["kronos_forecast"]["recorded_at"].replace("Z", "+00:00"))
        forecast_evaluation_store.upsert_evaluation({
            **_row(cycle_id=row["cycle_id"], status="unavailable"),
            "forecast_recorded_at": recorded,
            "next_attempt_at": datetime.now(UTC) - timedelta(seconds=1),
        })
    due = forecast_evaluation_store.pending_evaluations_due()
    assert {row["cycle_id"] for row in due} == {row["cycle_id"] for row in rows}


def test_unproven_recording_times_are_excluded_and_reported(postgres_schema):
    assert forecast_evaluation_store.initialize_cursor()
    cursor = forecast_evaluation_store.get_cursor()
    assert cursor is not None
    cutover = forecast_evaluation_store.cursor_cutover(cursor)
    later = (datetime.fromisoformat(cutover) + timedelta(seconds=1)).isoformat()
    snapshots = {
        "MISSING": {},
        "INVALID_DAY": {"recorded_at": "2026-02-30T00:00:00Z"},
        "NAIVE": {"recorded_at": "2026-11-16T00:00:00"},
        "INFINITE": {"recorded_at": "infinity"},
        "OFFSET_RANGE": {"recorded_at": "2026-11-16T00:00:00+16:00"},
        "UTC_OVERFLOW": {"recorded_at": "9999-12-31T23:59:59-01:00"},
        "UTC_UNDERFLOW": {"recorded_at": "0001-01-01T00:00:00+01:00"},
        "NOT_STRING": {"recorded_at": 123},
        "NONOBJECT": None,
    }
    for name, snapshot in snapshots.items():
        _insert_cycle(
            postgres_schema, name, updated_at=later,
            payload={"kronos_forecast": snapshot},
        )
    assert forecast_evaluation_store.terminal_forecast_cycles_after(cursor) == []
    forecast_evaluation_store.upsert_evaluation({
        **_row(cycle_id="evaluated"), "status": "evaluated", "directional_correct": True,
    })
    report = forecast_evaluation_store.summary()
    assert report["total_count"] == report["sample_count"] == 1
    assert report["directional_accuracy"] == 1.0
    assert report["status"] == "partial"
    assert report["coverage"]["unproven_recording_time_count"] == len(snapshots)
    assert any("unproven recording" in warning for warning in report["coverage_warnings"])
    filtered = forecast_evaluation_store.summary(symbol="MISSING/USDT:USDT")
    assert filtered["coverage"]["unproven_recording_time_count"] == 1
    assert filtered["sample_count"] == 0


def test_cursor_advance_is_compare_and_swap(postgres_schema):
    assert forecast_evaluation_store.initialize_cursor()
    original = forecast_evaluation_store.get_cursor()
    assert original is not None
    assert forecast_evaluation_store.initialize_cursor(reset=True)
    assert forecast_evaluation_store.advance_cursor(
        original, datetime.now(UTC).isoformat(), "cycle-old-poll",
    ) is False


def test_pending_retry_query_uses_immutable_snapshot(postgres_schema):
    now = datetime.now(UTC)
    cycle_id = _record_cycle(
        "cycle-pending-retry",
        {"kronos_forecast": _forecast_block(), "kronos_venue_scope": {"exchange_id": "binance"}},
        created_at=now,
    )
    retry_row = {
        **_row(cycle_id=cycle_id, status="unavailable"),
        "target_as_of": now - timedelta(seconds=1),
        "next_attempt_at": now - timedelta(seconds=1),
    }
    forecast_evaluation_store.upsert_evaluation(retry_row)
    with psycopg.connect(postgres_schema) as conn:
        conn.execute(
            "UPDATE futures_cycles SET payload = %s WHERE cycle_id = %s",
            (Jsonb({"kronos_forecast": {"timeframe": "4h"}}), cycle_id),
        )
    due = forecast_evaluation_store.pending_evaluations_due(limit=10)
    assert len(due) == 1
    assert due[0]["forecast_snapshot"] == retry_row["forecast_snapshot"]
    assert due[0]["venue_scope"]["exchange_id"] == "binance"
    assert due[0]["cycle_timeframe"] == "1h"
    assert due[0]["cycle_selected_open"] == int(now.timestamp() * 1000)


def test_summary_counts_and_items_share_one_snapshot(postgres_schema, monkeypatch):
    forecast_evaluation_store.upsert_evaluation({
        **_row(cycle_id="first"), "status": "evaluated", "directional_correct": True,
    })
    original_read = forecast_evaluation_store.db.read
    inserted = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, params=None):
            result = self.conn.execute(statement, params)
            if "AS total_count" in statement and not inserted:
                inserted.append(True)
                forecast_evaluation_store.upsert_evaluation(_row(cycle_id="concurrent"))
            return result

    @contextmanager
    def interleaved_read():
        with original_read() as conn:
            yield Connection(conn)

    monkeypatch.setattr(forecast_evaluation_store.db, "read", interleaved_read)
    report = forecast_evaluation_store.summary()
    assert inserted
    assert report["total_count"] == 1
    assert report["status"] == "complete"
    assert len(report["items"]) == 1
    assert report["items"][0]["cycle_id"] == "first"


def test_summary_distinguishes_nonretryable_failed_forecast(postgres_schema):
    from server import forecast_evaluation_scoring
    from tradingagents.forecast.block import ForecastBlock

    block = ForecastBlock(symbol="BTC/USDT:USDT", status="unavailable", reason="no history").as_json()
    row = forecast_evaluation_scoring.evaluate(
        {"cycle_id": "failed-forecast", "symbol": block["symbol"], "payload": {}},
        block, exchange=None,
    )
    row["forecast_snapshot"] = block
    forecast_evaluation_store.upsert_evaluation(row)
    report = forecast_evaluation_store.summary()
    assert report["counts"]["unavailable"] == 1
    assert report["coverage"]["nonretryable_unavailable_count"] == 1
    assert any("cannot be retried" in warning for warning in report["coverage_warnings"])
    assert not any("will retry" in warning for warning in report["coverage_warnings"])
    assert forecast_evaluation_store.pending_evaluations_due() == []


def test_retry_keeps_missing_cycle_identity_explicit(postgres_schema):
    now = datetime.now(UTC)
    forecast_evaluation_store.upsert_evaluation({
        **_row(cycle_id="missing-cycle", status="unavailable"),
        "next_attempt_at": now - timedelta(seconds=1),
    })
    [row] = forecast_evaluation_store.pending_evaluations_due()
    assert row["cycle_timeframe"] is None
    assert row["cycle_selected_open"] is None
    assert row["forecast_snapshot"]["timeframe"] == "1h"
