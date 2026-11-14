"""E3: experiment isolation — every file-bound path lives in the sandbox.

Plan §4.3 E3: RESULTS_DIR/CACHE_DIR/MEMORY_LOG_PATH (plus the exec audit log)
are rerouted into the experiment's own directory; the operator's real memory
log is never opened, and a config that escapes the sandbox fails loudly.
"""

import os

import pytest

from tradingagents.experiments.isolation import IsolationSandbox
from tradingagents.experiments.runner import build_manifest

pytestmark = pytest.mark.unit


def test_config_overlay_points_every_file_key_into_the_sandbox(tmp_path):
    sandbox = IsolationSandbox(tmp_path / "run")
    sandbox.prepare()
    config = {
        "results_dir": str(os.path.expanduser("~") + "/.tradingagents/logs"),
        "data_cache_dir": str(os.path.expanduser("~") + "/.tradingagents/cache"),
        "memory_log_path": str(os.path.expanduser("~") + "/.tradingagents/memory/trading_memory.md"),
        "exec_log_path": str(os.path.expanduser("~") + "/.tradingagents/execution/audit.jsonl"),
        "llm_provider": "openai",
    }
    overlay = sandbox.config_overlay(config)
    for key in ("results_dir", "data_cache_dir", "memory_log_path", "exec_log_path"):
        assert str(sandbox.root.resolve()) in str(overlay[key])
    assert overlay["llm_provider"] == "openai"  # untouched keys pass through
    sandbox.assert_sandboxed(overlay)


def test_escaping_config_fails_loudly(tmp_path):
    sandbox = IsolationSandbox(tmp_path / "run")
    outside = tmp_path.parent / "elsewhere" / "memory.md"
    with pytest.raises(RuntimeError, match="outside the sandbox"):
        sandbox.assert_sandboxed({"memory_log_path": str(outside)})


def test_apply_env_reroutes_the_runtime_vars_and_restores(tmp_path):
    sandbox = IsolationSandbox(tmp_path / "run")
    sandbox.prepare()
    saved = sandbox.apply_env()
    try:
        assert os.environ["TRADINGAGENTS_RESULTS_DIR"] == str(sandbox.results_dir)
        assert os.environ["TRADINGAGENTS_CACHE_DIR"] == str(sandbox.cache_dir)
        assert os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] == str(sandbox.memory_log_path)
        assert os.environ["TRADINGAGENTS_EXEC_LOG_PATH"] == str(sandbox.exec_log_path)
    finally:
        sandbox.restore_env(saved)
    # Restoration is exact: each var is absent again or back to its pre-call
    # value — never the sandbox path.
    for var, target in (
        ("TRADINGAGENTS_RESULTS_DIR", sandbox.results_dir),
        ("TRADINGAGENTS_CACHE_DIR", sandbox.cache_dir),
        ("TRADINGAGENTS_MEMORY_LOG_PATH", sandbox.memory_log_path),
        ("TRADINGAGENTS_EXEC_LOG_PATH", sandbox.exec_log_path),
    ):
        current = os.environ.get(var)
        assert current != str(target), f"{var} still points into the sandbox"
        assert current == saved[var] or (current is None and saved[var] == "")


def test_experiment_run_writes_only_inside_its_own_directory(tmp_path):
    """A full runner pass touches nothing outside the run's artifact tree."""
    from tradingagents.dataflows.market_data import Candle
    from tradingagents.experiments.baselines import no_trade_source
    from tradingagents.experiments.runner import VariantSpec, run_experiment
    from tradingagents.experiments.walkforward import (
        CLOSE_TIME_KEY,
        OPEN_TIME_KEY,
        holdout_start_ms_from_iso,
        split_walkforward,
    )

    start_ms = holdout_start_ms_from_iso("2026-02-01")
    _DAY = 86_400_000
    rows = [{OPEN_TIME_KEY: start_ms + (i - 20) * _DAY,
             CLOSE_TIME_KEY: start_ms + (i - 20 + 1) * _DAY} for i in range(40)]
    split = split_walkforward(rows, holdout_start_ms=start_ms, horizon_days=1)
    candles = [
        Candle("BTC/USDT:USDT", "1h", start_ms + i * _DAY, start_ms + i * _DAY + 3_600_000,
               100.0, 101.0, 99.0, 100.5, 10.0, True)
        for i in range(4)
    ]
    manifest = build_manifest(
        run_id="iso_run", hypothesis="h", window="2026-01-01:2026-02-01",
        holdout_start="2026-02-01", horizon_days=1, embargo_days=0,
        seed=1, repeat=1, variants=["no_trade"], data_source="t",
        config={"results_dir": str(tmp_path)},
    )
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=[VariantSpec(name="no_trade", kind="no_trade", factory=lambda: no_trade_source)],
        candles_holdout=candles,
    )
    # Everything the run produced lives under results_dir/experiments/iso_run.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written and all(
        "experiments" in str(p) and "iso_run" in str(p) for p in written
    ), f"run wrote outside its artifact tree: {written}"
    # The sandbox subtree exists and the operator-style paths never appear.
    assert (tmp_path / "experiments" / "iso_run" / "sandbox" / "memory").is_dir()


def test_manifest_records_the_config_hash_not_the_operator_paths(tmp_path):
    manifest = build_manifest(
        run_id="cfg_run", hypothesis="h", window="2026-01-01:2026-02-01",
        holdout_start="2026-02-01", horizon_days=1, embargo_days=0,
        seed=2, repeat=1, variants=["no_trade"], data_source="t",
        config={"memory_log_path": str(tmp_path / "sandbox" / "memory.md")},
    )
    assert manifest["config_hash"]
    assert str(tmp_path) not in manifest["config_hash"]


def test_runner_writes_the_risk_audit_into_the_sandbox(tmp_path):
    """The isolation sandbox is wired, not decorative: a variant that opens a
    position drives RiskGuard, whose audit lines land in the sandbox's exec
    log — never in the engine's tempdir fallback and never outside the run.
    """
    from tradingagents.dataflows.market_data import Candle
    from tradingagents.experiments.baselines import no_trade_source
    from tradingagents.experiments.runner import VariantSpec, run_experiment
    from tradingagents.experiments.walkforward import (
        CLOSE_TIME_KEY,
        OPEN_TIME_KEY,
        holdout_start_ms_from_iso,
        split_walkforward,
    )
    from tradingagents.simulation.engine import Proposal

    start_ms = holdout_start_ms_from_iso("2026-02-01")
    _DAY = 86_400_000
    rows = [{OPEN_TIME_KEY: start_ms + (i - 20) * _DAY,
             CLOSE_TIME_KEY: start_ms + (i - 20 + 1) * _DAY} for i in range(40)]
    split = split_walkforward(rows, holdout_start_ms=start_ms, horizon_days=1)
    candles = [
        Candle("BTC/USDT:USDT", "1h", start_ms + i * _DAY, start_ms + i * _DAY + 3_600_000,
               100.0, 101.0, 99.0, 100.5, 10.0, True)
        for i in range(4)
    ]

    def trading_factory():
        def source(candle: Candle):
            return Proposal(
                signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
                stop_loss=candle.close * 0.98, take_profit=candle.close * 1.04,
                risk_fraction=0.004,
            )
        return source

    def limits():
        from tradingagents.execution.risk import RiskLimits
        return RiskLimits(
            max_daily_loss_pct=5.0, max_position_pct_per_asset=25.0,
            max_total_exposure_pct=80.0, max_consecutive_loss_count=3,
            max_derivatives_leverage=3.0, max_derivatives_exposure_pct=50.0,
            max_derivatives_exposure_hard_pct=100.0,
            risk_derivatives_override_min_r=2.0,
            risk_derivatives_override_max_risk_fraction=0.01,
            risk_derivatives_override_max_per_day=0, max_open_heat_pct=0.0,
        )

    manifest = build_manifest(
        run_id="audit_run", hypothesis="h", window="2026-01-01:2026-02-01",
        holdout_start="2026-02-01", horizon_days=1, embargo_days=0,
        seed=3, repeat=1, variants=["no_trade", "rule"], data_source="t",
        config={"results_dir": str(tmp_path)},
    )
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=[
            VariantSpec(name="no_trade", kind="no_trade", factory=lambda: no_trade_source),
            VariantSpec(name="rule", kind="rule", factory=trading_factory),
        ],
        candles_holdout=candles, risk_limits=limits(),
    )
    exec_log = tmp_path / "experiments" / "audit_run" / "sandbox" / "execution" / "audit.jsonl"
    assert exec_log.is_file(), "RiskGuard audit lines must land in the sandbox exec log"
    content = exec_log.read_text(encoding="utf-8")
    assert content.strip(), "the trading variant must have produced audit events"
