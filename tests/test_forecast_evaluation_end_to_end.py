"""PostgreSQL-backed forward forecast evaluation from cycle to report."""

import socket
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import ccxt
import psycopg
import pytest
from curl_cffi import requests as curl_requests
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from server import (
    forecast_evaluation,
    forecast_evaluation_scoring,
    forecast_evaluation_store,
    futures_cycle_store,
    futures_cycle_telemetry,
)
from server.main import app
from tests.server_api_fixtures import _no_network as _no_network
from tradingagents.dataflows.config import set_config
from tradingagents.forecast.block import ForecastBlock

pytestmark = pytest.mark.unit

_BTC = "BTC/USDT:USDT"
_ETH = "ETH/USDT:USDT"
_DURATIONS_MS = {"15m": 900_000, "1h": 3_600_000}


@pytest.fixture(autouse=True)
def _block_dns_and_curl(monkeypatch):
    # The shared ASGI guard allows only its own Windows self-pipe sockets.
    def refuse(*_args, **_kwargs):
        raise OSError("test tried to reach the network")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(curl_requests.Session, "request", refuse)
    monkeypatch.setattr(curl_requests.AsyncSession, "request", refuse)


@pytest.fixture
def evaluation_environment(postgres_schema, monkeypatch):
    # Keep the entire simulated lifecycle in the database's past, so its real
    # now() sees retries as due without waiting. Scoring controls the boundary.
    with psycopg.connect(postgres_schema) as conn:
        database_now = conn.execute("SELECT now()").fetchone()[0].astimezone(UTC)
    start = (database_now - timedelta(days=2)).replace(minute=5, second=0, microsecond=0)

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.current.timestamp(), tz)

    market = SimpleNamespace(candles={}, requests=[], clients=[], on_fetch=None)

    class PublicBinance(ccxt.Exchange):
        def __init__(self, options):
            self.options = options["options"]
            self.apiKey = self.secret = self.password = self.uid = ""
            self.demo = False
            self.closed = False
            market.clients.append(self)

        def enable_demo_trading(self, enabled):
            self.demo = enabled

        def load_markets(self):
            return {}

        def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
            market.requests.append((symbol, timeframe, since, limit))
            if market.on_fetch is not None:
                market.on_fetch()
            return market.candles.get((symbol, timeframe), [])

        def close(self):
            self.closed = True

    monkeypatch.setattr(forecast_evaluation_store, "datetime", Clock)
    monkeypatch.setattr(forecast_evaluation_scoring, "datetime", Clock)
    monkeypatch.setattr(futures_cycle_store, "_now", lambda: Clock.now(UTC).isoformat())
    monkeypatch.setattr(forecast_evaluation, "_runtime_enabled", None)
    monkeypatch.setattr(forecast_evaluation, "_stop_event", threading.Event())
    monkeypatch.setattr(ccxt, "binance", PublicBinance)
    return SimpleNamespace(clock=Clock, start=start, market=market, dsn=postgres_schema)


@pytest.fixture
def report_client():
    # No lifespan: the test drives the worker explicitly and starts no threads.
    client = TestClient(app)
    yield client
    client.close()


def _enable():
    set_config({"forecast_evaluation_enabled": True})
    forecast_evaluation.configure(enabled=True, reset_cursor=True)


def _record_forecast(
    env, symbol, *, timeframe="1h", horizon=2, direction="up", lookback=64, terminal=True,
):
    now = env.clock.now(UTC)
    duration = _DURATIONS_MS[timeframe]
    now_ms = int(now.timestamp() * 1000)
    input_open = now_ms // duration * duration - duration
    as_of = datetime.fromtimestamp((input_open + duration) / 1000, UTC)
    block = ForecastBlock(
        symbol=symbol,
        status="ok",
        direction=direction,
        direction_counts={"up": 3, "down": 1, "flat": 0},
        direction_ratio={"up": 0.75, "down": 0.25, "flat": 0.0},
        band={"low": 95.0, "p50": 110.0, "high": 120.0},
        reference_close=100.0,
        data_open_time=input_open,
        as_of=as_of.isoformat(),
        horizon_hours=horizon * duration / 3_600_000,
        horizon_bars=horizon,
        effective_lookback_bars=lookback,
        timeframe=timeframe,
        paths=4,
        generation_kind="forward",
        profile="small_base",
        created_at=now.isoformat(),
    )
    evidence = futures_cycle_telemetry.forecast_evidence({
        **block.as_json(), "recorded_at": now.isoformat(),
    })
    scope = futures_cycle_telemetry.forecast_venue_scope({"exec_exchange_id": "binance"})
    hour_open = datetime.fromtimestamp((now_ms // 3_600_000 - 1) * 3600, UTC).isoformat()
    four_hour_open = datetime.fromtimestamp((now_ms // 14_400_000 - 1) * 14_400, UTC).isoformat()
    cycle_id = futures_cycle_telemetry.canonical_cycle_id(
        symbol=symbol, four_hour_open=four_hour_open, one_hour_open=hour_open,
        timeframe=timeframe, open_time=input_open,
    )
    assert futures_cycle_telemetry.claim_cycle(
        cycle_id, symbol=symbol, four_hour_open=four_hour_open, one_hour_open=hour_open,
        timeframe=timeframe, selected_open=input_open,
        payload={"candle_identity": {"timeframe": timeframe, "open_time": input_open}},
    )
    futures_cycle_telemetry.persist_progress(
        cycle_id, phase="context", timestamp=now.isoformat(),
        payload={"kronos_forecast": evidence, "kronos_venue_scope": scope},
    )
    if terminal:
        _finish_cycle(env, cycle_id)
    return cycle_id, evidence, scope


def _finish_cycle(env, cycle_id):
    futures_cycle_telemetry.persist_progress(
        cycle_id, status="rejected", phase="proposal", payload={},
        timestamp=env.clock.now(UTC).isoformat(), terminal=True,
    )


def _target(evidence):
    duration = _DURATIONS_MS[evidence["timeframe"]]
    target_ms = evidence["data_open_time"] + duration * (evidence["horizon_bars"] + 1)
    return datetime.fromtimestamp(target_ms / 1000, UTC)


def _ohlcv(evidence, close, *, shift_bars=0):
    duration = _DURATIONS_MS[evidence["timeframe"]]
    target_open_ms = int(_target(evidence).timestamp() * 1000) - duration
    return [
        target_open_ms + shift_bars * duration,
        114.0, max(114.0, close) + 1.0, min(114.0, close) - 1.0, close, 10.0,
    ]


def test_new_cycle_waits_for_exact_target_and_retries_its_original_snapshot(evaluation_environment):
    env = evaluation_environment
    _enable()
    env.clock.current += timedelta(seconds=1)
    cycle_id, evidence, scope = _record_forecast(env, _BTC)

    first = forecast_evaluation.poll_once()
    assert first["processed"] == [cycle_id]
    assert first["cursor_blocked"] is False
    [pending] = forecast_evaluation_store.list_evaluations()
    assert pending["status"] == "pending"
    assert pending["cycle_id"] == cycle_id
    assert pending["forecast_snapshot"] == evidence
    assert pending["venue_scope"] == scope
    assert pending["target_as_of"] == _target(evidence)
    assert pending["next_attempt_at"] == _target(evidence)
    assert pending["target_close"] is None
    assert pending["directional_correct"] is None
    assert env.market.clients == []

    # Terminal cycles are immutable through the normal store API. Simulate an
    # out-of-band correction without changing the scan position: retries must
    # use the evaluation's snapshot, not re-read the altered cycle payload.
    changed = {**evidence, "direction": "down", "band": {"low": 500, "p50": 600, "high": 700}}
    with psycopg.connect(env.dsn) as conn:
        conn.execute(
            """UPDATE futures_cycles SET payload = jsonb_set(
                   jsonb_set(payload, '{kronos_forecast}', %s), '{kronos_venue_scope}', %s)
               WHERE cycle_id = %s""",
            (Jsonb(changed), Jsonb({"exchange_id": "okx"}), cycle_id),
        )
    assert futures_cycle_store.load_futures_cycle(cycle_id)["payload"]["kronos_forecast"] == changed

    env.clock.current = _target(evidence) - timedelta(microseconds=1)
    forecast_evaluation.poll_once()
    assert forecast_evaluation_store.list_evaluations()[0]["status"] == "pending"
    assert env.market.clients == []

    env.clock.current = _target(evidence)
    env.market.candles[(_BTC, "1h")] = [_ohlcv(evidence, 116.0, shift_bars=-1)]
    missing = forecast_evaluation.poll_once()
    assert missing["due_retries"] == 1
    [unavailable] = forecast_evaluation_store.list_evaluations()
    assert unavailable["status"] == "unavailable"
    assert unavailable["reason"] == "exact target horizon candle is not available yet"
    assert unavailable["retry_count"] == 1
    assert unavailable["next_attempt_at"] == _target(evidence) + timedelta(minutes=1)
    assert unavailable["target_close"] is None
    assert unavailable["forecast_snapshot"] == evidence
    assert unavailable["venue_scope"] == scope

    env.clock.current += timedelta(minutes=1)
    env.market.candles[(_BTC, "1h")] = [_ohlcv(evidence, 116.0)]
    retried = forecast_evaluation.poll_once()
    assert retried["due_retries"] == 1
    [evaluated] = forecast_evaluation_store.list_evaluations()
    assert evaluated["evaluation_id"] == pending["evaluation_id"]
    assert evaluated["forecast_snapshot"] == evidence
    assert evaluated["venue_scope"] == scope
    assert evaluated["status"] == "evaluated"
    assert evaluated["retry_count"] == 1
    assert float(evaluated["target_close"]) == 116.0
    assert float(evaluated["target_return"]) == pytest.approx(0.16)
    assert float(evaluated["absolute_error"]) == 6.0
    assert float(evaluated["absolute_error_pct"]) == pytest.approx(6 / 110 * 100)
    assert evaluated["directional_correct"] is True
    assert evaluated["band_contains_target"] is True
    assert evaluated["evaluated_at"] == env.clock.current
    assert env.market.requests[-1] == (_BTC, "1h", _ohlcv(evidence, 116.0)[0], 1)
    assert all(client.demo and client.closed for client in env.market.clients)

    forecast_evaluation.poll_once()
    assert len(forecast_evaluation_store.list_evaluations()) == 1
    assert forecast_evaluation_store.list_evaluations()[0]["status"] == "evaluated"


def test_invalid_forecast_retains_its_recorded_evidence_and_validation_reason(evaluation_environment):
    env = evaluation_environment
    _enable()
    env.clock.current += timedelta(seconds=1)
    cycle_id, evidence, _ = _record_forecast(env, _BTC, lookback=-1)

    result = forecast_evaluation.poll_once()

    assert result["processed"] == [cycle_id]
    [row] = forecast_evaluation_store.list_evaluations()
    assert row["status"] == "invalid"
    assert row["forecast_snapshot"] == evidence, row["reason"]
    assert row["reason"] == "effective input lookback is malformed"
    assert row["target_close"] is None
    assert env.market.clients == []


def test_enable_does_not_backfill_even_if_an_old_forecast_finishes_later(evaluation_environment):
    env = evaluation_environment
    env.clock.current = env.start - timedelta(minutes=2)
    finished_id, _, _ = _record_forecast(env, "SOL/USDT:USDT")
    env.clock.current += timedelta(minutes=1)
    late_id, _, _ = _record_forecast(env, _ETH, terminal=False)

    env.clock.current = env.start
    _enable()
    env.clock.current += timedelta(seconds=1)
    _finish_cycle(env, late_id)
    new_id, _, _ = _record_forecast(env, _BTC)
    polled = forecast_evaluation.poll_once()

    assert polled["cycles"] == 1
    assert polled["processed"] == [new_id]
    assert finished_id != new_id and late_id != new_id
    [row] = forecast_evaluation_store.list_evaluations()
    assert row["cycle_id"] == new_id
    assert row["status"] == "pending"


def test_cursor_reset_fences_inflight_poll_but_resumes_captured_retries(evaluation_environment):
    env = evaluation_environment
    _enable()
    env.clock.current += timedelta(seconds=1)
    old_id, evidence, _ = _record_forecast(env, _BTC)
    forecast_evaluation.poll_once()
    old_cursor = forecast_evaluation_store.get_cursor()
    assert old_cursor is not None
    env.clock.current = _target(evidence) - timedelta(minutes=55)
    unscanned_id, _, _ = _record_forecast(env, _ETH, horizon=1)

    env.clock.current = _target(evidence)
    env.market.candles[(_BTC, "1h")] = [_ohlcv(evidence, 116.0)]

    def reset_from_another_process():
        env.market.on_fetch = None
        env.clock.current += timedelta(seconds=1)
        assert forecast_evaluation_store.initialize_cursor(reset=True)

    env.market.on_fetch = reset_from_another_process
    interrupted = forecast_evaluation.poll_once()
    reset_cursor = forecast_evaluation_store.get_cursor()
    assert reset_cursor != old_cursor
    assert interrupted["cycles"] == 1
    assert interrupted["due_retries"] == 1
    assert interrupted["cursor_blocked"] is True
    assert interrupted["processed"] == []
    assert forecast_evaluation_store.advance_cursor(old_cursor, env.clock.current.isoformat(), old_id) is False
    [old_row] = forecast_evaluation_store.list_evaluations()
    assert old_row["cycle_id"] == old_id
    assert old_row["status"] == "pending"
    assert old_row["target_close"] is None

    after_reset = forecast_evaluation.poll_once()
    assert after_reset["cycles"] == 0
    assert after_reset["due_retries"] == 1
    assert after_reset["processed"] == [old_id]
    assert forecast_evaluation_store.get_cursor() == reset_cursor
    [old_row] = forecast_evaluation_store.list_evaluations()
    assert old_row["status"] == "evaluated"
    assert old_row["forecast_snapshot"] == evidence

    env.clock.current += timedelta(seconds=1)
    new_id, _, _ = _record_forecast(env, _BTC)
    result = forecast_evaluation.poll_once()
    assert result["processed"] == [new_id]
    rows = forecast_evaluation_store.list_evaluations()
    assert {row["cycle_id"] for row in rows} == {old_id, new_id}
    assert unscanned_id not in {row["cycle_id"] for row in rows}
    assert {row["status"] for row in rows} == {"evaluated", "pending"}


def test_report_filters_and_counts_all_matching_rows_not_just_the_page(
    evaluation_environment, report_client,
):
    env = evaluation_environment
    empty = report_client.get("/api/forecast-evaluations").json()
    assert empty["status"] == "unavailable"
    assert empty["total_count"] == 0
    assert empty["directional_accuracy"] is None
    assert empty["band_containment"] is None
    assert empty["items"] == []

    _enable()
    env.clock.current += timedelta(seconds=1)
    _, first, _ = _record_forecast(env, _BTC, horizon=1)
    _, second, _ = _record_forecast(env, _BTC, timeframe="15m", horizon=1, direction="down")
    _record_forecast(env, _ETH, horizon=2)
    forecast_evaluation.poll_once()
    env.clock.current = _target(first)
    env.market.candles[(_BTC, "1h")] = [_ohlcv(first, 116.0)]
    env.market.candles[(_BTC, "15m")] = [_ohlcv(second, 125.0)]
    forecast_evaluation.poll_once()
    requests_before_report = list(env.market.requests)
    client_count_before_report = len(env.market.clients)

    response = report_client.get("/api/forecast-evaluations", params={"limit": 1})
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "partial"
    assert report["total_count"] == 3
    assert report["counts"] == {"pending": 1, "evaluated": 2, "unavailable": 0, "invalid": 0}
    assert report["sample_count"] == 2
    assert report["directional_sample_count"] == 2
    assert report["band_sample_count"] == 2
    assert report["directional_accuracy"] == 0.5
    assert report["band_containment"] == 0.5
    assert report["metrics_scope"] == "all_matching_rows"
    assert len(report["items"]) == 1
    assert "some forecast horizons have not closed" in report["coverage_warnings"]

    btc = report_client.get("/api/forecast-evaluations", params={"symbol": _BTC, "limit": 1}).json()
    assert btc["status"] == "complete"
    assert btc["total_count"] == btc["sample_count"] == 2
    assert btc["directional_accuracy"] == btc["band_containment"] == 0.5
    assert len(btc["items"]) == 1
    assert btc["items"][0]["symbol"] == _BTC

    eth = report_client.get("/api/forecast-evaluations", params={"symbol": _ETH}).json()
    assert eth["status"] == "partial"
    assert eth["counts"]["pending"] == 1
    assert eth["sample_count"] == 0
    assert eth["directional_accuracy"] is None
    assert eth["band_containment"] is None

    absent = report_client.get("/api/forecast-evaluations", params={"symbol": "XRP/USDT:USDT"}).json()
    assert absent["status"] == "unavailable"
    assert absent["total_count"] == 0
    assert absent["items"] == []
    assert absent["directional_accuracy"] is None
    assert absent["band_containment"] is None
    assert "no forecast evaluation rows are available" in absent["coverage_warnings"]
    assert any("self-reported" in warning for warning in absent["coverage_warnings"])
    assert env.market.requests == requests_before_report
    assert len(env.market.clients) == client_count_before_report
