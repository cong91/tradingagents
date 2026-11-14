"""Incomplete mini/2k history must defer a futures candidate before the LLM."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import (
    autoloop_analysis,
    autoloop_futures_context,
    futures_cycle_telemetry,
    kronos_context,
)
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import settings_from_config

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000


@pytest.mark.parametrize(
    ("history_shape", "expected_reason"),
    [("missing", "received 2047, requested 2048"), ("gap", "candle gap")],
)
def test_missing_or_gap_history_defers_candidate_before_graph_and_llm(
    monkeypatch, history_shape, expected_reason,
):
    graph_builds: list[str] = []

    def _graph_builder(*_args, **_kwargs):
        graph_builds.append("graph")
        raise AssertionError("incomplete forecast history must defer before the graph/LLM")

    step = _FOUR_HOUR_MS
    four_hour_open = 1_800_000_000_000 - (1_800_000_000_000 % step) - step
    opens = [four_hour_open - step * (2047 - index) for index in range(2048)]
    if history_shape == "missing":
        opens = opens[-2047:]
    else:
        del opens[20]
    rows = [[open_time, 100.0, 101.0, 99.0, 100.0, 10.0] for open_time in opens]

    class _HistoryVenue:
        max_ohlcv_limit = 1000

        def __init__(self):
            self.pages: list[tuple[int, int]] = []

        def fetch_ohlcv(self, symbol, timeframe=None, since=None, limit=None, **_kwargs):
            assert symbol == "BTC/USDT:USDT"
            assert timeframe == "4h"
            if limit == 1:
                return [rows[-1]]
            assert limit == self.max_ohlcv_limit
            self.pages.append((since, limit))
            return [row for row in rows if row[0] >= since][:limit]

    class _Sampler:
        def __init__(self):
            self.calls: list[list[tuple]] = []

        def sample(self, requests):
            self.calls.append(list(requests))
            raise AssertionError("invalid history must be refused before inference")

        def close(self):
            pass

    venue = _HistoryVenue()
    sampler = _Sampler()

    def _service_from_config(cls, config, **_kwargs):
        return KronosForecastService(
            sampler=sampler, settings=settings_from_config(config), artifact_hash="a" * 64,
        )

    monkeypatch.setattr(
        kronos_context.KronosForecastService, "from_config", classmethod(_service_from_config),
    )
    monkeypatch.setattr(autoloop_futures_context, "_closed_futures_context", lambda *_: {
        "decision_as_of": datetime.fromtimestamp((four_hour_open + step) / 1000, UTC).isoformat(),
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": four_hour_open,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": four_hour_open, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, "read_futures_account", lambda *_args, **_kwargs: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(
        autoloop_futures_context, "_bracket_prices", lambda _ticker, direction: (90.0, 110.0),
    )
    monkeypatch.setattr(futures_cycle_telemetry, "claim_cycle", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(autoloop_analysis.job_store, "futures_position_stops", lambda: {})
    persisted: list[dict] = []
    monkeypatch.setattr(
        futures_cycle_telemetry, "persist_progress",
        lambda _cycle_id, payload=None, **kwargs: persisted.append(
            {"payload": dict(payload or {}), **kwargs}
        ),
    )
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: venue),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )

    result = autoloop_analysis._process_candidate(
        None,
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {
            "kronos_active_profile": "mini_2k",
            "risk_max_derivatives_leverage": 3.0,
        },
        _graph_builder,
        executor,
    )

    assert len(venue.pages) >= 3
    assert sampler.calls == [], "invalid history must be refused before inference"
    assert graph_builds == [], "defer must stop graph construction and any LLM calls"
    assert result.reason is not None and "deferred" in result.reason
    assert expected_reason in result.reason
    terminal = [row for row in persisted if row.get("terminal")]
    assert terminal
    assert terminal[-1]["payload"]["kronos_policy"] == "defer"
    assert expected_reason in terminal[-1]["payload"]["kronos_defer_reason"]
