"""Abandoned autoloop candidates cannot write after their cancellation."""

import threading
from types import SimpleNamespace

import pytest

from server import autoloop_analysis, autoloop_futures_context, futures_cycle_telemetry, job_store
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.risk import RiskGuard, RiskLimits

pytestmark = pytest.mark.unit


def _install(monkeypatch, cancelled, *, cancel_at):
    candle = SimpleNamespace(open=100, high=110, low=90, close=105)
    context = {
        "unified_symbol": "BTC/USDT:USDT", "one_hour": candle, "four_hour": candle,
        "one_hour_open": 1_799_990_400_000, "four_hour_open": 1_799_990_400_000,
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
