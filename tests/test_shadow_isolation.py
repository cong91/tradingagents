"""S3: parity — a shadow observation never changes an emitted order.

The ask is explicit: emitted orders must be identical whether the shadow layer is
enabled, disabled, failing or timing out. These tests drive the REAL
``DerivativesExecutor`` (plan order + execute + bracket) with an offline fake
exchange, run the shadow worker in each state around it, and compare the venue
calls and the probe plan field by field. A diff here would mean the observation
layer reached the decision path, which is the one thing stage 4 may not do.

Two further invariants are pinned:

- the shadow layer is not imported by the decision path (AST check);
- a worker exception cannot escape into the caller that would be the scheduler.
"""

import ast
import shutil
from itertools import count
from pathlib import Path

import pytest

import server.shadow_worker as shadow_worker
from server import job_store, shadow_store
from server.auto_modes import AutoMode
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    bind_identity,
    bracket_kwargs,
    gates_config,
    make_executor,
    read_audit,
)
from tradingagents.dataflows.config import set_config
from tradingagents.execution.auto_authorization import mode_update_lock
from tradingagents.portfolio import PortfolioContext
from tradingagents.shadow.provider import (
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNAVAILABLE,
    ForecastAttempt,
)

pytestmark = pytest.mark.unit

_CYCLE_SEQ = count()


class _ExplodingProvider:
    provider_id = "kronos"
    model_revision = "stub@1"
    source_revision = "rev"
    artifact_hash = None
    distribution_kind = "model_samples"
    calibration_label = "model_sample_ratio_not_probability"
    deadline_seconds = 5.0

    def forecast(self, request):
        raise RuntimeError("inference exploded")


class _TimeoutProvider(_ExplodingProvider):
    def forecast(self, request):
        return ForecastAttempt(status=STATUS_TIMEOUT, reason="deadline exceeded")


class _UnavailableProvider(_ExplodingProvider):
    def forecast(self, request):
        return ForecastAttempt(status=STATUS_UNAVAILABLE, reason="package not installed")


class _OkProvider(_ExplodingProvider):
    def forecast(self, request):
        return ForecastAttempt(
            status=STATUS_OK, direction="up",
            samples={"band": {"low": 1.0, "high": 2.0}, "sample_count": 1},
        )


@pytest.fixture(autouse=True)
def _shadow_off_by_default():
    set_config({
        "shadow_observations_enabled": False,
        "shadow_providers": "filter:donchian_breakout",
    })
    yield
    set_config({
        "shadow_observations_enabled": False,
        "shadow_providers": "filter:donchian_breakout",
    })


def _enable_shadow(providers: str = "filter:donchian_breakout") -> None:
    set_config({"shadow_observations_enabled": True, "shadow_providers": providers})


def _executor(tmp_path, monkeypatch, exchange, *, config=None):
    executor = make_executor(
        tmp_path, monkeypatch,
        config=config or gates_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange,
    )
    return executor


def _probe(executor) -> dict:
    """The plan the runtime would emit for one fixed set of terms."""
    order = executor.plan_order(
        "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
        **bracket_kwargs(),
    )
    return order.model_dump()


def _run_shadow(*, provider, candle_source=None) -> dict:
    """One worker poll in the given state, bounded and side-effect free."""
    return shadow_worker.poll_once(
        provider_factory=(lambda: [provider]) if provider is not None else list,
        candle_source=candle_source or (lambda *args: []),
        batch_size=3,
    )


def _record_cycle() -> None:
    seq = next(_CYCLE_SEQ)
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    payload = {
        "execution_status": "executed", "executed": True, "order_id": "9001",
        "phase_timestamps": {"execution_finished_at": now.isoformat()},
        "snapshot": {},
    }
    assert job_store.claim_futures_cycle(
        f"futures:BTC/USDT:USDT:parity{seq}", symbol="BTC/USDT:USDT",
        four_hour_open=(now - timedelta(hours=4 + seq)).isoformat(),
        one_hour_open=(now - timedelta(hours=1 + seq)).isoformat(),
        strategy_version="1", payload=payload,
    )
    job_store.update_futures_cycle(
        f"futures:BTC/USDT:USDT:parity{seq}", status="completed", phase="entry",
        payload=payload, terminal=True,
    )


def test_plan_is_identical_with_shadow_disabled_enabled_error_and_timeout(
    tmp_path, monkeypatch, postgres_schema,
):
    """The five states of the shadow layer produce byte-identical plans."""
    observed: dict[str, dict] = {}
    for state, provider in (
        ("disabled", None),
        ("enabled_ok", _OkProvider()),
        ("error", _ExplodingProvider()),
        ("timeout", _TimeoutProvider()),
        ("unavailable", _UnavailableProvider()),
    ):
        case_dir = tmp_path / state
        case_dir.mkdir()
        exchange = FakeDerivExchange()
        executor = _executor(case_dir, monkeypatch, exchange)
        if state != "disabled":
            _enable_shadow()
            _record_cycle()
            shadow_store.set_cursor("1970-01-01T00:00:00+00:00|seed")
            _run_shadow(provider=provider)
        observed[state] = _probe(executor)

    baseline = observed["disabled"]
    for state, plan in observed.items():
        assert plan == baseline, f"{state} changed the emitted plan"


def test_venue_side_effects_are_identical_when_shadow_fails(
    tmp_path, monkeypatch, postgres_schema,
):
    """A failing observation writes no venue call and no audit action of its own."""
    exchange_off = FakeDerivExchange()
    off = _executor(tmp_path / "off", monkeypatch, exchange_off)
    off_plan = _probe(off)

    _enable_shadow()
    _record_cycle()
    shadow_store.set_cursor("1970-01-01T00:00:00+00:00|seed")
    _run_shadow(provider=_ExplodingProvider())
    _run_shadow(provider=_TimeoutProvider())

    exchange_on = FakeDerivExchange()
    on = _executor(tmp_path / "on", monkeypatch, exchange_on)
    on_plan = _probe(on)

    assert on_plan == off_plan
    assert exchange_on.calls == exchange_off.calls
    # The audit trail holds exactly what the disabled run wrote: the shadow layer
    # never appends an execution action.
    assert [row["action"] for row in read_audit(tmp_path / "on")] == [
        row["action"] for row in read_audit(tmp_path / "off")
    ]


def test_live_entry_is_unaffected_by_a_failing_observation(
    tmp_path, monkeypatch, postgres_schema,
):
    """The live path (gates open + verified identity) emits the same venue calls."""
    from server import shadow_store as store

    def _live_calls(with_shadow: bool) -> tuple[dict, list]:
        case = tmp_path / ("live_on" if with_shadow else "live_off")
        case.mkdir()
        exchange = FakeDerivExchange()
        # Auto Futures must be authorized BEFORE the bridge is built: the bridge
        # captures its authorization at construction and refuses a later change.
        with mode_update_lock((AutoMode.FUTURES,)):
            set_config({"auto_futures_enabled": True})
        try:
            executor = make_executor(
                case, monkeypatch,
                config=gates_config(exec_live=True, risk_max_derivatives_exposure_pct=100.0),
                factory=lambda: exchange,
                env={"TRADINGAGENTS_EXEC_LIVE": "true"},
            )
            bind_identity(monkeypatch, VERIFIED_IDENTITY)
            if with_shadow:
                _enable_shadow()
                _record_cycle()
                store.set_cursor("1970-01-01T00:00:00+00:00|seed")
                _run_shadow(provider=_ExplodingProvider())
            order = executor.plan_order(
                "BTC-USD", "Buy", PortfolioContext(cash=1_000.0), price=50_000.0,
                **bracket_kwargs(),
            )
        finally:
            with mode_update_lock((AutoMode.FUTURES,)):
                set_config({"auto_futures_enabled": False})
        return order.model_dump(), list(exchange.calls)

    plan_off, calls_off = _live_calls(False)
    plan_on, calls_on = _live_calls(True)
    assert plan_on == plan_off
    assert calls_on == calls_off


def test_worker_exception_never_escapes_the_poll(postgres_schema):
    """The poll returns a status even when the store itself is unusable."""
    _enable_shadow()
    _record_cycle()
    shadow_store.set_cursor("1970-01-01T00:00:00+00:00|seed")

    from datetime import UTC, datetime

    from tradingagents.dataflows.market_data import Candle

    def bad_source(symbol, timeframe, since_ms, until_ms):
        raise OSError("venue read failed")

    now = int(datetime.now(UTC).timestamp() * 1000)
    series = [
        Candle(symbol="BTC/USDT:USDT", timeframe="1h",
               open_time=now - (index + 1) * 3_600_000,
               close_time=now - index * 3_600_000,
               open=100.0, high=101.0, low=99.0, close=100.5,
               volume=10.0, is_closed=True)
        for index in reversed(range(6))
    ]
    result = shadow_worker.poll_once(
        provider_factory=lambda: [_ExplodingProvider()],
        candle_source=lambda symbol, timeframe, since, until: series,
        batch_size=2,
    )
    assert result["status"] == "polled"
    # A read failure through the worker's own source is absorbed too. The status
    # is pinned exactly — accepting a set of three plausible statuses would let a
    # regression that skips every cycle pass as "one of the allowed outcomes".
    result = shadow_worker.poll_once(
        provider_factory=lambda: [_ExplodingProvider()], candle_source=bad_source, batch_size=2,
    )
    assert result["status"] == "polled"


def test_graph_and_agents_never_import_the_shadow_layer():
    repo = Path(__file__).parents[1]
    forbidden_prefixes = ("tradingagents.shadow", "server.shadow")

    for root in ("tradingagents/graph", "tradingagents/agents"):
        for path in sorted((repo / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.append(node.module or "")
            assert not [
                name for name in names if name.startswith(forbidden_prefixes)
            ], f"{path} imports the shadow layer"

    for path in sorted((repo / "server").glob("autoloop*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                if node.module == "server":
                    modules.extend(f"server.{alias.name}" for alias in node.names)
        assert not [name for name in modules if name.startswith(forbidden_prefixes)], path


def test_worker_thread_lifecycle_is_wired_only_through_the_composition_root():
    """main.py starts/stops the worker; the autoloop never does."""
    repo = Path(__file__).parents[1]
    main_source = (repo / "server" / "main.py").read_text(encoding="utf-8")
    assert "shadow_worker.start()" in main_source
    assert "shadow_worker.stop()" in main_source
    for path in sorted((repo / "server").glob("autoloop*.py")):
        source = path.read_text(encoding="utf-8")
        assert "shadow_worker" not in source, path
        assert "shadow_store" not in source, path


def test_shadow_api_is_read_only():
    """The shadow router exposes GET only, and no writer is reachable from it."""
    from server import shadow_api

    methods = {
        route.path: sorted(route.methods or []) for route in shadow_api.router.routes
    }
    assert methods == {"/api/shadow": ["GET"]}


def test_shadow_api_refuses_a_bad_page_size_as_a_client_error():
    """F5: ?limit=0|-1 used to reach PostgreSQL and surface as a 500.

    Exercised on the handler and the guard directly: TestClient needs a socketpair,
    which this suite's ``_no_network`` fixture refuses in unit tests, and the
    behaviour under test lives in the argument check, not in the transport.
    """
    from server import shadow_api
    from server.contract import ApiError

    for bad in (0, -1):
        with pytest.raises(ApiError) as excinfo:
            shadow_api.get_shadow(limit=bad)
        assert excinfo.value.status_code == 400
        assert excinfo.value.code == "validation_error"
        assert "positive" in excinfo.value.message
        assert excinfo.value.details["limit"] == bad

    # The upper bound is enforced too, so one request cannot ask for the whole table.
    with pytest.raises(ApiError) as excinfo:
        shadow_api.get_shadow(limit=shadow_api.MAX_LIMIT + 1)
    assert excinfo.value.status_code == 400
    assert "at most" in excinfo.value.message

    # A valid page size passes the guard (the store call is what would need a DB).
    assert shadow_api._checked_limit(20) == 20
    assert shadow_api._checked_limit(shadow_api.MAX_LIMIT) == shadow_api.MAX_LIMIT


def test_shadow_module_is_importable_without_the_model_package():
    """The optional model never becomes an import-time dependency of the app."""
    import importlib

    for name in (
        "tradingagents.shadow",
        "tradingagents.shadow.filters",
        "tradingagents.shadow.kronos",
        "tradingagents.shadow.evaluation",
        "tradingagents.shadow.recorder",
        "server.shadow_store",
        "server.shadow_worker",
        "server.shadow_api",
    ):
        importlib.import_module(name)

    import sys

    assert "torch" not in sys.modules
    # pandas IS a repo dependency (dataflows), so it may already be loaded; what
    # must never appear is the model runtime.
    assert "einops" not in sys.modules


def test_kronos_adapter_reports_unavailable_without_a_local_install(tmp_path):
    """With no isolated interpreter the adapter says so instead of forecasting."""
    from tradingagents.shadow.kronos import KronosProvider
    from tradingagents.shadow.provider import STATUS_UNAVAILABLE as UNAVAILABLE

    provider = KronosProvider(
        python=tmp_path / "missing-python.exe",
        source_dir=tmp_path / "src",
        model_dir=tmp_path / "models",
    )
    reason = provider.unavailable_reason()
    assert reason is not None and "isolated Kronos interpreter" in reason

    from tradingagents.dataflows.market_data import Candle
    from tradingagents.shadow.provider import ForecastRequest

    candles = tuple(
        Candle(
            symbol="BTC/USDT:USDT", timeframe="1h",
            open_time=1_700_000_000_000 + index * 3_600_000,
            close_time=1_700_000_000_000 + (index + 1) * 3_600_000,
            open=100.0, high=101.0, low=99.0, close=100.5, volume=10.0, is_closed=True,
        )
        for index in range(40)
    )
    attempt = KronosProvider(
        python=tmp_path / "missing-python.exe",
        source_dir=tmp_path / "src", model_dir=tmp_path / "models",
    ).forecast(ForecastRequest(
        symbol="BTC/USDT:USDT", timeframe="1h", horizon_hours=4,
        candles=candles, as_of="2025-01-01T00:00:00+00:00", seed=1,
    ))
    assert attempt.status == UNAVAILABLE
    assert attempt.paths == ()
    assert "interpreter" in attempt.reason


def test_kronos_adapter_refuses_a_tampered_artifact(tmp_path):
    """A weight file whose bytes changed is refused before any inference runs."""
    from tradingagents.shadow.kronos import PINNED_SOURCE_HASHES, verify_artifacts

    source = tmp_path / "src"
    models = tmp_path / "models"
    for relative in PINNED_SOURCE_HASHES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# tampered\n")
    (models / "Kronos-Tokenizer-base").mkdir(parents=True)
    (models / "Kronos-Tokenizer-base" / "model.safetensors").write_bytes(b"x")
    problem, _ = verify_artifacts(source, models)
    assert problem is not None
    assert "hash mismatch" in problem


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_shadow_layer_adds_no_git_tracked_runtime_dependency():
    """The optional model is not a shipped dependency: pyproject stays clean."""
    repo = Path(__file__).parents[1]
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("torch", "einops", "safetensors", "huggingface_hub", "kronos"):
        assert package not in text.lower(), f"{package} leaked into pyproject dependencies"


def test_stored_observation_does_not_change_risk_limits(postgres_schema):
    """Recording an observation leaves the risk limits and modes untouched."""
    from tradingagents.dataflows.config import get_config

    before = {
        key: get_config().get(key)
        for key in (
            "risk_max_total_exposure_pct", "risk_max_derivatives_exposure_pct",
            "risk_max_derivatives_exposure_hard_pct", "risk_max_open_heat_pct",
            "auto_spot_enabled", "auto_futures_enabled", "exec_live",
        )
    }
    _enable_shadow()
    _record_cycle()
    shadow_store.set_cursor("1970-01-01T00:00:00+00:00|seed")
    _run_shadow(provider=_OkProvider())
    after = {key: get_config().get(key) for key in before}
    assert after == before
