"""Abandoned autoloop candidates cannot write after their cancellation."""

import threading
import time
from types import SimpleNamespace

import pytest

from server import (
    autoloop,
    autoloop_analysis,
    autoloop_futures_context,
    futures_cycle_telemetry,
    job_store,
)
from server.autoloop_telemetry import CycleTelemetry
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.contract import GENERATION_FORWARD, STATUS_OK

pytestmark = pytest.mark.unit


def _install(monkeypatch, cancelled, *, cancel_at):
    candle = SimpleNamespace(open=100, high=110, low=90, close=105)
    context = {
        "unified_symbol": "BTC/USDT:USDT", "one_hour": candle, "four_hour": candle,
        "one_hour_open": 1_799_996_400_000, "four_hour_open": 1_799_985_600_000,
        "one_hour_direction": "bullish", "four_hour_direction": "bullish",
    }
    claims, writes = [], []

    def market(*_args):
        if cancel_at == "context_error":
            cancelled.set()
            raise ValueError("market read completed after cancellation")
        return context

    def account(*_args, **_kwargs):
        if cancel_at == "account":
            cancelled.set()
        return FuturesAccountSnapshot(
            equity=1000, available_balance=900, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )

    monkeypatch.setattr(autoloop_futures_context, "_closed_futures_context", market)
    monkeypatch.setattr(futures_account, "read_futures_account", account)
    monkeypatch.setattr(futures_cycle_telemetry, "claim_cycle", lambda *a, **k: claims.append(k) or True)
    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(futures_cycle_telemetry, "persist_rejection", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(job_store, "futures_position_stops", lambda: {})
    monkeypatch.setattr(autoloop_futures_context, "_bracket_prices", lambda *_a, **_k: (90, 110))
    executor = SimpleNamespace(
        _bridge=object(), risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3)),
    )
    return executor, claims, writes


@pytest.mark.parametrize("cancel_at", ["context_error", "account"])
def test_cancelled_market_reads_never_claim_or_persist(monkeypatch, cancel_at):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at=cancel_at)

    def no_graph(*_args):
        pytest.fail("a cancelled candidate must not build the decision graph")

    if cancel_at == "account":
        with pytest.raises(TimeoutError):
            autoloop_analysis._process_candidate(
                None, {"ticker": "BTC-USD"}, {}, no_graph, executor, cancelled=cancelled,
            )
    else:
        result = autoloop_analysis._process_candidate(
            None, {"ticker": "BTC-USD"}, {}, no_graph, executor, cancelled=cancelled,
        )
        assert "market_rejected" in result.reason
    assert claims == []
    assert writes == []


@pytest.mark.parametrize("graph_fails", [False, True])
def test_late_graph_result_or_error_never_adds_a_cycle_write(monkeypatch, graph_fails):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="graph")
    executor._bridge = SimpleNamespace(client=lambda: object())
    forecast = ForecastBlock(
        symbol="BTC/USDT:USDT", status=STATUS_OK, direction="up",
        direction_counts={"up": 12, "down": 3, "flat": 1},
        direction_ratio={"up": 0.75, "down": 0.1875, "flat": 0.0625},
        band={"low": 100.0, "p50": 106.0, "high": 110.0},
        band_spread_ratio=10 / 105, reference_close=105.0,
        data_open_time=1_799_985_600_000, as_of="2027-01-15T08:00:00+00:00",
        horizon_hours=24, timeframe="4h", paths=16,
        generation_kind=GENERATION_FORWARD, temperature=0.5, profile="small_base",
        model_revision="NeoQuasar/Kronos-small@901c26c13326", artifact_hash="a" * 64,
        created_at="2027-01-15T08:00:00+00:00", latency_ms=100.0,
    )
    monkeypatch.setattr(
        autoloop_futures_context.kronos_context, "forecast_for_cycle", lambda **_: forecast,
    )
    before = []

    class Graph:
        def propagate(self, *_args, **_kwargs):
            before.extend(writes)
            cancelled.set()
            if graph_fails:
                raise RuntimeError("late graph failure")
            return {"futures_execution_intent": "abstain"}, "Hold"

    if graph_fails:
        result = autoloop_analysis._process_candidate(
            None, {"ticker": "BTC-USD"}, {}, lambda *_: Graph(), executor, cancelled=cancelled,
        )
        assert "late graph failure" in result.reason
    else:
        with pytest.raises(TimeoutError):
            autoloop_analysis._process_candidate(
                None, {"ticker": "BTC-USD"}, {}, lambda *_: Graph(), executor, cancelled=cancelled,
            )
    assert len(claims) == 1
    assert before
    assert writes == before


def test_owner_timeout_terminalizes_claimed_forecast_cycle(monkeypatch):
    writes: list[dict] = []

    def persist_progress(*args, **kwargs):
        writes.append({"args": args, **kwargs})

    def slow_candidate(*_args, **kwargs):
        kwargs["on_cycle_checkpoint"]({
            "cycle_id": "futures:BTC/USDT:USDT:cutoff:1",
            "symbol": "BTC/USDT:USDT",
            "payload": {
                "execution_status": "in_progress",
                "forecast_started": True,
                "kronos_forecast": {
                    "symbol": "BTC/USDT:USDT",
                    "status": "timeout",
                    "reason": "sampler deadline exceeded",
                },
            },
        })
        time.sleep(0.2)
        return None

    monkeypatch.setattr(autoloop_analysis, "_process_candidate", slow_candidate)
    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", persist_progress)
    telemetry = CycleTelemetry()
    with pytest.raises(TimeoutError, match="cycle timed out"):
        autoloop._run_candidate_with_timeout(
            None,
            {"ticker": "BTC-USD"},
            {},
            lambda *_: None,
            None,
            time.monotonic() + 0.02,
            threading.Event(),
            threading.Lock(),
            telemetry,
        )

    assert len(writes) == 1
    terminal = writes[0]
    assert terminal["args"] == ("futures:BTC/USDT:USDT:cutoff:1",)
    assert terminal["status"] == "rejected"
    assert terminal["phase"] == "forecast"
    assert terminal["terminal"] is True
    assert terminal["payload"]["kronos_policy"] == "defer"
    assert terminal["payload"]["execution_status"] == "not_started"
    assert terminal["payload"]["executed"] is False
    assert "kronos forecast deferred" in terminal["payload"]["kronos_defer_reason"]


def test_owner_timeout_after_usable_forecast_marks_execution_uncertain(monkeypatch):
    writes: list[dict] = []

    def persist_progress(*args, **kwargs):
        writes.append({"args": args, **kwargs})

    def slow_candidate(*_args, **kwargs):
        kwargs["on_cycle_checkpoint"]({
            "cycle_id": "futures:ETH/USDT:USDT:cutoff:1",
            "symbol": "ETH/USDT:USDT",
            "payload": {
                "execution_status": "in_progress",
                "forecast_started": True,
                "kronos_forecast": {"symbol": "ETH/USDT:USDT", "status": "ok"},
            },
        })
        time.sleep(0.2)
        return None

    monkeypatch.setattr(autoloop_analysis, "_process_candidate", slow_candidate)
    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", persist_progress)
    with pytest.raises(TimeoutError, match="cycle timed out"):
        autoloop._run_candidate_with_timeout(
            None, {"ticker": "ETH-USD"}, {}, lambda *_: None, None,
            time.monotonic() + 0.02, threading.Event(), threading.Lock(), CycleTelemetry(),
        )

    terminal = writes[0]
    assert terminal["status"] == "interrupted"
    assert terminal["phase"] == "context"
    assert terminal["payload"]["kronos_policy"] == "use"
    assert terminal["payload"]["execution_status"] == "not_started"
    assert terminal["payload"]["executed"] is False
