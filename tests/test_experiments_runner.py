"""E2: the experiment runner — seeded repeats, locked criteria, no promotion.

Plan §4.3 E2: seed/repeat store all N repeats; the manifest locks the
comparison criteria BEFORE any result exists; LLM cost/latency columns exist
per repeat; and the experiments layer must contain no promotion code path
(no runtime-config/settings-writer import or reference).
"""

import json
import random
from datetime import UTC, datetime

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.execution.risk import RiskLimits
from tradingagents.experiments import artifacts, runner as experiment_runner
from tradingagents.experiments.runner import VariantSpec, build_manifest, run_experiment
from tradingagents.experiments.walkforward import (
    CLOSE_TIME_KEY,
    OPEN_TIME_KEY,
    holdout_start_ms_from_iso,
    split_walkforward,
)
from tradingagents.simulation.costs import FundingSeries
from tradingagents.simulation.engine import Proposal

pytestmark = pytest.mark.unit

_DAY_MS = 86_400_000


def _holdout_candles(count: int, start_ms: int) -> list[Candle]:
    candles = []
    price = 100.0
    for index in range(count):
        open_ms = start_ms + index * _DAY_MS
        close = price * 1.01
        candles.append(Candle(
            symbol="BTC/USDT:USDT", timeframe="1h", open_time=open_ms,
            close_time=open_ms + 3_600_000, open=price, high=close * 1.002,
            low=price * 0.999, close=close, volume=1000.0, is_closed=True,
        ))
        price = close
    return candles


def _rows(count: int, end_ms: int) -> list[dict]:
    return [
        {OPEN_TIME_KEY: end_ms - (count - i) * _DAY_MS,
         CLOSE_TIME_KEY: end_ms - (count - i - 1) * _DAY_MS}
        for i in range(count)
    ]


def _split(start_ms: int):
    # Rows straddle the boundary: 20 rows ending one day after it, so the
    # holdout side is non-empty (20 rows up to boundary+1d).
    rows = _rows(40, start_ms + _DAY_MS)
    return split_walkforward(rows, holdout_start_ms=start_ms, horizon_days=1)


def _manifest(tmp_path, *, seed=7, repeat=3, variants=("no_trade", "rule")) -> dict:
    return build_manifest(
        run_id=f"exp_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{seed}",
        hypothesis="the rule variant's net differs from the cash baseline",
        window="2026-01-01:2026-02-01",
        holdout_start="2026-02-01",
        horizon_days=1,
        embargo_days=0,
        seed=seed,
        repeat=repeat,
        variants=list(variants),
        data_source="test-candles",
        config={"risk_derivatives_risk_fraction": 0.01},
    )


def _variants() -> list[VariantSpec]:
    from tradingagents.experiments.baselines import no_trade_source

    def rule_factory():
        def source(candle: Candle):
            return None  # deterministic: same shape, honest zero activity
        return source

    return [
        VariantSpec(name="no_trade", kind="no_trade", factory=lambda: no_trade_source),
        VariantSpec(name="rule", kind="rule", factory=rule_factory),
    ]


def _trading_variants() -> list[VariantSpec]:
    def trader_factory():
        def source(candle: Candle) -> Proposal:
            return Proposal(
                signal="Buy", ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT",
                stop_loss=candle.close * 0.98, take_profit=candle.close * 1.04,
                risk_fraction=0.004, margin_fraction=0.25,
            )

        return source

    return [
        VariantSpec(name="no_trade", kind="no_trade", factory=lambda: lambda candle: None),
        VariantSpec(name="rule", kind="rule", factory=trader_factory),
    ]


def _funding_for(candles: list[Candle], *, partial: bool) -> FundingSeries:
    # "Full" must cover every exposure window the engine now accounts,
    # including the final candle's open-to-close interval; "partial" stops at
    # the last open so that final window stays unknown. The settlement sits in
    # candles[3] because candles[2] fires the first scale-out tranche — a
    # settlement sharing that candle is honestly unattributable intrabar.
    return FundingSeries(
        amounts={candles[3].open_time + 1: 2.0},
        venue_semantics="test: positive = received",
        known_from_ms=candles[2 if partial else 1].open_time,
        known_to_ms=candles[-1].open_time if partial else candles[-1].close_time,
    )


def _run_trading_experiment(tmp_path, *, funding: FundingSeries | None) -> dict:
    start_ms = holdout_start_ms_from_iso("2026-06-01")
    split = _split(start_ms)
    candles = _holdout_candles(6, start_ms)
    manifest = _manifest(tmp_path, seed=17, repeat=3, variants=("no_trade", "rule"))
    return run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_trading_variants(), candles_holdout=candles,
        risk_limits=RiskLimits(max_derivatives_leverage=3.0, max_derivatives_exposure_pct=50.0),
        funding=funding,
    )


def test_all_repeats_are_stored_and_seed_reproduces_them(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-02-01")
    split = _split(start_ms)
    candles = _holdout_candles(6, start_ms)
    manifest = _manifest(tmp_path, seed=7, repeat=3)
    summary = run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_variants(), candles_holdout=candles,
    )
    directory = artifacts.run_dir(tmp_path, manifest["run_id"])
    rows = artifacts.load_results(directory)
    # Every variant × every repeat: 2 × 3 = 6 rows, seeds 7/8/9 — no
    # cherry-picking: all N repeats are on disk and in the summary.
    assert len(rows) == 6
    assert {row["seed"] for row in rows} == {7, 8, 9}
    assert {row["variant"] for row in rows} == {"no_trade", "rule"}
    assert all(row["repeat"] in (0, 1, 2) for row in rows)
    assert summary["repeat_count"] == 3
    rerun = run_experiment(
        results_dir=tmp_path, manifest=dict(manifest, run_id=manifest["run_id"] + "_b"),
        split=split, variants=_variants(), candles_holdout=candles,
    )
    assert rerun["per_variant"]["no_trade"]["metric_values"] == \
        summary["per_variant"]["no_trade"]["metric_values"]



def test_traded_run_with_missing_funding_is_unpairable_but_cash_control_is_numeric(tmp_path):
    summary = _run_trading_experiment(tmp_path, funding=None)
    assert summary["per_variant"]["no_trade"]["metric_values"] == [10000.0] * 3
    assert summary["per_variant"]["rule"]["metric_values"] == [None] * 3
    comparison = summary["comparisons"][0]
    assert comparison["verdict"] == "insufficient_evidence_coverage"


def test_traded_run_with_partial_funding_is_unpairable(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-06-01")
    candles = _holdout_candles(6, start_ms)
    summary = _run_trading_experiment(
        tmp_path, funding=_funding_for(candles, partial=True),
    )
    assert summary["per_variant"]["no_trade"]["metric_values"] == [10000.0] * 3
    assert summary["per_variant"]["rule"]["metric_values"] == [None] * 3
    assert summary["comparisons"][0]["verdict"] == "insufficient_evidence_coverage"


def test_traded_run_with_full_funding_keeps_numeric_comparison(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-06-01")
    candles = _holdout_candles(6, start_ms)
    summary = _run_trading_experiment(
        tmp_path, funding=_funding_for(candles, partial=False),
    )
    assert all(
        isinstance(value, float)
        for value in summary["per_variant"]["rule"]["metric_values"]
    )
    assert summary["comparisons"][0]["verdict"] != "insufficient_evidence_coverage"


    start_ms = holdout_start_ms_from_iso("2026-02-01")
    split = _split(start_ms)
    candles = _holdout_candles(4, start_ms)
    manifest = _manifest(tmp_path)
    directory = artifacts.run_dir(tmp_path, manifest["run_id"])
    assert not (directory / artifacts.MANIFEST_NAME).is_file()
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_variants(), candles_holdout=candles,
    )
    # Manifest written first, digest recorded, results reference it, and the
    # digest verification passes (criteria were not edited after the fact).
    assert (directory / artifacts.MANIFEST_NAME).is_file()
    assert artifacts.verify_manifest(directory)
    rows = artifacts.load_results(directory)
    digest = artifacts.load_manifest(directory)["manifest_digest"]
    assert rows and all(row["manifest_digest"] == digest for row in rows)
    with pytest.raises(ValueError, match="criteria are locked"):
        artifacts.write_manifest(directory, {**manifest, "criteria_locked_at": None})


def test_criteria_cannot_be_silently_relocked_after_results(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-02-01")
    split = _split(start_ms)
    manifest = _manifest(tmp_path)
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_variants(), candles_holdout=_holdout_candles(4, start_ms),
    )
    directory = artifacts.run_dir(tmp_path, manifest["run_id"])
    tampered = dict(manifest)
    tampered["criteria"] = dict(manifest["criteria"], metric="other.metric")
    with pytest.raises(ValueError, match="criteria are locked, not revised"):
        artifacts.write_manifest(directory, tampered)


def test_llm_cost_and_latency_columns_exist_per_repeat(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-03-01")
    split = _split(start_ms)

    def llm_factory():
        def source(candle: Candle):
            return None
        source.llm_usage = lambda: {"tokens": 500, "calls": 2, "latency_ms": 1200.0}
        return source

    variants = [
        VariantSpec(name="no_trade", kind="no_trade",
                    factory=lambda: (lambda candle: None)),
        VariantSpec(name="current", kind="recorded", factory=llm_factory),
    ]
    manifest = _manifest(tmp_path, seed=3, repeat=2, variants=("no_trade", "current"))
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=variants, candles_holdout=_holdout_candles(4, start_ms),
    )
    rows = artifacts.load_results(artifacts.run_dir(tmp_path, manifest["run_id"]))
    llm_rows = [row for row in rows if row["variant"] == "current"]
    assert llm_rows and all(row["llm_tokens"] == 500 and row["llm_calls"] == 2
                            and row["llm_latency_ms"] == 1200.0 for row in llm_rows)
    assert all(row["llm_tokens"] == 0 for row in rows if row["variant"] == "no_trade")


def test_insufficient_evidence_verdicts_never_declare_a_winner(tmp_path):
    from tradingagents.experiments.stats import paired_difference

    rng = random.Random(7)
    verdict = paired_difference([1.0], [2.0], rng=rng, metric="x")
    assert verdict["verdict"] == "insufficient_evidence_coverage"
    # Same values every repeat: a consistent nonzero difference.
    same = paired_difference([10.0, 10.0, 10.0], [1.0, 1.0, 1.0], rng=rng, metric="x")
    assert same["verdict"] == "consistent_difference"
    assert "NOT a promotion" in same["reason"]
    # Interval straddling zero → insufficient, no direction.
    mixed = paired_difference([1.0, -1.0, 1.0], [-1.0, 1.0, -1.0], rng=rng, metric="x")
    assert mixed["verdict"] == "insufficient_evidence_interval"


def test_degenerate_interval_is_disclosed_not_pretended(tmp_path):
    """Identical deterministic repeats → a point interval, disclosed as such."""
    from tradingagents.experiments.stats import paired_difference

    verdict = paired_difference(
        [10.0, 10.0, 10.0], [1.0, 1.0, 1.0], rng=random.Random(7), metric="x",
    )
    assert verdict["verdict"] == "consistent_difference"
    assert verdict["interval_low"] == verdict["interval_high"]
    assert "no sampling" in verdict["reason"]
    # A non-degenerate interval does NOT carry the degenerate note.
    varied = paired_difference(
        [9.0, 10.0, 11.0], [1.0, 1.0, 1.0], rng=random.Random(7), metric="x",
    )
    assert "no sampling" not in varied["reason"]


def test_none_metric_values_are_reported_not_zeroed(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-04-01")
    split = _split(start_ms)
    manifest = _manifest(tmp_path, seed=11, repeat=3)
    run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_variants(), candles_holdout=_holdout_candles(4, start_ms),
    )
    directory = artifacts.run_dir(tmp_path, manifest["run_id"])
    rows = artifacts.load_results(directory)
    # no_trade produces no fills: the engine's net is the cash constant, so
    # the metric exists; assert the column is a float, never None-by-drop.
    assert all(isinstance(row["metric_value"], float) for row in rows)
    summary = artifacts.load_summary(directory)
    assert summary["comparisons"], "a paired comparison row must exist"


def test_no_promotion_imports_anywhere_in_the_experiments_layer():
    """The no-auto-promotion contract: no settings/config-writer import."""
    scanned = experiment_runner.assert_no_promotion_imports()
    assert "runner.py" in scanned
    # Belt and braces: walk every file's AST again from the test side.
    import ast
    from pathlib import Path
    package_dir = Path(experiment_runner.__file__).parent
    forbidden_names = {"server.operator_config", "server.settings", "server.control_store"}
    for source in package_dir.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            else:
                continue
            assert not (names & forbidden_names), f"{source.name} imports a promotion module"


def test_artifact_summary_is_json_round_trippable(tmp_path):
    start_ms = holdout_start_ms_from_iso("2026-05-01")
    split = _split(start_ms)
    manifest = _manifest(tmp_path, seed=5, repeat=2)
    summary = run_experiment(
        results_dir=tmp_path, manifest=manifest, split=split,
        variants=_variants(), candles_holdout=_holdout_candles(4, start_ms),
    )
    directory = artifacts.run_dir(tmp_path, manifest["run_id"])
    round_trip = json.loads(json.dumps(summary, default=str))
    assert round_trip["run_id"] == manifest["run_id"]
    assert artifacts.verify_manifest(directory)
