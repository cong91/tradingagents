"""Autoloop never presents an unverified position scope as a flat book."""

from types import SimpleNamespace

import pytest

from server import autoloop
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.risk import RiskGuard, RiskLimits

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("positions", [(), (object(),)])
def test_candidate_defers_before_analysis_when_scope_is_unverified(monkeypatch, positions):
    candle = SimpleNamespace(open=100, high=110, low=90, close=105)
    market = {
        "unified_symbol": "BTC/USDT:USDT", "one_hour": candle, "four_hour": candle,
        "one_hour_open": "2026-10-26T07:00:00Z",
        "four_hour_open": "2026-10-26T04:00:00Z",
        "one_hour_direction": "bullish", "four_hour_direction": "bullish",
    }
    account = FuturesAccountSnapshot(equity=100, positions=positions)
    executor = SimpleNamespace(
        _bridge=object(), risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=1)),
    )
    writes = []
    monkeypatch.setattr(autoloop, "_closed_futures_context", lambda *_: market)
    monkeypatch.setattr(autoloop, "read_futures_account", lambda *_: account)
    monkeypatch.setattr(autoloop, "claim_futures_cycle", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        autoloop, "persist_futures_cycle_progress", lambda *args, **kwargs: writes.append(kwargs),
    )

    def no_graph(*args, **kwargs):
        pytest.fail("unverified book must not reach the decision graph")

    result = autoloop._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"}, {}, no_graph, executor,
    )

    assert result.reason == "ValueError: futures position scope is unverified; analysis deferred"
    assert result.executed is False
    assert writes == []


def test_unverified_scope_can_be_retried_without_claiming_candle_pair(monkeypatch):
    candle = SimpleNamespace(open=100, high=110, low=90, close=105)
    market = {
        "unified_symbol": "BTC/USDT:USDT", "one_hour": candle, "four_hour": candle,
        "one_hour_open": "2026-10-26T07:00:00Z",
        "four_hour_open": "2026-10-26T04:00:00Z",
        "one_hour_direction": "bullish", "four_hour_direction": "bullish",
    }
    executor = SimpleNamespace(
        _bridge=object(), risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=1)),
    )
    claims, reads = [], []

    def read(_bridge):
        reads.append(1)
        return FuturesAccountSnapshot(equity=100)

    def claim(cycle_id, **kwargs):
        claims.append(cycle_id)
        return len(claims) == 1

    def no_graph(*args, **kwargs):
        pytest.fail("unverified book must not reach the decision graph")

    monkeypatch.setattr(autoloop, "_closed_futures_context", lambda *_: market)
    monkeypatch.setattr(autoloop, "read_futures_account", read)
    monkeypatch.setattr(autoloop, "claim_futures_cycle", claim)
    monkeypatch.setattr(autoloop, "persist_futures_cycle_progress", lambda *args, **kwargs: None)

    candidate = {"ticker": "BTC-USD", "asset_type": "crypto"}
    first = autoloop._process_candidate(None, candidate, {}, no_graph, executor)
    second = autoloop._process_candidate(None, candidate, {}, no_graph, executor)

    assert "scope is unverified" in first.reason
    assert "scope is unverified" in second.reason
    assert reads == [1, 1]
    assert claims == []
