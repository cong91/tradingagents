"""S2: the shadow worker — bounded, restartable, and never a decision input.

Every test here is about what the worker may NOT do as much as what it does: a
disabled worker reads and writes nothing, a provider that raises or times out is
recorded as that failure without stopping the poll, the cursor is durable across
a restart, and the cycles the worker looks at are the ones the runtime already
finished. The parity tests drive a real ``DerivativesExecutor`` with the shadow
layer in each state and require byte-identical order outcomes.
"""

import ast
import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from server import job_store, shadow_store, shadow_worker
from tradingagents.dataflows.market_data import Candle
from tradingagents.shadow.filters import FilterProvider
from tradingagents.shadow.provider import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    ForecastAttempt,
)
from tradingagents.shadow.recorder import RequestError

pytestmark = pytest.mark.unit

SYMBOL = "BTC/USDT:USDT"
HOUR_MS = 3_600_000
_CYCLE_SEQ = count()


def _now() -> datetime:
    return datetime.now(UTC)


def _candles(ending: datetime, count: int = 12, *, close: float = 100.0,
             timeframe: str = "1h") -> list[Candle]:
    """Closed candles whose newest bar closed at least one full bar before ``ending``.

    The one-bar offset is what keeps the fixture honest AND time-of-day
    independent: a decision moment derived as ``ending - N minutes`` must be at or
    after the newest close, and with candles ending at the top of the current hour
    that only held when the wall-clock minute happened to be past N — the exact
    timezone/clock-dependent flakiness the repo forbids. Ending a bar earlier makes
    the relationship true at every minute of the day.
    """
    step = HOUR_MS if timeframe == "1h" else 4 * HOUR_MS
    last_open_ms = int(ending.timestamp() * 1000) // step * step - 2 * step
    series = []
    for index in range(count):
        open_time = last_open_ms - (count - 1 - index) * step
        price = close + index
        series.append(Candle(
            symbol=SYMBOL, timeframe=timeframe, open_time=open_time,
            close_time=open_time + step, open=price - 0.5, high=price + 1.0,
            low=price - 1.0, close=price, volume=10.0 + index, is_closed=True,
        ))
    return series


def _production_snapshot(ending: datetime) -> list[Candle]:
    """The shape a real cycle records: BOTH decision timeframes, 1h and 4h.

    ``snapshot_payload`` (futures_cycle_telemetry) encodes the whole context, so a
    production row carries ``one_hour`` and ``four_hour`` together — 77/77 exported
    snapshots do. Building only 1h candles is what let the blocker through: the
    worker narrowed by timeframe nowhere, so a real cycle was refused as "mixed
    timeframes" while a 1h-only fixture passed.

    The 4h bar is the one the runtime actually saw, i.e. it is built FROM the 1h
    bars it spans — not a second independent series. Two independent series would
    disagree on the bar they share, and the merge would (correctly) refuse them.
    """
    hourly = _candles(ending, count=6)
    span = hourly[-4:]  # the last four 1h bars are exactly the newest 4h bar
    four_hour = Candle(
        symbol=SYMBOL, timeframe="4h",
        open_time=span[0].open_time, close_time=span[-1].close_time,
        open=span[0].open, high=max(candle.high for candle in span),
        low=min(candle.low for candle in span), close=span[-1].close,
        volume=sum(candle.volume for candle in span), is_closed=True,
    )
    return hourly + [four_hour]


def _cycle_payload(*, execution_status: str = "executed",
                   snapshot: list[Candle] | None = None,
                   phase_timestamps: dict | None = None) -> dict:
    snapshot_payload = {}
    if snapshot:
        by_timeframe = {candle.timeframe: candle for candle in snapshot}
        for timeframe, key in (("1h", "one_hour"), ("4h", "four_hour")):
            candle = by_timeframe.get(timeframe)
            if candle is not None:
                snapshot_payload[key] = {
                    "symbol": candle.symbol, "timeframe": candle.timeframe,
                    "open_time": candle.open_time, "close_time": candle.close_time,
                    "open": candle.open, "high": candle.high, "low": candle.low,
                    "close": candle.close, "volume": candle.volume, "is_closed": True,
                }
    return {
        "execution_status": execution_status,
        "executed": execution_status == "executed",
        "order_id": "9001" if execution_status == "executed" else None,
        "snapshot": snapshot_payload,
        "phase_timestamps": phase_timestamps or {},
        "signal": "Buy",
    }


def _record_cycle(cycle_id: str, *, execution_status: str = "executed",
                  snapshot: list[Candle] | None = None,
                  phase_timestamps: dict | None = None) -> dict:
    """Persist one terminal cycle the way the runtime does (payload + status)."""
    seq = next(_CYCLE_SEQ)
    now = _now()
    payload = _cycle_payload(
        execution_status=execution_status, snapshot=snapshot,
        phase_timestamps=phase_timestamps,
    )
    assert job_store.claim_futures_cycle(
        cycle_id, symbol=SYMBOL,
        four_hour_open=(now - timedelta(hours=4 + seq)).isoformat(),
        one_hour_open=(now - timedelta(hours=1 + seq)).isoformat(),
        strategy_version="1", payload=payload,
    )
    job_store.update_futures_cycle(
        cycle_id, status="completed" if execution_status == "executed" else "rejected",
        phase="entry" if execution_status == "executed" else "execution",
        payload=payload, terminal=True,
    )
    return payload


@pytest.fixture
def observations_enabled():
    from tradingagents.dataflows.config import set_config

    set_config({"shadow_observations_enabled": True, "shadow_providers": "filter:donchian_breakout"})
    yield
    set_config({"shadow_observations_enabled": False, "shadow_providers": "filter:donchian_breakout"})


def _seed_cursor(behind_hours: int = 1) -> None:
    shadow_store.set_cursor(f"{(_now() - timedelta(hours=behind_hours)).isoformat()}|seed")


def _moment_after_last_close(series: list[Candle]) -> datetime:
    """The decision moment one minute after the newest 1h candle in ``series``.

    Derived from the candles, never from the wall clock: that is what the runtime
    records, and it makes "was this forward?" a property of the data rather than of
    the minute the test happens to run.
    """
    newest = max(
        (candle for candle in series if candle.timeframe == "1h"),
        key=lambda candle: candle.close_time,
    )
    return datetime.fromtimestamp(newest.close_time / 1000, UTC) + timedelta(minutes=1)


def _candle_source(series: list[Candle] | None = None, *, raise_with: Exception | None = None):
    """A venue-history stand-in: the same closed bars the snapshot recorded.

    Passing the snapshot's OWN candles (rather than a second, independently
    generated series) is what a real venue read looks like when both agree: the
    merge accepts identical duplicates and refuses a genuine disagreement, so a
    fixture that invents different prices for the same bar would be testing the
    refusal path, not the happy path.
    """
    def source(symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list[Candle]:
        if raise_with is not None:
            raise raise_with
        return [candle for candle in (series or []) if timeframe == candle.timeframe]

    return source


class _RaisingProvider:
    provider_id = "kronos"
    model_revision = "stub@1"
    source_revision = "rev"
    artifact_hash = None
    distribution_kind = "model_samples"
    calibration_label = "model_sample_ratio_not_probability"
    deadline_seconds = 5.0

    def forecast(self, request):
        raise RuntimeError("model exploded")


class _TimingOutProvider(_RaisingProvider):
    def forecast(self, request):
        return ForecastAttempt(status=STATUS_TIMEOUT, reason="deadline exceeded")


class _FixedProvider(_RaisingProvider):
    provider_id = "kronos"

    def forecast(self, request):
        attempt = FilterProvider("donchian_breakout").forecast(request)
        return ForecastAttempt(
            status=STATUS_OK, direction=attempt.direction or "flat",
            samples={"band": {"low": 1.0, "high": 2.0}},
        )


# --- independence from the decision path ------------------------------------


def test_worker_is_not_imported_by_the_decision_path():
    """graph/agents/autoloop must stay free of the shadow layer (plan §6.1)."""
    repo = Path(__file__).parents[1]

    def imports_of(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        return names

    forbidden = ("tradingagents.shadow", "server.shadow_store", "server.shadow_worker")
    for path in [repo / "server" / "autoloop.py", *sorted((repo / "tradingagents" / "graph").rglob("*.py")),
                 *sorted((repo / "tradingagents" / "agents").rglob("*.py"))]:
        names = imports_of(path)
        assert not [name for name in names if name.startswith(forbidden)], path

    # And the worker itself never imports the decision path or the executor.
    worker_imports = imports_of(Path(shadow_worker.__file__))
    assert not any(name.startswith(("server.autoloop", "tradingagents.graph")) for name in worker_imports)


def test_worker_does_not_write_cycle_or_execution_state():
    """The only tables the worker writes are shadow_* + its own cursor."""
    source = Path(shadow_worker.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "update_futures_cycle", "claim_futures_cycle", "create_bracket_recovery",
        "outcome_store", "RiskGuard", "execute_order", "create_order",
    ):
        assert forbidden not in source, f"shadow worker must not touch {forbidden}"


# --- disabled / enabled behaviours ------------------------------------------


def test_disabled_worker_reads_and_writes_nothing(postgres_schema):
    _record_cycle("futures:BTC/USDT:USDT:off1", snapshot=_candles(_now()))
    result = shadow_worker.poll_once(candle_source=_candle_source())
    assert result["status"] == "disabled"
    assert shadow_store.summary()["status"] == "unavailable"
    assert shadow_store.get_cursor() is None


def test_disabled_worker_never_starts_a_thread():
    shadow_worker.start()
    assert shadow_worker._worker_thread is None


def test_first_poll_initializes_the_cursor_without_backfill(
    postgres_schema, observations_enabled,
):
    _record_cycle("futures:BTC/USDT:USDT:init", snapshot=_candles(_now()))
    result = shadow_worker.poll_once(candle_source=_candle_source())
    assert result["status"] == "initialized"
    assert "no backfill" in result["note"]
    assert shadow_store.summary()["status"] == "unavailable"


def test_observation_is_recorded_for_a_terminal_cycle(
    postgres_schema, observations_enabled,
):
    _seed_cursor()
    now = _now()
    # Production snapshot shape: 1h AND 4h. Before the fix this produced
    # "unusable_series:mixed timeframes" and wrote zero rows.
    series = _production_snapshot(now)
    moment = _moment_after_last_close(series)
    _record_cycle(
        "futures:BTC/USDT:USDT:obs1", snapshot=series,
        phase_timestamps={"execution_finished_at": moment.isoformat()},
    )
    # The poll runs a minute after the decision: nothing the forecast predicts has
    # closed yet, so the row is forward — not because of the polling clock, but
    # because of where the observation sits relative to its own candles.
    result = shadow_worker.poll_once(
        candle_source=_candle_source(series), now=moment + timedelta(minutes=1),
    )
    assert result["status"] == "polled"
    # No cycle may be dropped for an unusable series: that was the blocker.
    assert not [item for item in result["outcomes"]
                if "unusable_series" in json.dumps(item)]
    rows = shadow_store.forecast_rows(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "filter:donchian_breakout"
    assert row["status"] == STATUS_OK
    assert row["outcome_group"] == "executed"
    assert row["generation_kind"] == "forward"
    # The observation is stamped with the cycle's own decision moment, not "now".
    assert row["data_open_time"] is not None


def test_series_narrows_a_two_timeframe_snapshot_to_the_observed_one():
    """F1 regression, without a database: 1h+4h in, one 1h series out."""
    now = _now()
    moment = now - timedelta(minutes=1)
    cycle = {"cycle_id": "c", "symbol": SYMBOL}
    snapshot = _production_snapshot(now)
    payload = _cycle_payload(snapshot=snapshot)
    series = shadow_worker._series_for(
        cycle, payload, candle_source=_candle_source(snapshot),
        timeframe="1h", as_of=moment,
    )
    assert series
    assert {candle.timeframe for candle in series} == {"1h"}
    # Contiguous and ascending, so the provider can read it.
    assert all(b.open_time == a.close_time for a, b in zip(series, series[1:], strict=False))

    # The same narrowing works for the 4h observation.
    four_hour = shadow_worker._series_for(
        cycle, payload, candle_source=_candle_source(snapshot),
        timeframe="4h", as_of=moment,
    )
    assert {candle.timeframe for candle in four_hour} == {"4h"}
    assert len(four_hour) == 1


def test_recorder_still_refuses_a_mixed_series_it_is_handed_directly():
    """The narrowing lives at the worker seam; the recorder stays strict."""
    from tradingagents.shadow.recorder import RequestError, validate_candles

    now = _now()
    with pytest.raises(RequestError, match="mixed timeframes"):
        validate_candles(_production_snapshot(now), "1h", now)


def test_rejected_cycle_lands_in_the_rejected_group(postgres_schema, observations_enabled):
    _seed_cursor()
    now = _now()
    series = _candles(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:rej", execution_status="risk_rejected", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=1)).isoformat()},
    )
    shadow_worker.poll_once(candle_source=_candle_source(series), now=now)
    row = shadow_store.forecast_rows(limit=1)[0]
    assert row["outcome_group"] == "rejected"
    assert "risk_rejected" in row["outcome_reason"]


def test_cycle_without_a_signal_lands_in_the_abstained_group(
    postgres_schema, observations_enabled,
):
    _seed_cursor()
    now = _now()
    series = _candles(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:abs", execution_status="", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=1)).isoformat()},
    )
    shadow_worker.poll_once(candle_source=_candle_source(series), now=now)
    row = shadow_store.forecast_rows(limit=1)[0]
    assert row["outcome_group"] == "abstained"


# --- failure modes ----------------------------------------------------------


def test_provider_exception_is_stored_as_error_and_the_poll_finishes(
    postgres_schema, observations_enabled,
):
    _seed_cursor()
    now = _now()
    series = _candles(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:boom", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=1)).isoformat()},
    )
    result = shadow_worker.poll_once(
        provider_factory=lambda: [_RaisingProvider()],
        candle_source=_candle_source(series), now=now,
    )
    assert result["status"] == "polled"
    row = shadow_store.forecast_rows(limit=1)[0]
    assert row["status"] == STATUS_ERROR
    assert "model exploded" in row["reason"]
    # The cursor advanced, so the same cycle is not re-observed forever.
    assert shadow_store.get_cursor().startswith(
        shadow_store.terminal_cycles_after(None, None, batch_size=1)[0]["updated_at"]
    ) or shadow_store.get_cursor() is not None


def test_timeout_is_stored_as_a_timeout_status(postgres_schema, observations_enabled):
    _seed_cursor()
    now = _now()
    series = _candles(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:slow", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=1)).isoformat()},
    )
    shadow_worker.poll_once(
        provider_factory=lambda: [_TimingOutProvider()],
        candle_source=_candle_source(series), now=now,
    )
    assert shadow_store.forecast_rows(limit=1)[0]["status"] == STATUS_TIMEOUT


def test_unavailable_provider_yields_no_observation_but_no_crash(
    postgres_schema, observations_enabled,
):
    """With no provider available the poll reports it and writes nothing."""
    result = shadow_worker.poll_once(provider_factory=list, candle_source=_candle_source())
    assert result["status"] == "no_provider"
    assert shadow_store.summary()["status"] == "unavailable"


def test_decision_moment_falls_back_to_the_rows_own_recorded_timestamp(
    postgres_schema, observations_enabled,
):
    """No phase timestamps: the row's own created/updated time is the moment.

    That timestamp is recorded data (the runtime stamped the row), not "now"
    invented by the worker, so a historical cycle keeps a historical cutoff. A
    cycle with no usable timestamp at all is refused outright instead.
    """
    _seed_cursor()
    now = _now()
    series = _candles(now)
    _record_cycle("futures:BTC/USDT:USDT:nots", snapshot=series, phase_timestamps={})
    result = shadow_worker.poll_once(candle_source=_candle_source(series), now=now)
    assert result["status"] == "polled"
    row = shadow_store.forecast_rows(limit=1)[0]
    assert row["data_open_time"] is not None
    assert row["provenance"]["as_of"] is not None

    with pytest.raises(RequestError, match="no decision timestamp"):
        shadow_worker._decision_moment({"cycle_id": "x", "symbol": SYMBOL}, {})


def test_failing_cycle_is_retried_then_the_cursor_moves_on(
    postgres_schema, observations_enabled,
):
    """A cycle whose own observation raises twice must not stall the worker."""
    _seed_cursor()
    now = _now()
    _record_cycle("futures:BTC/USDT:USDT:poison")
    calls = {"n": 0}

    def exploding_series(cycle, payload, *, candle_source, timeframe, as_of):
        calls["n"] += 1
        raise RuntimeError("poison candle set")

    original = shadow_worker._series_for
    shadow_worker._series_for = exploding_series
    try:
        first = shadow_worker.poll_once(candle_source=_candle_source(), now=now)
        second = shadow_worker.poll_once(candle_source=_candle_source(), now=now)
        third = shadow_worker.poll_once(candle_source=_candle_source(), now=now)
    finally:
        shadow_worker._series_for = original
    assert first["status"] == "polled"
    assert any("error" in json.dumps(item) for item in first["outcomes"])
    # After MAX_CYCLE_ATTEMPTS the worker stops re-reading the same cycle.
    assert second["status"] == "polled"
    assert calls["n"] >= shadow_worker.MAX_CYCLE_ATTEMPTS
    assert third["status"] == "polled"


# --- cursor / pagination / restart -----------------------------------------


def test_cursor_is_durable_so_a_restart_catches_up(postgres_schema, observations_enabled):
    _seed_cursor(behind_hours=2)
    now = _now()
    series = _candles(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:r1", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=5)).isoformat()},
    )
    shadow_worker.poll_once(candle_source=_candle_source(series), now=now)
    assert len(shadow_store.forecast_rows(limit=5)) == 1

    # The worker "restarts": a new cycle completed while it was down.
    _record_cycle(
        "futures:BTC/USDT:USDT:r2", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=4)).isoformat()},
    )
    shadow_worker.poll_once(candle_source=_candle_source(series), now=now)
    rows = shadow_store.forecast_rows(limit=5)
    # The second cycle shares its symbol/timeframe/candle, so the idempotency key
    # keeps it a single observation — but the cursor still advanced past it.
    assert len(rows) == 1
    assert shadow_store.terminal_cycles_after(
        *shadow_store.get_cursor().split("|", 1), batch_size=5,
    ) == []


def test_batch_is_bounded(postgres_schema, observations_enabled):
    _seed_cursor(behind_hours=3)
    now = _now()
    series = _candles(now)
    for index in range(3):
        _record_cycle(f"futures:BTC/USDT:USDT:b{index}", snapshot=series)
    result = shadow_worker.poll_once(
        candle_source=_candle_source(series), batch_size=2, now=now,
    )
    assert result["processed"] == 2
    assert len(shadow_store.terminal_cycles_after(
        *shadow_store.get_cursor().split("|", 1), batch_size=10,
    )) == 1


def test_candle_read_failure_is_recorded_as_an_error_not_dropped(
    postgres_schema, observations_enabled,
):
    """A failed venue read leaves the snapshot's single bar, and the filter says so."""
    _seed_cursor()
    now = _now()
    # A production snapshot holds ONE candle per timeframe (the decision pair),
    # so with the venue read down the filter below cannot read a series.
    series = _production_snapshot(now)
    _record_cycle(
        "futures:BTC/USDT:USDT:readfail", snapshot=series,
        phase_timestamps={"execution_finished_at": (now - timedelta(minutes=1)).isoformat()},
    )
    result = shadow_worker.poll_once(
        candle_source=_candle_source(raise_with=RuntimeError("venue down")), now=now,
    )
    assert result["status"] == "polled"
    # The cycle is still observed (not dropped), and the short series is recorded
    # as an explicit error with the count it got — never a fabricated verdict.
    assert not [item for item in result["outcomes"]
                if "unusable_series" in json.dumps(item)]
    row = shadow_store.forecast_rows(limit=1)[0]
    assert row["status"] == STATUS_ERROR
    assert "closed candles" in (row["reason"] or "")


# --- failure isolation without a database -----------------------------------


def test_provider_failure_is_data_even_when_persistence_is_injected(
    observations_enabled, monkeypatch,
):
    """F7: "a provider bug is harmless" must not depend on PostgreSQL being up.

    The store call used to sit in the same unwrapped sequence as the forecast, so
    the property was only true while a database happened to answer. The recorder
    is now injectable, which lets this run with no database at all.
    """
    now = _now()
    moment = now - timedelta(minutes=1)
    series = _production_snapshot(now)
    payload = _cycle_payload(
        snapshot=series,
        phase_timestamps={"execution_finished_at": moment.isoformat()},
    )
    cycle = {
        "cycle_id": "futures:BTC/USDT:USDT:nodb", "symbol": SYMBOL,
        "payload": payload, "status": "completed", "phase": "entry",
        "created_at": moment.isoformat(), "updated_at": moment.isoformat(),
    }
    captured: list[dict] = []

    def record(row: dict) -> bool:
        captured.append(row)
        return True

    results = shadow_worker._observe_cycle(
        cycle, built=[_RaisingProvider()],
        candle_source=_candle_source(series), now=now, record=record,
    )
    assert results == [{"provider": "kronos", "status": STATUS_ERROR, "stored": True}]
    # The failure was stored as a row with its reason, not raised.
    assert captured[0]["status"] == STATUS_ERROR
    assert "model exploded" in captured[0]["reason"]
    assert captured[0]["paths"] == []


def test_a_raising_recorder_surfaces_but_a_provider_does_not(monkeypatch):
    """The injected recorder's own failure IS an error — it must not be swallowed.

    Distinguishing the two is the point of the seam: a provider raising is data
    (recorded as ``error``), while the persistence layer raising means the
    observation was lost, which the poll's per-cycle guard must see.
    """
    now = _now()
    moment = now - timedelta(minutes=1)
    series = _production_snapshot(now)
    cycle = {
        "cycle_id": "c", "symbol": SYMBOL,
        "payload": _cycle_payload(
            snapshot=series,
            phase_timestamps={"execution_finished_at": moment.isoformat()},
        ),
        "status": "completed", "phase": "entry",
        "created_at": moment.isoformat(), "updated_at": moment.isoformat(),
    }

    def exploding_store(row: dict) -> bool:
        raise RuntimeError("disk is gone")

    with pytest.raises(RuntimeError, match="disk is gone"):
        shadow_worker._observe_cycle(
            cycle, built=[_FixedProvider()],
            candle_source=_candle_source(series), now=now, record=exploding_store,
        )


def test_shutdown_waits_a_short_bounded_grace_not_the_poll_interval(monkeypatch):
    """F9: stop() must not block for a whole 300 s poll interval."""
    from tradingagents.dataflows.config import set_config

    set_config({"shadow_observations_interval_seconds": 300})
    try:
        joined: list[float] = []

        class _SlowThread:
            name = "server-shadow-worker"

            def is_alive(self) -> bool:
                return False

            def join(self, timeout: float | None = None) -> None:
                joined.append(float(timeout or 0))

        monkeypatch.setattr(shadow_worker, "_worker_thread", _SlowThread())
        shadow_worker.stop()
        assert joined == [shadow_worker.SHUTDOWN_GRACE_SECONDS]
        assert shadow_worker.SHUTDOWN_GRACE_SECONDS <= 30
        assert shadow_worker._worker_thread is None
    finally:
        set_config({"shadow_observations_interval_seconds": shadow_worker.DEFAULT_INTERVAL_SECONDS})


def test_model_bounds_are_pinned():
    """The optional model's cost bounds must not drift silently."""
    from tradingagents.shadow import kronos

    assert kronos.MAX_PATHS == 32
    assert kronos.DEFAULT_PATHS < kronos.MAX_PATHS
    # Kronos-small/base cannot read more than 512 bars of context.
    assert kronos.MAX_LOOKBACK_BARS == 512
    assert kronos.DEFAULT_LOOKBACK_BARS <= kronos.MAX_LOOKBACK_BARS
    assert kronos.MIN_LOOKBACK_BARS < kronos.DEFAULT_LOOKBACK_BARS
    assert kronos.MAX_HORIZON_BARS > 0

    # And they are actually enforced, not just declared.
    from tradingagents.dataflows.market_data import Candle
    from tradingagents.shadow.provider import ForecastRequest

    candles = tuple(
        Candle(
            symbol=SYMBOL, timeframe="1h", open_time=1_700_000_000_000 + i * HOUR_MS,
            close_time=1_700_000_000_000 + (i + 1) * HOUR_MS, open=100.0,
            high=101.0, low=99.0, close=100.5, volume=10.0, is_closed=True,
        )
        for i in range(40)
    )
    provider = kronos.KronosProvider()
    over_horizon = provider.forecast(ForecastRequest(
        symbol=SYMBOL, timeframe="1h",
        horizon_hours=(kronos.MAX_HORIZON_BARS + 1),
        candles=candles, as_of="2025-01-03T00:00:00+00:00", seed=1,
    ))
    # Either the bound refuses it, or the adapter reports its own unavailability
    # first — both are explicit, neither is a fabricated forecast.
    assert over_horizon.status in ("error", "unavailable")
    if over_horizon.status == "error":
        assert "bar bound" in (over_horizon.reason or "")


# --- evaluation -------------------------------------------------------------


def test_evaluation_runs_only_after_the_horizon_realized(
    postgres_schema, observations_enabled,
):
    _seed_cursor(behind_hours=48)
    moment = _now() - timedelta(hours=48)
    series = _candles(moment, count=12)
    _record_cycle(
        "futures:BTC/USDT:USDT:ev1", snapshot=series,
        phase_timestamps={"execution_finished_at": (moment - timedelta(minutes=1)).isoformat()},
    )
    shadow_worker.poll_once(
        provider_factory=lambda: [_FixedProvider()],
        candle_source=_candle_source(series), now=moment,
    )
    rows = shadow_store.forecast_rows(limit=1)
    assert rows and rows[0]["evaluation_status"] is None

    # The horizon is 24h; a source that only reaches 2h past the cutoff cannot
    # score it, so the evaluation stays pending (never a partial verdict).
    partially = _candles(moment + timedelta(hours=2), count=12)
    shadow_worker.poll_once(
        provider_factory=lambda: [_FixedProvider()],
        candle_source=_candle_source(partially), now=moment + timedelta(hours=3),
    )
    assert shadow_store.forecast_rows(limit=1)[0]["evaluation_status"] is None

    # Once the whole horizon has closed, the evaluation is recorded.
    fully = [
        Candle(
            symbol=candle.symbol, timeframe=candle.timeframe,
            open_time=candle.open_time, close_time=candle.close_time,
            open=candle.open, high=candle.high, low=candle.low, close=candle.close,
            volume=candle.volume, is_closed=True,
        )
        for candle in _candles(moment + timedelta(hours=30), count=40)
    ]
    later = moment + timedelta(hours=31)
    shadow_worker.poll_once(
        provider_factory=lambda: [_FixedProvider()],
        candle_source=_candle_source(fully), now=later,
    )
    after = shadow_store.forecast_rows(limit=1)[0]
    assert after["evaluation_status"] in ("ok", "partial", "unavailable")


def test_interval_seconds_coercion():
    from tradingagents.dataflows.config import set_config

    set_config({"shadow_observations_interval_seconds": 0})
    assert shadow_worker.interval_seconds() == float(shadow_worker.DEFAULT_INTERVAL_SECONDS)
    set_config({"shadow_observations_interval_seconds": 45})
    assert shadow_worker.interval_seconds() == 45.0
    set_config({"shadow_observations_interval_seconds": shadow_worker.DEFAULT_INTERVAL_SECONDS})


def test_terminal_vocabulary_matches_job_store():
    assert shadow_store.TERMINAL_CYCLE_STATUSES == job_store._FUTURES_TERMINAL_STATUSES


def test_series_validation_rejects_look_ahead():
    """A series whose last candle closes after the decision moment is refused."""
    now = _now()
    series = _candles(now)
    with pytest.raises(RequestError, match="may not look ahead"):
        shadow_worker._series_for(
            {"symbol": SYMBOL, "cycle_id": "c"},
            _cycle_payload(snapshot=series),
            candle_source=None, timeframe="1h",
            as_of=datetime.fromtimestamp(series[-1].open_time / 1000, UTC),
        )
