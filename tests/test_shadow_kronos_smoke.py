"""S4: real local Kronos inference on a deterministic closed-candle fixture.

This is the one test that runs the actual pinned model, in the operator's
isolated interpreter (never the repo venv). It is a ``smoke`` test because it
needs ~115 MB of pinned weights and a CPU/GPU that can run them.

The ask is explicit that "unavailable" must be an EXPLICIT NONPASS, not a silent
skip: when the isolated environment is absent the test FAILS with the concrete
reason (which interpreter and which file it looked for), so a green shadow stage
can never be printed over a model that was never run. ``pytest.skip`` is used
only for the one case the plan allows — the test is not selected — and the
runner treats a skip as a failure too.

The fixture is generated deterministically inside the test: a pure sine-plus-drift
series with fixed epoch timestamps. No network, no vendor, no recorded data.
"""

import json
import math
import os
from datetime import UTC, datetime

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast.artifact import MIN_PATHS
from tradingagents.shadow.kronos import (
    DEFAULT_LOOKBACK_BARS,
    KronosProvider,
    KronosSettings,
    isolated_model_dir,
    isolated_python,
    isolated_source_dir,
    verify_artifacts,
)
from tradingagents.shadow.provider import (
    CALIBRATION_SAMPLE_RATIO,
    DISTRIBUTION_MODEL,
    GENERATION_FORWARD,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ForecastRequest,
)
from tradingagents.shadow.recorder import build_observation

pytestmark = [pytest.mark.smoke, pytest.mark.integration]

START_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z
STEP_MS = 3_600_000
SYMBOL = "BTC/USDT:USDT"
# The sampler enforces a 16-path floor (a direction ratio from fewer draws
# cannot be told apart from chance, see forecast/sampler.validate_settings),
# and the provider clamps the request up to it — so the smoke asks for exactly
# the floor and asserts the run honors it.
PATHS = MIN_PATHS


def _fixture_candles(count: int = DEFAULT_LOOKBACK_BARS) -> list[Candle]:
    """A deterministic closed-candle series (no randomness, no recorded data)."""
    candles = []
    price = 42_000.0
    for index in range(count):
        wave = math.sin(index / 5.0) * 90.0 + math.sin(index / 17.0) * 220.0
        open_price = price
        close_price = 42_000.0 + wave + index * 3.0
        candles.append(Candle(
            symbol=SYMBOL, timeframe="1h",
            open_time=START_MS + STEP_MS * index,
            close_time=START_MS + STEP_MS * (index + 1),
            open=round(open_price, 2),
            high=round(max(open_price, close_price) + 55.0, 2),
            low=round(min(open_price, close_price) - 55.0, 2),
            close=round(close_price, 2),
            volume=float(100 + (index * 3) % 41),
            is_closed=True,
        ))
        price = close_price
    return candles


def _provider() -> KronosProvider:
    return KronosProvider(
        settings=KronosSettings(paths=PATHS, lookback_bars=DEFAULT_LOOKBACK_BARS),
        deadline_seconds=float(os.environ.get("TRADINGAGENTS_SHADOW_KRONOS_DEADLINE", "900")),
    )


def _environment_problem() -> str | None:
    python = isolated_python()
    if not python.exists():
        return (
            "the isolated Kronos interpreter is absent: "
            f"{python} — create it outside the repo (plan §6.1) and rerun; "
            "this test does NOT pass without a real model run"
        )
    missing = [str(path) for path in (isolated_source_dir(), isolated_model_dir()) if not path.exists()]
    if missing:
        return (
            "the pinned Kronos source/weights are absent: "
            + ", ".join(missing)
            + " — download the pinned revision and rerun; this test does NOT pass "
            "without a real model run"
        )
    problem, _ = verify_artifacts(isolated_source_dir(), isolated_model_dir())
    return problem


def test_pinned_artifacts_match_their_recorded_hashes():
    """The pinned revision on disk is the one the adapter claims to have run."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    problem, verified = verify_artifacts(isolated_source_dir(), isolated_model_dir())
    assert problem is None
    # Every pinned file was actually hashed, not assumed.
    assert verified["model/kronos.py"]
    assert verified["Kronos-small/model.safetensors"]


def test_real_local_inference_on_a_closed_candle_fixture():
    """Run the actual model; a missing environment is a FAILURE, never a skip."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)

    candles = _fixture_candles()
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    request = ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=4,
        candles=tuple(candles), as_of=as_of.isoformat(), seed=7,
    )
    provider = _provider()
    attempt = provider.forecast(request)

    assert attempt.status == STATUS_OK, attempt.reason
    # One independent sample_count=1 draw per path, kept separate.
    assert len(attempt.paths) == PATHS
    for path in attempt.paths:
        assert len(path.closes) == 4  # one bar per predicted hour
        assert len(path.highs) == len(path.lows) == len(path.closes)
        assert path.highs[0] >= path.lows[0]
        assert all(value > 0 for value in path.closes)
    assert attempt.direction in ("up", "down", "flat")

    samples = attempt.samples
    assert samples["sample_count"] == PATHS
    assert samples["per_path_sample_count"] == 1
    assert sum(samples["direction_counts"].values()) == PATHS
    assert samples["band"]["low"] <= samples["band"]["high"]
    # The label is the model's own, and it says the ratio is not a probability.
    assert provider.calibration_label == CALIBRATION_SAMPLE_RATIO
    assert provider.distribution_kind == DISTRIBUTION_MODEL


def test_the_run_is_reproducible_for_a_fixed_seed():
    """Same seed, same closed candles, same paths: a documented model property."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    candles = tuple(_fixture_candles())
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC).isoformat()
    request = ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=4,
        candles=candles, as_of=as_of, seed=11,
    )
    first = _provider().forecast(request)
    second = _provider().forecast(request)
    assert first.status == second.status == STATUS_OK
    assert [path.closes for path in first.paths] == [path.closes for path in second.paths]


def test_observation_row_from_a_real_run_carries_its_provenance():
    """A real inference result becomes a storable row with the pinned revisions."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    candles = tuple(_fixture_candles())
    as_of = datetime.fromtimestamp(candles[-1].close_time / 1000, UTC)
    request = ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=4,
        candles=candles, as_of=as_of.isoformat(), seed=3,
    )
    provider = _provider()
    attempt = provider.forecast(request)
    assert attempt.status == STATUS_OK, attempt.reason
    # Created right after the run: strictly after the last observed close, and
    # before the first predicted bar closes (the model run takes seconds).
    created = datetime.now(UTC)
    row = build_observation(
        provider=provider, request=request, attempt=attempt,
        prediction_created_at=created, decision_moment=as_of,
        cycle_id=None, execution_status=None,
        provenance={"source": "kronos_smoke_test"},
    )
    assert row["status"] == STATUS_OK
    assert row["generation_kind"] in (GENERATION_FORWARD, "retrospective")
    assert row["model_revision"].startswith("NeoQuasar/Kronos-small@")
    assert row["source_revision"]
    assert row["artifact_hash"] and len(row["artifact_hash"]) == 64
    assert row["sample_count"] == PATHS
    assert row["per_path_sample_count"] == 1
    assert json.loads(json.dumps(row["paths"]))  # serializable as stored
    assert row["calibration_label"] == CALIBRATION_SAMPLE_RATIO


def test_adapter_says_unavailable_for_a_missing_interpreter(tmp_path):
    """The unavailable path is exercised without needing the model at all."""
    provider = KronosProvider(
        python=tmp_path / "nope" / "python.exe",
        source_dir=tmp_path / "src", model_dir=tmp_path / "models",
    )
    assert provider.unavailable_reason() is not None
    attempt = provider.forecast(ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=4,
        candles=tuple(_fixture_candles(40)), as_of="2025-01-03T00:00:00+00:00", seed=1,
    ))
    assert attempt.status == STATUS_UNAVAILABLE
    assert attempt.paths == ()
    assert "interpreter" in attempt.reason
    # No stray artifacts were created by the failed attempt.
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / "models").exists()


def test_a_deadline_is_reported_as_a_timeout_not_a_forecast():
    """A process that never answers is a timeout, recorded as such."""
    problem = _environment_problem()
    if problem is not None:
        pytest.fail(problem)
    provider = KronosProvider(
        settings=KronosSettings(paths=1, lookback_bars=64),
        deadline_seconds=0.05,  # far below a real inference
    )
    candles = _fixture_candles(64)
    attempt = provider.forecast(ForecastRequest(
        symbol=SYMBOL, timeframe="1h", horizon_hours=4,
        candles=tuple(candles),
        as_of=datetime.fromtimestamp(candles[-1].close_time / 1000, UTC).isoformat(),
        seed=1,
    ))
    assert attempt.status == "timeout"
    assert "deadline" in attempt.reason
    assert attempt.paths == ()


def test_no_model_library_leaks_into_the_repo_venv():
    """The repo venv stays free of the model's heavy dependencies."""
    import importlib.util

    for name in ("torch", "einops"):
        assert importlib.util.find_spec(name) is None, (
            f"{name} must live in the isolated environment, not the repo venv"
        )


def test_fixture_is_deterministic():
    """The smoke fixture itself has no randomness: two builds are identical."""
    first = _fixture_candles(48)
    second = _fixture_candles(48)
    assert [(c.open, c.high, c.low, c.close, c.volume) for c in first] == [
        (c.open, c.high, c.low, c.close, c.volume) for c in second
    ]
    assert first[0].open_time == START_MS
    assert first[-1].close_time == START_MS + 48 * STEP_MS
    assert all(candle.is_closed for candle in first)
    # A series that would break the engine's contiguity contract is not used here.
    assert first[1].open_time == first[0].close_time
