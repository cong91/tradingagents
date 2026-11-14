"""Abandoned autoloop candidates cannot write after their cancellation."""

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from server import (
    autoloop,
    autoloop_analysis,
    autoloop_execution,
    autoloop_futures_context,
    autoloop_kronos,
    futures_cycle_telemetry,
    job_store,
)
from server.auto_modes import AutoMode
from server.autoloop_telemetry import CycleTelemetry
from tests.execution_derivatives_support import VERIFIED_IDENTITY
from tradingagents.execution import futures_account
from tradingagents.execution.futures_account import FuturesAccountSnapshot
from tradingagents.execution.futures_entry_journal import managed_live_provenance
from tradingagents.execution.risk import RiskGuard, RiskLimits
from tradingagents.execution.sizing import PlannedOrder
from tradingagents.forecast.block import ForecastBlock
from tradingagents.forecast.contract import GENERATION_FORWARD, STATUS_OK

pytestmark = pytest.mark.unit


def _install(monkeypatch, cancelled, *, cancel_at):
    from tradingagents.auto_timeframes import last_closed_candle
    from tradingagents.dataflows.market_data import Candle

    now = datetime.now(UTC)
    opened, closed = last_closed_candle(now, "4h")
    candle = Candle(
        "BTC/USDT:USDT", "4h", int(opened.timestamp() * 1000), int(closed.timestamp() * 1000),
        100, 110, 90, 105, 1, True,
    )
    context = {
        "decision_as_of": now.isoformat(),
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
    monkeypatch.setattr(autoloop_analysis.autoloop_candles, "selected_candle", lambda *_a, **_k: candle)
    monkeypatch.setattr(futures_account, "read_futures_account", account)
    monkeypatch.setattr(futures_cycle_telemetry, "claim_cycle", lambda *a, **k: claims.append(k) or True)
    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(futures_cycle_telemetry, "persist_rejection", lambda *a, **k: writes.append(k))
    monkeypatch.setattr(job_store, "futures_position_stops", lambda: {})
    monkeypatch.setattr(autoloop_futures_context, "_bracket_prices", lambda *_a, **_k: (90, 110))
    executor = SimpleNamespace(
        _bridge=SimpleNamespace(client=lambda: object()), risk_guard=RiskGuard(limits=RiskLimits(max_derivatives_leverage=3)),
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
    from dataclasses import replace

    selected = autoloop_analysis.autoloop_candles.selected_candle(None, None, None)
    forecast = replace(forecast, data_open_time=selected.open_time)
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


@pytest.mark.parametrize("closed_by", ["cancel", "deadline"])
def test_forecast_return_after_cycle_closes_does_not_publish_or_write(monkeypatch, closed_by):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="forecast")
    deadline = 2_000.0
    clock = {"now": 1_000.0}
    monkeypatch.setattr(autoloop_execution.time, "monotonic", lambda: clock["now"])
    checkpoints = []

    def forecast(*_args, **kwargs):
        assert kwargs["deadline"] == deadline
        assert kwargs["cancelled"] is cancelled
        assert checkpoints[-1]["payload"]["forecast_started"] is True
        assert "kronos_forecast" not in checkpoints[-1]["payload"]
        if closed_by == "cancel":
            cancelled.set()
        else:
            clock["now"] = deadline
        return ForecastBlock(symbol="BTC/USDT:USDT", status="unavailable", reason="late")

    def no_graph(*_args):
        pytest.fail("a closed candidate must not build the decision graph")

    monkeypatch.setattr(autoloop_futures_context, "_kronos_block_for", forecast)
    with pytest.raises(TimeoutError, match="kronos forecast"):
        autoloop_analysis._process_candidate(
            None, {"ticker": "BTC-USD"}, {}, no_graph, executor,
            deadline=deadline, cancelled=cancelled, on_cycle_checkpoint=checkpoints.append,
        )

    assert len(claims) == 1
    assert [write["phase"] for write in writes] == ["market", "context"]
    assert len(checkpoints) == 3
    assert all("kronos_forecast" not in checkpoint["payload"] for checkpoint in checkpoints)


@pytest.mark.parametrize("closed_by", ["cancel", "deadline"])
def test_checkpoint_closing_cycle_fences_forecast_writes(monkeypatch, closed_by):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="forecast")
    deadline = 2_000.0
    clock = {"now": 1_000.0}
    monkeypatch.setattr(autoloop_execution.time, "monotonic", lambda: clock["now"])
    checkpoints = []

    def checkpoint(cycle):
        checkpoints.append(cycle)
        if "kronos_forecast" in cycle["payload"]:
            if closed_by == "cancel":
                cancelled.set()
            else:
                clock["now"] = deadline

    def no_graph(*_args):
        pytest.fail("a deferred candidate must not build the decision graph")

    monkeypatch.setattr(
        autoloop_futures_context, "_kronos_block_for",
        lambda *_args, **_kwargs: ForecastBlock(
            symbol="BTC/USDT:USDT", status="unavailable", reason="no forecast",
        ),
    )
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD"}, {}, no_graph, executor,
        deadline=deadline, cancelled=cancelled, on_cycle_checkpoint=checkpoint,
    )

    assert result.reason.endswith("no forecast")
    assert len(claims) == 1
    assert [write["phase"] for write in writes] == ["market", "context"]
    assert len(checkpoints) == 4
    assert checkpoints[-1]["payload"]["kronos_policy"] == "defer"


def test_mode_check_precedes_deadline_check_after_forecast(monkeypatch):
    from server.auto_modes import AutoMode

    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="forecast")
    deadline = 2_000.0
    state = {"now": 1_000.0, "enabled": True}
    authorization = object()
    monkeypatch.setattr(autoloop_execution.time, "monotonic", lambda: state["now"])

    def mode_enabled(mode, snapshot):
        assert mode is AutoMode.FUTURES
        assert snapshot is authorization
        return state["enabled"]

    def forecast(*_args, **_kwargs):
        state.update(now=deadline, enabled=False)
        return ForecastBlock(symbol="BTC/USDT:USDT", status="unavailable", reason="closed")

    monkeypatch.setattr(autoloop_execution, "mode_is_enabled", mode_enabled)
    monkeypatch.setattr(autoloop_futures_context, "_kronos_block_for", forecast)
    with pytest.raises(autoloop_execution.AutoModeDisabled, match="kronos forecast"):
        autoloop_analysis._process_candidate(
            None, {"ticker": "BTC-USD"}, {}, lambda *_: pytest.fail("graph built"), executor,
            deadline=deadline, cancelled=cancelled, mode=AutoMode.FUTURES,
            authorization=authorization,
        )

    assert len(claims) == 1
    assert [write["phase"] for write in writes] == ["market", "context"]


def test_forecast_persist_callback_rechecks_write_fence(monkeypatch):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="forecast")
    original_writes_allowed = autoloop_execution.cycle_writes_allowed
    fence_checks = []
    after_forecast = False

    def checkpoint(cycle):
        nonlocal after_forecast
        if "kronos_forecast" in cycle["payload"]:
            after_forecast = True

    def writes_allowed(deadline, cancellation):
        if not after_forecast:
            return original_writes_allowed(deadline, cancellation)
        fence_checks.append(None)
        return len(fence_checks) % 2 == 1

    monkeypatch.setattr(autoloop_execution, "cycle_writes_allowed", writes_allowed)
    monkeypatch.setattr(
        autoloop_futures_context, "_kronos_block_for",
        lambda *_args, **_kwargs: ForecastBlock(
            symbol="BTC/USDT:USDT", status="unavailable", reason="no forecast",
        ),
    )
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD"}, {}, lambda *_: pytest.fail("graph built"), executor,
        cancelled=cancelled, on_cycle_checkpoint=checkpoint,
    )

    assert result.reason.endswith("no forecast")
    assert len(claims) == 1
    assert len(fence_checks) == 4
    assert [write["phase"] for write in writes] == ["market", "context"]


@pytest.mark.parametrize("source", ["before", "forecast", "after", "persist", "terminal_persist"])
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
def test_forecast_callback_and_persist_errors_keep_candidate_handling(monkeypatch, source, error_type):
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="forecast")
    checkpoints = []

    def checkpoint(cycle):
        checkpoints.append(cycle)
        payload = cycle["payload"]
        if payload.get("forecast_started"):
            phase = "after" if "kronos_forecast" in payload else "before"
            if source == phase:
                raise error_type("checkpoint failure")

    def forecast(*_args, **_kwargs):
        if source == "forecast":
            raise error_type("forecast failure")
        return ForecastBlock(symbol="BTC/USDT:USDT", status="unavailable", reason="no forecast")

    def persist(*_args, **kwargs):
        if (source == "persist" and kwargs.get("phase") == "context"
                and "kronos_forecast" in kwargs["payload"]):
            raise error_type("persist failure")
        if kwargs.get("phase") == "forecast" and source == "terminal_persist":
            raise error_type("terminal persist failure")
        writes.append(kwargs)

    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", persist)
    monkeypatch.setattr(autoloop_futures_context, "_kronos_block_for", forecast)
    arguments = (
        None, {"ticker": "BTC-USD"}, {}, lambda *_: pytest.fail("graph built"), executor,
    )
    if error_type is TimeoutError:
        with pytest.raises(TimeoutError, match="failure"):
            autoloop_analysis._process_candidate(
                *arguments, cancelled=cancelled, on_cycle_checkpoint=checkpoint,
            )
        phases = [write["phase"] for write in writes]
        assert phases == (["market", "context", "context"] if source == "terminal_persist"
                          else ["market", "context"])
    else:
        result = autoloop_analysis._process_candidate(
            *arguments, cancelled=cancelled, on_cycle_checkpoint=checkpoint,
        )
        assert result.reason.startswith("RuntimeError:")
        assert writes[-1]["phase"] == "analysis"
        assert writes[-1]["status"] == "failed"
        assert writes[-1]["terminal"] is True
    assert len(claims) == 1
    assert len(checkpoints) == (3 if source in {"before", "forecast"} else 4)


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


@pytest.mark.parametrize("outcome", [
    "returned", "exception", "timeout", "ack", "cancelled_before_submit", "worker_timeout",
])
def test_timeout_owner_accounts_for_inflight_futures_order(monkeypatch, outcome):
    """The owner waits for the execution result, even after the deadline."""
    cancelled = threading.Event()
    executor, claims, writes = _install(monkeypatch, cancelled, cancel_at="execution")
    started = threading.Event()
    finished = threading.Event()
    submitted = []
    plan = SimpleNamespace(
        side="buy", market="derivatives", quantity=1.0, estimated_price=100.0,
        cost=100.0, leverage=2, margin_mode="isolated", stop_loss=90.0,
        take_profit=110.0, reason=None,
    )
    marker = managed_live_provenance(
        PlannedOrder(ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", side="buy"),
        "entry-0123456789abcdef01234567", identity=VERIFIED_IDENTITY, venue="binance:demo",
    )

    def prepare_forecast(*_args, **kwargs):
        payload = kwargs["cycle_payload"]
        payload.update(forecast_started=True, kronos_forecast={"status": "ok"})
        kwargs["on_cycle_checkpoint"]({
            "cycle_id": kwargs["cycle_id"], "symbol": "BTC/USDT:USDT",
            "payload": dict(payload),
        })
        return None, ""

    def place_order(*_args, on_entry_evidence=None, check_entry_allowed=None, **_kwargs):
        if outcome in {"ack", "cancelled_before_submit"}:
            on_entry_evidence({
                "entry_intent_id": marker["intent_id"], "entry_provenance": marker,
                "execution_status": "uncertain",
            })
        if outcome != "cancelled_before_submit":
            submitted.append("entry")
        started.set()
        if outcome == "worker_timeout":
            raise TimeoutError("venue timeout before owner deadline")
        assert cancelled.wait(5), "owner did not cancel the in-flight call"
        if outcome == "cancelled_before_submit":
            check_entry_allowed()
        if outcome == "exception":
            raise RuntimeError("ambiguous venue response")
        if outcome == "timeout":
            raise TimeoutError("ambiguous venue timeout")
        if outcome == "ack":
            on_entry_evidence({
                "entry_intent_id": marker["intent_id"], "entry_provenance": marker,
                "execution_status": "uncertain", "protection_state": "unverified",
                "order_id": "venue-123",
            })
            raise RuntimeError("protection unverified")
        return "venue-123"

    executor.plan_order = lambda *_args, **_kwargs: plan
    executor.execute_order = place_order
    executor.auto_mode_enabled = lambda *_args, **_kwargs: True
    executor.effective_mode = lambda: "live"
    executor.risk_guard_decisions = [{"allowed": True, "halted": False}]
    monkeypatch.setattr(autoloop_kronos, "_prepare_forecast", prepare_forecast)
    monkeypatch.setattr(autoloop_futures_context, "_unified_mark_price", lambda *_args: 100.0)
    monkeypatch.setattr(autoloop_futures_context, "_confirmation_state", lambda *_args: (True, "aligned"))

    class Graph:
        def propagate(self, *_args, **_kwargs):
            return {
                "futures_execution_intent": "enter",
                "futures_execution_proposal": {
                    "margin_mode": "isolated", "leverage": 2,
                    "risk_fraction": 0.01, "margin_fraction": 0.2,
                    "stop_loss": 90.0, "take_profit": 110.0, "rationale": "test",
                },
            }, "Buy"

    original = autoloop_analysis._process_candidate

    def process(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
            if not started.is_set():
                raise AssertionError(f"candidate stopped before execution: {result.reason}")
            return result
        finally:
            finished.set()

    monkeypatch.setattr(autoloop_analysis, "_process_candidate", process)
    try:
        with pytest.raises(TimeoutError):
            autoloop._run_candidate_with_timeout(
                None, {"ticker": "BTC-USD"}, {}, lambda *_: Graph(), executor,
                time.monotonic() + 0.2, cancelled, threading.Lock(), CycleTelemetry(),
            )
        assert started.is_set()
        assert finished.wait(5)
        terminal = [write for write in writes if write.get("terminal")]
        assert len(claims) == 1
        assert len(terminal) == 1
        assert writes[-1] is terminal[0]
        payload = terminal[0]["payload"]
        assert payload["signal"] == "Buy"
        assert payload["plan"]["stop_loss"] == 90.0
        if outcome == "returned":
            assert payload["execution_status"] == "executed"
            assert payload["executed"] is True
            assert payload["order_id"] == "venue-123"
        elif outcome == "ack":
            assert payload["execution_status"] == "uncertain"
            assert payload.get("executed") is not False
            assert payload["order_id"] == "venue-123"
            assert payload["protection_state"] == "unverified"
        else:
            assert payload["execution_status"] == "uncertain"
            assert payload.get("executed") is not False
            assert payload.get("order_id") is None
        if outcome in {"ack", "cancelled_before_submit"}:
            assert payload["entry_intent_id"] == marker["intent_id"]
            assert payload["entry_provenance"] == marker
            prior = [write for write in writes if write["payload"].get("entry_provenance")]
            assert not prior[0].get("terminal", False)
            assert prior[0]["payload"]["entry_provenance"] == marker
        assert submitted == ([] if outcome == "cancelled_before_submit" else ["entry"])
    finally:
        cancelled.set()
        assert finished.wait(5)


@pytest.mark.parametrize("failed_write", ["started", "terminal"])
def test_cycle_persistence_failure_does_not_erase_successful_order(monkeypatch, failed_write):
    cancelled = threading.Event()
    executor, _claims, writes = _install(monkeypatch, cancelled, cancel_at="execution")
    executor.plan_order = lambda *_args, **_kwargs: SimpleNamespace(
        side="buy", market="derivatives", quantity=1.0, estimated_price=100.0,
        cost=100.0, leverage=2, margin_mode="isolated", stop_loss=90.0,
        take_profit=110.0, reason=None,
    )
    executor.execute_order = lambda *_args, **_kwargs: "venue-456"
    executor.auto_mode_enabled = lambda *_args, **_kwargs: True
    executor.effective_mode = lambda: "live"

    def prepare_forecast(*_args, **kwargs):
        kwargs["cycle_payload"].update(forecast_started=True, kronos_forecast={"status": "ok"})
        return None, ""

    def persist(*_args, **kwargs):
        if (failed_write == "started" and kwargs.get("phase") == "execution"
                and not kwargs.get("terminal")
                and kwargs["payload"].get("execution_status") == "in_progress"):
            raise OSError("write unavailable")
        if failed_write == "terminal" and kwargs.get("status") == "completed":
            raise OSError("terminal write unavailable")
        writes.append(kwargs)

    monkeypatch.setattr(futures_cycle_telemetry, "persist_progress", persist)
    monkeypatch.setattr(autoloop_kronos, "_prepare_forecast", prepare_forecast)
    monkeypatch.setattr(autoloop_futures_context, "_unified_mark_price", lambda *_args: 100.0)
    monkeypatch.setattr(autoloop_futures_context, "_confirmation_state", lambda *_args: (True, "aligned"))

    class Graph:
        def propagate(self, *_args, **_kwargs):
            return {"futures_execution_intent": "enter", "futures_execution_proposal": {
                "margin_mode": "isolated", "leverage": 2, "risk_fraction": 0.01,
                "margin_fraction": 0.2, "stop_loss": 90.0, "take_profit": 110.0, "rationale": "test",
            }}, "Buy"

    checkpoints = []
    result = autoloop_analysis._process_candidate(
        None, {"ticker": "BTC-USD"}, {}, lambda *_: Graph(), executor,
        cancelled=cancelled, on_cycle_checkpoint=checkpoints.append,
    )
    if failed_write == "started":
        assert result.executed is False
    else:
        assert result.executed is True
        assert result.order_id == "venue-456"
        assert checkpoints[-1]["payload"]["execution_status"] == "executed"
        assert checkpoints[-1]["payload"]["order_id"] == "venue-456"


def test_cancelled_or_disabled_execution_cannot_start_an_order(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    submitted = []
    with pytest.raises(TimeoutError):
        autoloop_execution.execute_with_deadline(
            lambda: submitted.append("entry"), None, cancelled, threading.Lock(),
        )
    cancelled.clear()
    monkeypatch.setattr(autoloop_execution, "mode_is_enabled", lambda *_args: False)

    with pytest.raises(autoloop_execution.AutoModeDisabled):
        autoloop_execution.execute_with_deadline(
            lambda: submitted.append("entry"), None, cancelled, threading.Lock(), AutoMode.FUTURES,
        )
    assert submitted == []
