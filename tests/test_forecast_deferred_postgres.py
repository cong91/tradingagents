"""PostgreSQL coverage for forecast deferral accounting and cycle details."""

import json

import pytest

from server import autoloop_api, brief_activity, db, futures_cycle_store

pytestmark = pytest.mark.unit


def test_forecast_deferrals_are_pipeline_errors_and_have_disjoint_funnel_buckets(
    postgres_schema, monkeypatch,
):
    """Exercise real JSONB NULL semantics, mixed outcomes, and newest-cycle reasons."""
    rows = [
        ("running", "BTC", "running", "forecast", {"kronos_policy": "defer"}, 0),
        ("btc-old", "BTC", "rejected", "forecast", {
            "execution_status": "not_started", "kronos_policy": "defer",
            "kronos_defer_reason": "older forecast failure",
        }, 1),
        ("market", "BTC", "rejected", "market", {}, 2),
        ("proposal", "BTC", "rejected", "proposal", {
            "execution_status": "awaiting_approval",
        }, 3),
        ("legacy", "BTC", "completed", "entry", {}, 4),
        ("entered", "BTC", "completed", "entry", {
            "execution_status": "executed", "executed": True,
        }, 5),
        ("no-policy", "BTC", "rejected", "forecast", {}, 6),
        ("failed", "BTC", "interrupted", "execution", {
            "execution_status": "failed", "failure_reason": "venue failed",
        }, 7),
        ("btc-new", "BTC", "rejected", "forecast", {
            "execution_status": "not_started", "kronos_policy": "defer",
            "kronos_defer_reason": "latest forecast failure",
        }, 8),
        ("eth-old", "ETH", "rejected", "forecast", {
            "execution_status": "not_started", "kronos_policy": "defer",
            "kronos_defer_reason": "older ETH failure",
        }, 1),
        ("eth-new", "ETH", "rejected", "proposal", {
            "signal_reason": "agent_abstained",
        }, 2),
    ]
    with db.session() as conn:
        for cycle_id, symbol, status, phase, payload, second in rows:
            stamp = f"2026-11-12T00:00:{second:02d}+00:00"
            # Rows must satisfy the canonical identity CHECK: the id is built
            # from the same opens/timeframe the columns carry.
            stored_id = (
                f"futures:{symbol}:{cycle_id}:{cycle_id}:1:4h:{second}"
            )
            conn.execute(
                """INSERT INTO futures_cycles
                   (cycle_id, symbol, four_hour_open, one_hour_open, strategy_version,
                    timeframe, selected_open, status, phase, payload, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, '1', '4h', %s, %s, %s, %s::jsonb, %s, %s)""",
                (stored_id, symbol, cycle_id, cycle_id, second, status, phase,
                 json.dumps(payload), stamp, stamp),
            )

    summary = futures_cycle_store.futures_cycle_summary()
    assert summary == {
        "in_flight": 1, "forecast_deferred": 3, "market_rejected": 1,
        "proposal_rejected": 2, "other": 2, "entered": 1, "execution_failed": 1,
    }
    rollups = {row["symbol"]: row for row in futures_cycle_store.futures_cycle_symbol_rollup()}
    assert rollups["BTC"]["forecast_deferred"] == 2
    assert rollups["BTC"]["last_kronos_policy"] == "defer"
    assert rollups["BTC"]["last_kronos_defer_reason"] == "latest forecast failure"
    assert rollups["ETH"]["forecast_deferred"] == 1
    assert rollups["ETH"]["last_kronos_policy"] is None
    assert rollups["ETH"]["last_kronos_defer_reason"] is None
    buckets = set(autoloop_api._CYCLE_SUMMARY_BUCKETS)
    for row in rollups.values():
        assert sum(row[bucket] for bucket in buckets) == row["total"]
    for bucket in buckets:
        assert sum(row[bucket] for row in rollups.values()) == summary.get(bucket, 0)

    start = "2026-11-12T00:00:00+00:00"
    assert brief_activity._cycle_counts(start) == {
        "cycles_today": 11, "analysed_today": 7, "entries_today": 1,
    }
    reasons, errors = brief_activity._no_entry_reasons(start)
    assert errors == 4
    assert {row["code"]: row["count"] for row in reasons} == {
        "market_rejected": 1, "proposal_rejected": 2, "other": 2,
    }

    monkeypatch.setattr(autoloop_api.autoloop, "_load_state", lambda: {"enabled": False})
    monkeypatch.setattr(autoloop_api.autoloop, "gates", lambda: {})
    monkeypatch.setattr(autoloop_api.autoloop, "_last_cycle_snapshot", lambda: None)
    body = autoloop_api.get_autoloop_cycles(symbol="BTC")
    assert body["summary"]["total"] == 11
    assert body["summary"]["forecast_deferred"] == 3
    assert next(row for row in body["symbols"] if row["symbol"] == "BTC")[
        "last_kronos_defer_reason"
    ] == "latest forecast failure"
    assert next(item for item in body["items"] if item["cycle_id"] == "futures:BTC:btc-new:btc-new:1:4h:8")[
        "payload"
    ]["kronos_defer_reason"] == "latest forecast failure"
