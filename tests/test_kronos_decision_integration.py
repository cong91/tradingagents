"""F2: the active Kronos block reaches the REAL Trader and PM prompts.

The forecast is unconditional for eligible Futures candidates. This suite proves
that its compact block reaches the real agent prompts and that failures defer a
candidate before graph/LLM construction, while the separate Auto Futures mode
safety check remains covered.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import (
    autoloop_analysis,
    autoloop_cycle_records,
    autoloop_execution,
    autoloop_futures_context,
    autoloop_futures_execution,
    futures_cycle_telemetry,
)
from server.autoloop_telemetry import CycleTelemetry
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.contract import STATUS_OK, STATUS_TIMEOUT
from tradingagents.forecast.service import KronosForecastService
from tradingagents.forecast.settings import ForecastSettings

pytestmark = pytest.mark.unit

_FOUR_HOUR_MS = 14_400_000


class _RecordingLLM:
    """A fake LLM that records every prompt it is asked to answer.

    It answers with text, never with a structured object, so the agent node runs
    its real prompt-building path and the recorded content is exactly what a
    real provider would have received. The Trader calls ``invoke(messages)`` and
    the PM's structured fallback calls ``invoke(prompt_string)``; both shapes are
    recorded as-is, never split into characters.
    """

    def __init__(self, answer: str = "Buy") -> None:
        self.answer = answer
        self.calls: list[list[dict]] = []

    def invoke(self, messages, **_kwargs):
        if isinstance(messages, str):
            self.calls.append([{"content": messages}])
        else:
            self.calls.append(list(messages))
        return SimpleNamespace(content=self.answer)

    def __call__(self, messages, **_kwargs):  # some binders call the object directly
        return self.invoke(messages)

    @property
    def text(self) -> str:
        return "\n".join(
            str(message.get("content") or "") for call in self.calls for message in call
        )


class _GoodSampler:
    def __init__(self, *, paths: int = 16, up_share: float = 0.75) -> None:
        self.paths = paths
        self.up_share = up_share
        self.calls: list[list[tuple]] = []

    def sample(self, requests):
        from tradingagents.forecast.sampler import SampledSymbol

        self.calls.append(list(requests))
        out = []
        for symbol, _timeframe, _horizon, candles, _seed in requests:
            reference = candles[-1].close
            ups = round(self.paths * self.up_share)
            drawn = []
            for index in range(self.paths):
                move = 30.0 if index < ups else -30.0
                end = reference + move
                drawn.append({
                    "opens": [reference, end], "closes": [reference, end],
                    "highs": [max(reference, end), max(reference, end)],
                    "lows": [min(reference, end), min(reference, end)], "seed": 5000 + index,
                })
            out.append(SampledSymbol(
                symbol=symbol, status=STATUS_OK, paths=tuple(drawn),
                duration_ms=21_900.0, params={"paths": self.paths},
            ))
        return out

    def close(self) -> None:
        pass


def _block(**overrides) -> ForecastBlock:
    base = {
        "symbol": "BTC/USDT:USDT", "status": STATUS_OK, "direction": "up",
        "direction_counts": {"up": 12, "down": 3, "flat": 1},
        "direction_ratio": {"up": 0.75, "down": 0.1875, "flat": 0.0625},
        "band": {"low": 74_150.0, "p50": 74_230.0, "high": 74_310.0},
        "band_spread_ratio": 0.0022,
        "reference_close": 74_200.0, "data_open_time": 1_799_990_400_000,
        "as_of": "2026-11-08T20:00:00+00:00", "horizon_hours": 24, "timeframe": "4h",
        "paths": 16, "generation_kind": "forward", "temperature": 0.5,
        "model_revision": "NeoQuasar/Kronos-small@901c26c13326", "artifact_hash": "a" * 64,
        "latency_ms": 22_491.0,
    }
    base.update(overrides)
    return ForecastBlock(**base)


def _service(sampler=None, **overrides) -> KronosForecastService:
    settings = {"paths": 16, "lookback_bars": 96}
    settings.update(overrides)
    return KronosForecastService(
        sampler=sampler if sampler is not None else _GoodSampler(),
        settings=ForecastSettings(**settings), artifact_hash="a" * 64,
        created_at=datetime(2026, 11, 8, 20, tzinfo=UTC),
    )


# -- the reachability proof -------------------------------------------------


def test_the_forecast_text_reaches_the_real_trader_prompt():
    """The Trader's own messages must contain the block, before it decides."""
    from server.kronos_context import append_to_context
    from tradingagents.forecast.rendering import render_forecast_block

    block = _block()
    context = append_to_context(
        "Futures Account and Market Context:\nFutures equity: 1,000 USDT", block,
    )
    llm = _RecordingLLM("Buy")
    node = create_trader(llm)
    state = {
        "company_of_interest": "BTC-USD",
        "trade_date": "2026-11-08",
        "asset_type": "crypto",
        "market_report": "ATR 500",
        "investment_plan": "Enter long.",
        "execution_context": context,
        "sender": "Research Manager",
    }
    node(state)

    text = llm.text
    assert "Kronos forecast" in text, "the real Trader prompt has no forecast block"
    assert "NOT a probability" in text
    assert "paths=16" in text and "per_path_samples=1" in text
    assert "data_open_time=" in text and "horizon=24h" in text
    assert "up=12/16" in text, "the sample ratio must arrive with its counts"
    assert "temperature=0.5" in text
    # The block is data, not an instruction: no order language in it.
    rendered = render_forecast_block(block)
    assert "buy" not in rendered.lower() and "sell" not in rendered.lower()


def test_the_forecast_text_reaches_the_real_portfolio_manager_prompt():
    from server.kronos_context import append_to_context

    context = append_to_context("**Current Futures Account and Market Context:**\nok", _block())
    llm = _RecordingLLM("Buy")
    node = create_portfolio_manager(llm)
    state = {
        "company_of_interest": "BTC-USD",
        "trade_date": "2026-11-08",
        "asset_type": "crypto",
        "investment_plan": "Enter long.",
        "trader_investment_plan": "Long 2x.",
        "execution_context": context,
        "risk_debate_state": {
            "history": "risky: fine", "aggressive_history": "fine",
            "conservative_history": "careful", "neutral_history": "neutral",
            "judge_decision": "proceed", "current_aggressive_response": "",
            "current_conservative_response": "", "current_neutral_response": "",
            "count": 1, "latest_speaker": "Aggressive",
        },
    }
    node(state)

    text = llm.text
    assert "Kronos forecast" in text, "the real PM prompt has no forecast block"
    assert "model_sample_ratio_not_probability" in text or "NOT a probability" in text
    assert "up=12/16" in text
    assert "NeoQuasar/Kronos-small" in text, "the revision must travel with the numbers"
    assert "Futures Account and Market Context" in text


def test_the_block_reaches_the_context_before_the_graph_is_built(monkeypatch):
    """Ordering: the forecast is appended to execution_context, then the graph runs.

    This drives the real ``_process_candidate`` with an offline graph and asserts
    the context the graph received already contains the forecast — the ordering
    requirement stated as an assertion about the actual call path.
    """

    seen: dict = {}
    events: list[str] = []
    execution_lock = threading.Lock()
    real_mode_check = autoloop_execution.raise_if_mode_disabled
    real_deadline_check = autoloop_execution.raise_if_execution_closed

    def check_mode(mode, stage, authorization):
        if stage == "kronos forecast":
            events.append("mode")
        real_mode_check(mode, stage, authorization)

    def check_deadline(deadline, cancelled, stage):
        if stage == "candidate BTC-USD kronos forecast":
            events.append("deadline")
        real_deadline_check(deadline, cancelled, stage)

    def checkpoint(cycle):
        payload = cycle["payload"]
        if payload.get("forecast_started"):
            events.append("checkpoint_after" if "kronos_forecast" in payload else "checkpoint_before")
            seen[events[-1]] = cycle

    def persist(_cycle_id, **kwargs):
        payload = kwargs["payload"]
        if kwargs.get("phase") == "context" and "kronos_forecast" in payload:
            events.append("persist")
            seen["persist_payload"] = payload
            seen["persist_timestamps"] = payload["phase_timestamps"]
            seen["persist_timestamp"] = kwargs["timestamp"]

    def build_graph(*_args):
        events.append("build")
        return Graph()

    class Graph:
        def propagate(self, ticker, trade_date, *, asset_type, execution_context, **kwargs):
            events.append("graph")
            seen["context"] = execution_context
            return {"futures_execution_intent": "abstain"}, "Hold"

    monkeypatch.setattr(autoloop_execution, "raise_if_mode_disabled", check_mode)
    monkeypatch.setattr(autoloop_execution, "raise_if_execution_closed", check_deadline)

    monkeypatch.setattr(autoloop_futures_context, '_closed_futures_context', lambda *_: {
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": 1_799_990_400_000,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": 1_799_990_400_000, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, 'read_futures_account', lambda *_a, **_k: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda _t, direction: (90.0, 110.0))
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_a, **_k: True)
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', persist)
    monkeypatch.setattr(autoloop_cycle_records, '_record_futures_cycle_rejection', lambda *_a, **_k: None)
    monkeypatch.setattr(
        autoloop_futures_execution, '_process_futures_candidate',
        lambda _e, _t, _s, _c, result, **kwargs: setattr(result, "reason", "captured"),
    )
    # Replace the forecast at the existing seam to inspect its inputs and timing.
    real_block = _block()

    def forecast(*args, **kwargs):
        events.append("forecast")
        assert execution_lock.acquire(blocking=False)
        execution_lock.release()
        seen["forecast_args"] = args
        seen["forecast_kwargs"] = kwargs
        return real_block

    monkeypatch.setattr(autoloop_futures_context, "_kronos_block_for", forecast)
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )
    config = {
        **DEFAULT_CONFIG,
        "kronos_active_profile": "small_base",
        "risk_derivatives_max_hold_hours": 24,
        "risk_max_derivatives_leverage": 3.0,
    }
    telemetry = CycleTelemetry()
    autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"}, config, build_graph, executor,
        telemetry=telemetry, on_cycle_checkpoint=checkpoint, execution_lock=execution_lock,
    )

    assert events == [
        "mode", "deadline", "checkpoint_before", "forecast", "mode", "deadline",
        "checkpoint_after", "persist", "build", "graph",
    ]
    before, after = seen["checkpoint_before"], seen["checkpoint_after"]
    assert {key: before[key] for key in ("cycle_id", "symbol", "four_hour_open", "one_hour_open")} == {
        key: after[key] for key in ("cycle_id", "symbol", "four_hour_open", "one_hour_open")
    }
    assert before["symbol"] == "BTC/USDT:USDT"
    assert seen["forecast_args"][0] is executor
    assert seen["forecast_args"][1] == "BTC-USD"
    assert seen["forecast_args"][2]["unified_symbol"] == before["symbol"]
    assert seen["forecast_args"][3] is config
    assert seen["forecast_kwargs"] == {"cycle_id": before["cycle_id"], "deadline": None, "cancelled": None}
    assert "kronos_forecast" not in before["payload"]
    assert before["payload"]["phase_timestamps"]["forecast_started_at"]
    assert after["payload"]["kronos_forecast"] == real_block.as_json()
    assert after["payload"]["kronos_policy"] == "use"
    assert after["payload"] is not seen["persist_payload"]
    assert after["payload"]["phase_timestamps"] is seen["persist_timestamps"]
    assert after["payload"]["phase_timestamps"]["kronos_finished_at"] == seen["persist_timestamp"]
    span_events = [
        event["event"] for event in telemetry.snapshot()["events"]
        if event["stage"] == "kronos_active_context"
    ]
    assert span_events == ["started", "finished"]

    context = seen.get("context")
    assert context is not None
    assert "Kronos forecast" in context, "the graph ran without the forecast in its context"
    assert "Futures equity" in context, "the incumbent context must still be present"
    # The forecast section comes after the account context the agents already had.
    assert context.index("Futures equity") < context.index("Kronos forecast")


# -- failure / boundary -------------------------------------------------------


def test_a_timeout_renders_a_readable_status_and_defers_the_candidate():
    from server.kronos_context import append_to_context, policy_for
    from tradingagents.forecast.rendering import render_forecast_block

    block = ForecastBlock(
        symbol="BTC/USDT:USDT", status=STATUS_TIMEOUT,
        reason="Kronos inference exceeded its 600s chunk deadline",
    )
    rendered = render_forecast_block(block)
    assert "status=timeout" in rendered
    assert "reason=" in rendered and "not a passed check" in rendered
    assert "paths=" not in rendered, "a failed block must not show a sample count"
    assert policy_for(block) == "defer"
    assert policy_for(None) == "defer"
    context = append_to_context("incumbent", block)
    assert context.startswith("incumbent") and "status=timeout" in context


@pytest.mark.parametrize("forecast_missing", [False, True])
def test_defer_stops_the_candidate_before_the_graph_and_the_llm(monkeypatch, forecast_missing):
    """The failure policy is real: a defer blocks the graph/LLM and persists.

    The reproduced failure was that ``kronos_policy='defer'`` was only RECORDED
    while ``_process_candidate`` went on to build the graph anyway. The policy
    must stop the candidate at the seam, with the reason written on the cycle.
    """

    built: list[str] = []

    def _graph_builder(*_a, **_k):
        built.append("graph")
        raise AssertionError("a deferred candidate must never reach the graph/LLM")

    persisted: list[dict] = []
    persisted_timestamps: list[dict] = []

    def persist(_cid, payload=None, **kwargs):
        persisted.append({"payload": dict(payload or {}), **kwargs})
        if kwargs.get("terminal"):
            persisted_timestamps.append(payload["phase_timestamps"])

    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", persist)
    monkeypatch.setattr(autoloop_futures_context, '_closed_futures_context', lambda *_: {
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": 1_799_990_400_000,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": 1_799_990_400_000, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, 'read_futures_account', lambda *_a, **_k: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda _t, direction: (90.0, 110.0))
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_a, **_k: True)
    checkpoints: list[dict] = []
    failed = None if forecast_missing else ForecastBlock(
        symbol="BTC/USDT:USDT", status=STATUS_TIMEOUT,
        reason="Kronos inference exceeded its 600s chunk deadline",
    )
    monkeypatch.setattr(
        autoloop_futures_context, "_kronos_block_for", lambda *_args, **_kwargs: failed,
    )
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )
    # failure_policy defaults to defer; the candidate must stop here.
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"},
        {
            **DEFAULT_CONFIG,
            "kronos_active_profile": "small_base",
            "risk_derivatives_max_hold_hours": 24,
            "risk_max_derivatives_leverage": 3.0,
        },
        _graph_builder, executor, on_cycle_checkpoint=checkpoints.append,
    )
    assert built == [], "the graph (and any LLM call) must not run for a deferred candidate"
    assert result.reason is not None and "deferred" in result.reason
    assert result.reason.endswith("no block" if forecast_missing else "chunk deadline")
    forecast_checkpoints = [row for row in checkpoints if row["payload"].get("forecast_started")]
    assert len(forecast_checkpoints) == 2
    assert "kronos_forecast" not in forecast_checkpoints[0]["payload"]
    assert forecast_checkpoints[1]["payload"]["kronos_policy"] == "defer"
    terminal = [row for row in persisted if row.get("terminal")]
    assert terminal, "the defer reason must be persisted on the claimed cycle"
    final = terminal[-1]
    assert final.get("status") == "rejected" and final.get("phase") == "forecast"
    assert final["payload"]["execution_status"] == "not_started"
    assert "kronos_defer_reason" in final["payload"]
    assert final["payload"]["kronos_policy"] == "defer"
    assert final["payload"]["kronos_forecast"]["status"] == (
        "unavailable" if forecast_missing else STATUS_TIMEOUT
    )
    assert final["payload"]["kronos_defer_reason"] == result.reason
    assert final["payload"]["executed"] is False
    assert final["payload"]["phase_timestamps"]["kronos_finished_at"]
    assert persisted_timestamps[-1] is not forecast_checkpoints[-1]["payload"]["phase_timestamps"]


def test_a_forecast_client_failure_is_recorded_as_defer(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setattr(
        futures_cycle_telemetry, "persist_progress",
        lambda _cid, payload=None, **kwargs: persisted.append(
            {"payload": dict(payload or {}), **kwargs}
        ),
    )
    monkeypatch.setattr(autoloop_futures_context, "_closed_futures_context", lambda *_: {
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": 1_799_990_400_000,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": 1_799_990_400_000, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, "read_futures_account", lambda *_a, **_k: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(autoloop_futures_context, "_bracket_prices", lambda _t, direction: (90.0, 110.0))
    monkeypatch.setattr(futures_cycle_telemetry, "claim_cycle", lambda *_a, **_k: True)
    monkeypatch.setattr(autoloop_analysis.job_store, "futures_position_stops", lambda: {})

    def fail_client():
        raise RuntimeError("forecast venue unavailable")

    def fail_graph(*_args):
        pytest.fail("a client failure must defer before graph construction")

    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=fail_client),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"},
        {**DEFAULT_CONFIG, "risk_max_derivatives_leverage": 3.0}, fail_graph, executor,
    )

    assert "forecast venue unavailable" in result.reason
    terminal = [row for row in persisted if row.get("terminal")]
    assert terminal[-1]["payload"]["kronos_policy"] == "defer"
    assert terminal[-1]["payload"]["kronos_forecast"]["status"] == "error"
    assert "forecast venue unavailable" in terminal[-1]["payload"]["kronos_defer_reason"]


def test_the_forecast_stage_takes_no_execution_lock(monkeypatch):
    """Bracket monitoring and protection must not be blocked by inference.

    ``_kronos_block_for`` is called with no lock in scope at all; this pins that
    the helper's signature cannot accept one, so a later edit cannot quietly
    thread the cycle's execution lock through the inference path.
    """
    import inspect

    from server import kronos_context

    parameters = inspect.signature(kronos_context.forecast_for_cycle).parameters
    assert "execution_lock" not in parameters
    assert not any("lock" in name for name in parameters)


def test_closing_the_mode_mid_inference_stops_the_candidate(monkeypatch):
    """The mode is re-checked AFTER the model call, before its result is used."""
    from server.auto_modes import AutoMode

    order: list[str] = []
    # The mode is enabled when the candidate starts and CLOSED during the model
    # call — the window a 20-200 s inference realistically spans.
    state = {"closed": False}
    monkeypatch.setattr(
        autoloop_execution, 'mode_is_enabled',
        lambda *_a, **_k: not state["closed"],
    )

    class Graph:
        def propagate(self, *_a, **_k):
            order.append("graph")
            return {"futures_execution_intent": "abstain"}, "Hold"

    monkeypatch.setattr(autoloop_futures_context, '_closed_futures_context', lambda *_: {
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": 1_799_990_400_000,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": 1_799_990_400_000, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, 'read_futures_account', lambda *_a, **_k: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda _t, direction: (90.0, 110.0))
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_a, **_k: True)
    monkeypatch.setattr(futures_cycle_telemetry, 'persist_progress', lambda *_a, **_k: None)

    def _forecast(**_kwargs):
        order.append("forecast")
        # The operator closes the mode while the model is running.
        state["closed"] = True
        return _block()

    monkeypatch.setattr(autoloop_futures_context.kronos_context, "forecast_for_cycle", _forecast)
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )
    # The re-check after inference raises: the candidate never reaches the graph.
    with pytest.raises(autoloop_execution.AutoModeDisabled) as raised:
        autoloop_analysis._process_candidate(
            None, {"ticker": "BTC-USD", "asset_type": "crypto"},
            {
                **DEFAULT_CONFIG,
                "kronos_active_profile": "small_base",
                "risk_derivatives_max_hold_hours": 24,
                "risk_max_derivatives_leverage": 3.0,
            },
            lambda *_: Graph(), executor, mode=AutoMode.FUTURES,
        )
    assert order == ["forecast"], "the graph must not run after the mode closed"
    assert "disabled" in str(raised.value)


def test_the_forecast_block_is_recorded_on_the_cycle_payload(monkeypatch):
    """The cycle keeps the evidence, so the dashboard can show what was seen."""
    persisted: list[dict] = []
    monkeypatch.setattr(
        futures_cycle_telemetry, 'persist_progress',
        lambda _cid, payload=None, **_k: persisted.append(dict(payload or {})),
    )
    monkeypatch.setattr(autoloop_futures_context, '_closed_futures_context', lambda *_: {
        "unified_symbol": "BTC/USDT:USDT", "symbol": "BTC/USDT:USDT",
        "four_hour_open": 1_799_990_400_000,
        "one_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "four_hour": SimpleNamespace(open=1, high=1, low=1, close=1),
        "one_hour_open": 1_799_990_400_000, "one_hour_close": 1.0,
        "four_hour_close": 1.0, "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    })
    monkeypatch.setattr(futures_account, 'read_futures_account', lambda *_a, **_k: (
        FuturesAccountSnapshot(
            equity=1_000.0, available_balance=900.0, wallet_balance=950.0,
            unrealized_pnl=0.0, equity_currency="USDT",
            available_balance_currency="USDT", account_scope="unified",
            positions_scope_complete=True,
        )
    ))
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda _t, direction: (90.0, 110.0))
    monkeypatch.setattr(futures_cycle_telemetry, 'claim_cycle', lambda *_a, **_k: True)
    monkeypatch.setattr(autoloop_futures_context.kronos_context, "forecast_for_cycle", lambda **_k: _block())
    monkeypatch.setattr(
        autoloop_futures_execution, '_process_futures_candidate',
        lambda _e, _t, _s, _c, result, **kwargs: setattr(result, "reason", "captured"),
    )
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()),
        risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3.0)),
    )
    autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD", "asset_type": "crypto"},
        {
            **DEFAULT_CONFIG,
            "kronos_active_profile": "small_base",
            "risk_derivatives_max_hold_hours": 24,
            "risk_max_derivatives_leverage": 3.0,
        },
        lambda *_: object(), executor, telemetry=CycleTelemetry(),
    )
    with_forecast = [row for row in persisted if row.get("kronos_forecast")]
    assert with_forecast, "no cycle write carried the forecast"
    recorded = with_forecast[-1]["kronos_forecast"]
    # The JSON the dashboard reads must carry the honest labels, not a bare ratio.
    assert recorded["calibration_label"] == "model_sample_ratio_not_probability"
    assert recorded["paths"] == 16 and recorded["per_path_samples"] == 1
    assert recorded["temperature"] == 0.5
    assert recorded["direction"] == "up"
    assert json.dumps(recorded)  # serializable for the payload write
