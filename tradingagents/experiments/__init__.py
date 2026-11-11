"""Out-of-sample version comparison (plan §4, stage `experiments`).

The seam holds one concern per module: ``walkforward`` splits time with purge
+ embargo, ``baselines`` builds proposal sources that all run the SAME
simulation, ``stats`` pairs repeats without winner language, ``artifacts``
locks criteria in a manifest before results exist, ``isolation`` sandboxes
every file-bound path away from the operator's state, and ``runner``
orchestrates seeded repeats into the locked manifest's criteria.

No module here imports runtime config or settings writers: experiments
produce reports and artifacts only — promotion is a separate reviewed change.
"""

from tradingagents.experiments.artifacts import (
    load_manifest,
    load_results,
    load_summary,
    manifest_digest,
    results_root,
    run_dir,
    verify_manifest,
    write_manifest,
    write_summary,
)
from tradingagents.experiments.isolation import IsolationSandbox
from tradingagents.experiments.runner import VariantSpec, build_manifest, run_experiment
from tradingagents.experiments.stats import paired_difference
from tradingagents.experiments.walkforward import Split, split_walkforward

__all__ = [
    "Split",
    "IsolationSandbox",
    "VariantSpec",
    "build_manifest",
    "load_manifest",
    "load_results",
    "load_summary",
    "manifest_digest",
    "paired_difference",
    "results_root",
    "run_dir",
    "run_experiment",
    "split_walkforward",
    "verify_manifest",
    "write_manifest",
    "write_summary",
]
