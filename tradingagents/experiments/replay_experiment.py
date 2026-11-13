"""Deterministic replay experiment over recorded cycles (plan §4.2, command 1).

Compares confirmation rules from ``server.futures_replay.RULES`` on the
recorded cycle fixture, on an explicit time window, with a seed and repeats
for reproducibility. No LLM anywhere: the rules are pure functions over
closed candles. The financial simulation part is reported with whatever the
recorded data can prove — the export holds only the two decision candles, so
exits/funding/PnL stay unavailable-with-reason, never zero.

Usage (plan §4.2 contract):

    .venv\\Scripts\\python.exe -m tradingagents.experiments.replay_experiment \\
        --rule baseline --rule htf_dominance_bounded_pullback \\
        --window 2026-06-01:2026-08-31 --seed 7 --repeat 3
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from server.futures_replay import FIXTURE_PATH, RULES, load_cycles, replay_rule
from tradingagents.experiments import artifacts


def _fixture_digest(fixture_path: Path) -> str:
    """SHA-256 of the fixture's bytes: two runs over different content differ."""
    import hashlib
    return hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest()


def _git_sha() -> str:
    """The real repo commit, or 'unknown' outside a git checkout."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _parse_window(window: str) -> tuple[datetime, datetime]:
    try:
        start_text, end_text = window.split(":")
        start = datetime.strptime(start_text, "%Y-%m-%d").replace(tzinfo=UTC)
        end = datetime.strptime(end_text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"window must be YYYY-MM-DD:YYYY-MM-DD, got {window!r}") from exc
    if end < start:
        raise ValueError(f"window ends before it starts: {window!r}")
    return start, end


def run_replay_experiment(
    *,
    rules: list[str],
    window: str,
    seed: int,
    repeat: int,
    fixture_path: Path = FIXTURE_PATH,
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run each rule over the recorded cycles inside the window, per repeat.

    Every repeat is stored (the rules are deterministic, so repeats agree —
    a disagreement would mean a broken assumption and is reported loudly).
    The summary pairs each challenger rule against ``baseline`` on the
    agreement rate; with fewer than three paired repeats the verdict is
    insufficient-evidence by coverage.
    """
    if not rules:
        raise ValueError("at least one --rule is required")
    unknown = [name for name in rules if name not in RULES]
    if unknown:
        raise ValueError(f"unknown rule(s) {unknown}; available: {sorted(RULES)}")
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    start, end = _parse_window(window)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((end + timedelta(days=1)).timestamp() * 1000)

    _ISO_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?"

    def cycle_open_ms(cycle) -> int | None:
        # cycle_id shape: futures:<symbol>:<4h_open ISO>:<1h_open ISO>:<strategy>;
        # the ISO opens embed ':' themselves, so extract them by pattern.
        stamps = re.findall(_ISO_TS, cycle.cycle_id)
        if not stamps:
            return None
        try:
            return int(datetime.fromisoformat(stamps[0]).timestamp() * 1000)
        except ValueError:
            return None

    cycles = [cycle for cycle in load_cycles(fixture_path)
              if (open_ms := cycle_open_ms(cycle)) is not None and start_ms <= open_ms < end_ms]

    run_id = f"replay_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{seed}"
    manifest = {
        "run_id": run_id,
        "hypothesis": (
            "compare confirmation rules' decision behavior on recorded cycles "
            "inside the window; financial outcomes are unavailable from this data"
        ),
        "criteria": {
            "metric": "decision_agreement_rate",
            "holdout_start": start.strftime("%Y-%m-%d"),
            "horizon_days": 0,
            "min_paired_repeats": 3,
        },
        "seed": seed,
        "repeat": repeat,
        "variants": list(rules),
        "window": window,
        "data": str(fixture_path),
        # Real provenance: the repo commit the run was made under plus the
        # fixture's content digest — two runs over different fixture content
        # are distinguishable, not just by path.
        "git_sha": _git_sha(),
        "fixture_sha256": _fixture_digest(fixture_path),
        "config_hash": "n/a-deterministic",
        "kind": "replay_experiment",
    }
    home = results_dir
    if home is None:
        from tradingagents.default_config import DEFAULT_CONFIG
        home = DEFAULT_CONFIG.get("results_dir") or Path.home() / ".tradingagents" / "logs"
    directory = artifacts.run_dir(home, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    digest = artifacts.write_manifest(directory, manifest)

    rows: list[dict[str, Any]] = []
    per_rule: dict[str, list[dict[str, Any]]] = {}
    for repeat_index in range(repeat):
        rng = random.Random(seed + repeat_index)
        for name in rules:
            outcomes = replay_rule(cycles, RULES[name])
            confirmed = sum(1 for o in outcomes if o["rule_result"])
            recorded_decisions = {
                "aligned_direction": True,
                "not_required_for_non_tradeable_signal": True,
                "direction_mismatch": False,
                "doji_candle": False,
            }
            known = [o for o in outcomes if o["recorded_confirmation_reason"] in recorded_decisions]
            agreements = sum(
                o["rule_result"] == recorded_decisions[o["recorded_confirmation_reason"]]
                for o in known
            )
            agreement = agreements / len(outcomes) if outcomes and len(known) == len(outcomes) else None
            row = {
                "variant": name, "repeat": repeat_index, "seed": seed + repeat_index,
                "manifest_digest": digest,
                "metric_value": agreement,
                "cycles_replayed": len(outcomes),
                "confirmed_by_rule": confirmed,
                "decision_agreements": agreements,
                "unknown_recorded_decisions": len(outcomes) - len(known),
                "rng_sample": rng.random(),
            }
            per_rule.setdefault(name, []).append(row)
            rows.append(row)
    for row in rows:
        artifacts.append_result(directory, row)

    baseline_values = [row["metric_value"] for row in per_rule.get("baseline", [])]
    comparisons = []
    for name in rules:
        if name == "baseline":
            continue
        challenger = [row["metric_value"] for row in per_rule[name]]
        if len(challenger) < 3 or len(baseline_values) < 3 or any(
            v is None for v in challenger + baseline_values
        ):
            comparisons.append({
                "challenger": name, "control": "baseline",
                "metric": "decision_agreement_rate",
                "verdict": "insufficient_evidence_coverage",
                "reason": "fewer than 3 paired repeats (or an undefined rate); no direction claimed",
            })
        else:
            from tradingagents.experiments.stats import paired_difference
            comparisons.append(paired_difference(
                challenger, baseline_values,
                rng=random.Random(seed), metric="decision_agreement_rate",
            ))
    summary = {
        "run_id": run_id, "kind": "replay_experiment",
        "window": window,
        # Distinct, honest labels: ``cycles_in_window`` counts every cycle
        # whose 4h open falls inside the window; ``cycles_with_context`` the
        # subset whose snapshot rebuilds a full rule context — only those are
        # replayed (the per-rule ``cycles_replayed`` counts the same set).
        "cycles_in_window": len(cycles),
        "cycles_with_context": sum(1 for c in cycles if c.context is not None),
        "rules": rules, "repeat": repeat, "seed": seed,
        "per_rule": {
            name: {
                "repeats": len(values),
                "rates": [row["metric_value"] for row in values],
                "cycles_replayed": values[0]["cycles_replayed"],
                "confirmed_by_rule": values[0]["confirmed_by_rule"],
                "decision_agreements": values[0]["decision_agreements"],
                "unknown_recorded_decisions": values[0]["unknown_recorded_decisions"],
            }
            for name, values in per_rule.items()
        },
        "comparisons": comparisons,
        "financial_simulation": {
            "status": "unavailable",
            "reason": (
                "the recorded export holds only the two decision candles per cycle; "
                "exits, funding and net PnL cannot be replayed from this data. "
                "Missing data is not zero."
            ),
        },
        "recorded_baseline_note": (
            "the engine-level recorded baseline (baselines.recorded_source) "
            "proposes only where the export carries payload.agent_proposal "
            "entry terms; exports without them build zero proposals "
            "(built=0, reported honestly). This replay compares "
            "confirmation-rule behavior only."
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest_digest": digest,
    }
    artifacts.write_summary(directory, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.experiments.replay_experiment",
        description="Deterministic rule replay experiment over recorded cycles (no LLM).",
    )
    parser.add_argument("--rule", action="append", required=True,
                        help=f"a server.futures_replay.RULES name; repeatable. Choices: {sorted(RULES)}")
    parser.add_argument("--window", required=True,
                        help="recorded-cycle window, YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--seed", type=int, default=0, help="experiment seed")
    parser.add_argument("--repeat", type=int, default=1, help="number of repeats stored")
    parser.add_argument("--fixture", default=str(FIXTURE_PATH), help="recorded cycles fixture path")
    parser.add_argument("--results-dir", default=None,
                        help="artifact root (default TRADINGAGENTS_RESULTS_DIR, never the repo)")
    args = parser.parse_args(argv)
    try:
        summary = run_replay_experiment(
            rules=args.rule, window=args.window, seed=args.seed,
            repeat=args.repeat, fixture_path=Path(args.fixture),
            results_dir=args.results_dir,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
