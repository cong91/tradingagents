"""Experiment artifacts: manifest, per-repeat results, aggregate (plan §4.1).

One concern: write and read one experiment run's artifact directory —
``<results_dir>/experiments/<run_id>/`` by default (never inside the repo).
The ``manifest.json`` is written FIRST and locked with a SHA-256 digest before
any result is written; a result row must carry the manifest digest it was
produced under, so criteria edited after results exist cannot pass a later
verification. Nothing here deletes or rewrites a completed run's rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"
RESULTS_NAME = "results.jsonl"
SUMMARY_NAME = "summary.json"

REQUIRED_CRITERIA_KEYS = (
    "metric", "holdout_start", "horizon_days", "min_paired_repeats",
)
REQUIRED_MANIFEST_KEYS = (
    "run_id", "hypothesis", "criteria", "seed", "repeat", "variants",
    "data", "git_sha", "config_hash",
)


def results_root(results_dir: str | Path) -> Path:
    """``<results_dir>/experiments`` — the experiments' home, outside the repo."""
    return Path(results_dir) / "experiments"


def run_dir(results_dir: str | Path, run_id: str) -> Path:
    """One run's directory; a path-shaped run_id would escape and is refused."""
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise ValueError(f"run_id must be a plain directory name, got {run_id!r}")
    return results_root(results_dir) / run_id


def manifest_digest(manifest: dict[str, Any]) -> str:
    """The SHA-256 of the canonical JSON serialization of a manifest."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_keys(manifest: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise ValueError(f"manifest is missing required key(s): {missing}")
    criteria = manifest.get("criteria")
    if not isinstance(criteria, dict):
        raise ValueError("manifest.criteria must be a dict (the locked comparison criteria)")
    missing_criteria = [key for key in REQUIRED_CRITERIA_KEYS if key not in criteria]
    if missing_criteria:
        raise ValueError(f"manifest.criteria is missing required key(s): {missing_criteria}")


def write_manifest(directory: Path, manifest: dict[str, Any]) -> str:
    """Write the manifest first and lock it; returns the digest results carry.

    Refuses to overwrite an existing manifest (criteria are locked before the
    run — rewriting them after results exist would invalidate every digest)
    and refuses an already-locked manifest object passed back in.
    """
    _require_keys(manifest)
    if manifest.get("criteria_locked_at"):
        raise ValueError("manifest already carries criteria_locked_at; it must not be re-locked")
    if (directory / MANIFEST_NAME).is_file():
        raise ValueError(
            f"a manifest is already locked at {directory / MANIFEST_NAME}; "
            "criteria are locked, not revised"
        )
    digest = manifest_digest(manifest)
    locked = dict(manifest)
    locked["criteria_locked_at"] = datetime.now(UTC).isoformat()
    locked["manifest_digest"] = digest
    (directory / MANIFEST_NAME).write_text(
        json.dumps(locked, indent=1, default=str) + "\n", encoding="utf-8",
    )
    (directory / MANIFEST_DIGEST_NAME).write_text(digest + "\n", encoding="utf-8")
    return digest


def load_manifest(directory: Path) -> dict[str, Any]:
    """Read a run's locked manifest; raises when the file is absent."""
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_manifest(directory: Path) -> bool:
    """True when the stored digest matches the recomputed one; else False."""
    manifest = load_manifest(directory)
    digest_file = directory / MANIFEST_DIGEST_NAME
    if not digest_file.is_file():
        return False
    stored = digest_file.read_text(encoding="utf-8").strip()
    locked = manifest.get("manifest_digest")
    # The digest covers the pre-lock manifest: strip the lock fields before
    # recomputing so the check matches what was hashed at write time.
    candidate = {k: v for k, v in manifest.items() if k not in ("criteria_locked_at", "manifest_digest")}
    recomputed = manifest_digest(candidate)
    return stored == locked == recomputed


def append_result(directory: Path, result: dict[str, Any]) -> None:
    """One per-repeat result row; must reference the locked manifest digest."""
    digest = result.get("manifest_digest")
    if not digest:
        raise ValueError("a result row must carry manifest_digest — the criteria it ran under")
    with (directory / RESULTS_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, default=str) + "\n")


def load_results(directory: Path) -> list[dict[str, Any]]:
    """Every per-repeat row in write order; empty list when none yet."""
    path = directory / RESULTS_NAME
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_summary(directory: Path, summary: dict[str, Any]) -> None:
    """The aggregate summary (read-only view of the per-repeat rows)."""
    (directory / SUMMARY_NAME).write_text(
        json.dumps(summary, indent=1, default=str) + "\n", encoding="utf-8",
    )


def load_summary(directory: Path) -> dict[str, Any]:
    path = directory / SUMMARY_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no summary at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "REQUIRED_CRITERIA_KEYS",
    "REQUIRED_MANIFEST_KEYS",
    "append_result",
    "load_manifest",
    "load_results",
    "load_summary",
    "manifest_digest",
    "results_root",
    "run_dir",
    "verify_manifest",
    "write_manifest",
    "write_summary",
]
