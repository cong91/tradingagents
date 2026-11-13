"""Experiment runner: seeded repeats, honest pairing, no promotion (plan §4.1).

One concern: run the registered baselines over a walk-forward split with
``--seed S --repeat N``, store EVERY repeat (never a chosen best), pair the
repeats index-aligned into the comparison, and write the aggregate summary.
The runner never edits config, operator settings or risk limits: it produces
reports and artifacts only — promotion is a separate PR review (plan §4.1,
"No auto-promotion"). An LLM-backed variant records per-repeat token counts,
call counts and latency when its source reports them; a deterministic variant
records zeros so the column exists and is honest.

The comparison's criteria come from the manifest, locked before the first
result is written; the runner reads them from the manifest, not from CLI
state, so the criteria that produced a summary are the locked ones.
"""

from __future__ import annotations

import json
import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents.experiments import artifacts, stats as paired_stats
from tradingagents.experiments.isolation import IsolationSandbox
from tradingagents.experiments.walkforward import Split

# The comparison metric; the engine's net figure is the default because it is
# the number that already folds fees and known funding (and stays None when
# funding is unknown — those repeats are reported, not dropped).
DEFAULT_METRIC = "net_after_costs.final_equity"

# net_after_costs keys whose value depends on funding the run could not prove;
# any metric resolving to one of them on a traded run with unproven funding is
# unproducible, whatever the criteria named.
_FUNDING_DEPENDENT_KEYS = frozenset({
    "final_equity", "final_equity_estimate", "funding_received", "max_drawdown",
})

# Modules the runner must never import or call (plan: no code promotion).
_FORBIDDEN_IMPORTS = (
    "server.operator_config",
    "server.settings",
    "server.control_store",
)


@dataclass(frozen=True)
class VariantSpec:
    """One variant the manifest registers: name + its proposal source factory."""

    name: str
    kind: str  # "no_trade" | "rule" | "recorded" | "ablation"
    factory: Any  # Callable[[], Callable[[Candle], Proposal | None]]


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _config_hash(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, default=str)
    import hashlib
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _metric_value(result: dict[str, Any], metric: str) -> float | None:
    """Read a dotted metric path; unknown traded funding never becomes numeric."""
    net = result.get("net_after_costs")
    traded = bool(result.get("fills"))
    funding_unproven = traded and (
        result.get("funding_known") is False
        or result.get("max_drawdown_status") == "estimate"
        or (
            isinstance(net, dict)
            and (
                net.get("final_equity_status") == "estimate"
                or net.get("funding_known") is False
                or net.get("funding_partial") is True
            )
        )
    )
    value: Any = result
    final_key = metric.rsplit(".", 1)[-1]
    for part in metric.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    if funding_unproven and final_key in _FUNDING_DEPENDENT_KEYS:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_manifest(
    *,
    run_id: str,
    hypothesis: str,
    window: str,
    holdout_start: str,
    horizon_days: int,
    embargo_days: int,
    seed: int,
    repeat: int,
    variants: list[str],
    data_source: str,
    config: dict,
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The manifest dict; criteria default to the locked paired-comparison set."""
    locked_criteria = {
        "metric": DEFAULT_METRIC,
        "holdout_start": holdout_start,
        "horizon_days": horizon_days,
        "min_paired_repeats": paired_stats.MIN_PAIRED_REPEATS,
        "clear_margin_relative": paired_stats.RELATIVE_CLEAR_MARGIN,
        "confidence": 0.95,
    }
    if criteria is not None:
        locked_criteria.update(criteria)
    return {
        "run_id": run_id,
        "hypothesis": hypothesis,
        "criteria": locked_criteria,
        "seed": seed,
        "repeat": repeat,
        "variants": list(variants),
        "window": window,
        "embargo_days": embargo_days,
        "data": data_source,
        "git_sha": _git_sha(),
        "config_hash": _config_hash(config),
        "disclaimer": (
            "Experiment artifacts compare simulated variants; they are not evidence "
            "of live profitability and not financial advice."
        ),
    }


def run_experiment(
    *,
    results_dir: str | Path,
    manifest: dict[str, Any],
    split: Split,
    variants: list[VariantSpec],
    candles_holdout: list,
    risk_limits: Any | None = None,
    costs: Any | None = None,
    market_rules: Any | None = None,
    caps: Any | None = None,
    funding: Any | None = None,
    max_hold_hours: float | None = None,
    initial_cash: float = 10000.0,
    quote_currency: str = "USDT",
    ambiguity_policy: str = "worst-first",
) -> dict[str, Any]:
    """Run every variant over the same holdout candles, N seeded repeats.

    The split's leakage assertion runs first. Each repeat gets a fresh seeded
    RNG (``seed + repeat_index``) so the manifest's seed reproduces the run;
    every repeat's full report is stored, then paired by repeat index for the
    comparison the locked criteria name. The isolation sandbox is wired for
    real: the engine's RiskGuard audit log is pointed at the sandbox's exec
    log and the sandboxed env vars are rerouted for the duration of the run.
    Returns the run's summary dict.
    """
    split.assert_no_leakage()
    criteria = manifest["criteria"]
    metric = criteria["metric"]
    repeat_count = int(manifest["repeat"])
    if repeat_count < 1:
        raise ValueError("repeat must be at least 1")

    directory = artifacts.run_dir(results_dir, manifest["run_id"])
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / artifacts.MANIFEST_NAME).is_file():
        artifacts.write_manifest(directory, manifest)
    digest = artifacts.load_manifest(directory).get("manifest_digest")

    # Isolation is wired, not decorative: the engine's RiskGuard audit log is
    # pointed at the sandbox's exec log (never the operator's default), the
    # sandboxed env vars are rerouted for the duration of the run, and the
    # sandboxed config is asserted to stay inside the sandbox — any escape
    # fails loudly instead of silently touching operator state.
    sandbox = IsolationSandbox(directory / "sandbox")
    sandbox.prepare()
    sandbox.assert_sandboxed({
        "results_dir": str(sandbox.results_dir),
        "data_cache_dir": str(sandbox.cache_dir),
        "memory_log_path": str(sandbox.memory_log_path),
        "exec_log_path": str(sandbox.exec_log_path),
    })
    saved_env = sandbox.apply_env()
    try:
        _run_repeats(
            directory=directory, manifest=manifest, split=split, variants=variants,
            candles_holdout=candles_holdout, metric=metric, repeat_count=repeat_count,
            digest=digest, risk_limits=risk_limits, costs=costs, market_rules=market_rules,
            caps=caps, funding=funding, max_hold_hours=max_hold_hours,
            initial_cash=initial_cash, quote_currency=quote_currency,
            ambiguity_policy=ambiguity_policy,
            audit_log_path=sandbox.exec_log_path,
        )
    finally:
        sandbox.restore_env(saved_env)

    per_variant = _read_variant_rows(directory, variants, digest)
    summary = _summarize(directory, manifest, per_variant, metric)
    artifacts.write_summary(directory, summary)
    return summary


def _run_repeats(
    *,
    directory: Path,
    manifest: dict[str, Any],
    split: Split,
    variants: list[VariantSpec],
    candles_holdout: list,
    metric: str,
    repeat_count: int,
    digest: str,
    risk_limits: Any | None,
    costs: Any | None,
    market_rules: Any | None,
    caps: Any | None,
    funding: Any | None,
    max_hold_hours: float | None,
    initial_cash: float,
    quote_currency: str,
    ambiguity_policy: str,
    audit_log_path: Any,
) -> None:
    """Every repeat, appended to disk under the sandboxed env (no cherry-pick)."""
    from tradingagents.simulation.engine import run_simulation

    per_variant: dict[str, list[dict[str, Any]]] = {}
    for index in range(repeat_count):
        rng_seed = int(manifest["seed"]) + index
        rng = random.Random(rng_seed)
        for spec in variants:
            source = spec.factory()
            result = run_simulation(
                candles_holdout,
                proposal_source=source,
                risk_limits=risk_limits,
                costs=costs,
                market_rules=market_rules,
                caps=caps,
                funding=funding,
                ambiguity_policy=ambiguity_policy,
                max_hold_hours=max_hold_hours,
                initial_cash=initial_cash,
                quote_currency=quote_currency,
                audit_log_path=audit_log_path,
                include_sensitivity=False,
            )
            report = result.report()
            source_coverage = _source_coverage(source, candles_holdout)
            source_coverage["variant_kind"] = spec.kind
            report["source_coverage"] = source_coverage
            metric_value = _metric_value(report, metric)
            if source_coverage["replay_evidence"] and not source_coverage["evidence_backed"]:
                metric_value = None
            llm = _llm_usage(source)
            per_variant.setdefault(spec.name, []).append({
                "variant": spec.name, "repeat": index, "seed": rng_seed,
                "manifest_digest": digest,
                # The metric this value IS: the registry column is NOT NULL and
                # indexes row["metric_name"], so a row that omits it cannot be
                # registered at all. It comes from the LOCKED criteria, never
                # from a caller-supplied label, so a stored value can never be
                # filed under a metric the criteria did not name.
                "metric_name": metric,
                "llm_tokens": llm.get("tokens", 0),
                "llm_calls": llm.get("calls", 0),
                "llm_latency_ms": llm.get("latency_ms", 0.0),
                "metric_value": metric_value,
                "source_coverage": source_coverage,
                "report": report,
            })
        _ = rng  # the seed is folded into per-repeat seeds above

    baseline_name = next(
        (name for name in ("no_trade", "rule", "recorded")
         if name in per_variant),
        next(iter(per_variant)),
    )
    for index, control_row in enumerate(per_variant[baseline_name]):
        if control_row["source_coverage"]["variant_kind"] != "no_trade":
            continue
        has_evidence_backed_challenger = any(
            rows[index]["source_coverage"]["evidence_backed"]
            for name, rows in per_variant.items() if name != baseline_name
        )
        if not has_evidence_backed_challenger:
            control_row["metric_value"] = None
            control_row["source_coverage"]["comparison_status"] = "insufficient_evidence"

    # Persistence: every repeat row (no cherry-picking), one file per run.
    rows = [row for name in per_variant for row in per_variant[name]]
    for row in rows:
        artifacts.append_result(directory, row)


def _read_variant_rows(
    directory: Path,
    variants: list[VariantSpec],
    digest: str,
) -> dict[str, list[dict[str, Any]]]:
    """Reload every stored repeat row; the summary reads what hit the disk."""
    stored: dict[str, list[dict[str, Any]]] = {}
    names = {spec.name for spec in variants}
    for row in artifacts.load_results(directory):
        if row.get("manifest_digest") != digest:
            continue
        name = row.get("variant")
        if name in names:
            stored.setdefault(name, []).append(row)
    missing = [spec.name for spec in variants if spec.name not in stored]
    if missing:
        raise ValueError(f"variant(s) produced no repeats: {missing}")
    return stored


def _source_coverage(source: Any, candles_holdout: list) -> dict[str, Any]:
    """Distinguish recorded replay evidence from ordinary proposal sources."""
    mode = getattr(source, "mode", "unreported")
    replay_evidence = mode in ("counterfactual_rule", "recorded_decisions", "recorded_ablation")
    coverage_for = getattr(source, "coverage_for", None)
    scoped = coverage_for(candles_holdout) if replay_evidence and callable(coverage_for) else {}
    skipped = scoped.get("skipped", getattr(source, "skipped", {}))
    status = scoped.get("status", getattr(source, "coverage_status", "unreported"))
    built = scoped.get("built", getattr(source, "built", None))
    evidence_backed = (
        status == "sufficient_evidence" and isinstance(built, int) and built > 0
        if replay_evidence else mode != "no_trade"
    )
    return {
        "mode": mode,
        "replay_evidence": replay_evidence,
        "input_count": scoped.get("input_count", getattr(source, "input_count", None)),
        "evaluated": scoped.get("evaluated", getattr(source, "evaluated", None)),
        "built": built,
        "status": status,
        "evidence_backed": evidence_backed,
        "skipped": dict(sorted(skipped.items())) if isinstance(skipped, dict) else {},
    }


def _llm_usage(source: Any) -> dict[str, Any]:
    """A deterministic source reports nothing; an LLM one carries usage."""
    usage = getattr(source, "llm_usage", None)
    if callable(usage):
        return dict(usage())
    return {"tokens": 0, "calls": 0, "latency_ms": 0.0}


def _summarize(
    directory: Path,
    manifest: dict[str, Any],
    per_variant: dict[str, list[dict[str, Any]]],
    metric: str,
) -> dict[str, Any]:
    """Pair the repeats and aggregate; no promotion vocabulary anywhere."""
    criteria = manifest["criteria"]
    names = list(manifest["variants"])
    baseline_name = next(
        (name for name in ("no_trade", "rule", "recorded") if name in names), names[0]
    )
    comparisons = []
    missing = [name for name in names if name not in per_variant]
    if missing:
        raise ValueError(f"variant(s) produced no repeats: {missing}")
    counts = {len(per_variant[name]) for name in names}
    if len(counts) != 1:
        raise ValueError(
            f"variants ran different repeat counts: "
            f"{ {n: len(per_variant[n]) for n in names} }"
        )

    rng = random.Random(int(manifest["seed"]))
    for name in names:
        if name == baseline_name:
            continue
        challenger_values = [row["metric_value"] for row in per_variant[name]]
        control_values = [row["metric_value"] for row in per_variant[baseline_name]]
        challenger_coverage = [
            row.get("source_coverage", {}) for row in per_variant[name]
        ]
        insufficient_source = any(
            coverage.get("replay_evidence") is True
            and coverage.get("evidence_backed") is not True
            for coverage in challenger_coverage
        )
        if insufficient_source:
            comparisons.append({
                "challenger": name, "control": baseline_name, "metric": metric,
                "verdict": "insufficient_evidence_coverage",
                "reason": "challenger source has insufficient recorded proposal evidence",
            })
            continue
        if any(value is None for value in challenger_values + control_values):
            comparisons.append({
                "challenger": name, "control": baseline_name, "metric": metric,
                "verdict": "insufficient_evidence_coverage",
                "reason": (
                    "at least one repeat's metric is None (unknown funding or an "
                    "unproducible net figure); None is never treated as zero"
                ),
            })
            continue
        comparisons.append(paired_stats.paired_difference(
            challenger_values, control_values, rng=random.Random(rng.random()), metric=metric,
        ))

    # Every comparison names the pair it compares. ``paired_difference`` returns
    # the statistic alone, and the registry keys a comparison row on
    # (challenger, control, metric) — so the pair is attached HERE, once, for
    # both branches, instead of only on the coverage branch. Without this the
    # registration path raised KeyError('challenger') on every real run whose
    # repeats reached the bootstrap.
    comparisons = [
        {
            "challenger": name, "control": baseline_name,
            **{key: value for key, value in comparison.items()
               if key not in ("challenger", "control")},
        }
        for name, comparison in zip(
            (name for name in names if name != baseline_name), comparisons, strict=True,
        )
    ]

    totals = {}
    for name in names:
        rows = per_variant[name]
        totals[name] = {
            "repeats": len(rows),
            "seeds": [row["seed"] for row in rows],
            "llm_tokens": sum(row["llm_tokens"] for row in rows),
            "llm_calls": sum(row["llm_calls"] for row in rows),
            "metric_values": [row["metric_value"] for row in rows],
            "max_drawdowns": [
                None if (
                    row["report"].get("max_drawdown_status") == "estimate"
                    or (row["source_coverage"].get("replay_evidence")
                        and not row["source_coverage"].get("evidence_backed"))
                ) else row["report"].get("max_drawdown")
                for row in rows
            ],
            "source_coverage": [row.get("source_coverage", {}) for row in rows],
            "ambiguity_counts": [row["report"]["ambiguity_count"] for row in rows],
        }
    return {
        "run_id": manifest["run_id"],
        "criteria": criteria,
        "baseline": baseline_name,
        "comparisons": comparisons,
        "per_variant": totals,
        "repeat_count": next(iter(counts)),
        "verdict_note": (
            "insufficient_evidence_* is the honest result for a sample too small or "
            "an interval straddling zero; a consistent_difference is NOT a promotion. "
            "Promotion is a separate reviewed change to runtime config, never an "
            "experiment output."
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest_digest": manifest.get("manifest_digest"),
        "disclaimer": manifest["disclaimer"],
    }


def assert_no_promotion_imports() -> list[str]:
    """AST check: the experiments layer imports no settings/config writer.

    Part of the no-auto-promotion contract (plan §4.1, test E2): the modules
    that could change runtime state are enumerated and no experiment source
    file may IMPORT them. The check parses the AST, so this docstring and the
    constant below do not trip it — only a real import does. Returns the list
    of files scanned so the caller can pin the coverage.
    """
    import ast

    package_dir = Path(__file__).resolve().parent
    scanned: list[str] = []
    for source in sorted(package_dir.glob("*.py")):
        scanned.append(source.name)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            else:
                continue
            for name in names:
                if name in _FORBIDDEN_IMPORTS:
                    raise AssertionError(
                        f"{source.name} imports {name}; the experiments layer must "
                        "never touch runtime config or settings writers (no auto-promotion)"
                    )
    return scanned


__all__ = [
    "DEFAULT_METRIC",
    "VariantSpec",
    "assert_no_promotion_imports",
    "build_manifest",
    "run_experiment",
]
