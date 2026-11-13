"""Read back a finished experiment run's summary (plan §4.2, command 3).

One concern: load a run's locked manifest, verify its digest still matches
(criteria not edited after the fact), and print the aggregate summary. No
LLM, no simulation, no state change — a pure read.

Usage (plan §4.2 contract):

    .venv\\Scripts\\python.exe -m tradingagents.experiments.report experiments/<run_id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tradingagents.experiments import artifacts


def read_report(path: str | Path) -> dict:
    """The verified summary of one finished run; a tampered manifest fails."""
    directory = Path(path)
    if not directory.is_dir():
        raise FileNotFoundError(f"no experiment directory at {directory}")
    if not artifacts.verify_manifest(directory):
        raise ValueError(
            f"manifest digest mismatch at {directory}: criteria may have been "
            "edited after results exist; the run is not trustworthy"
        )
    summary = artifacts.load_summary(directory)
    summary["manifest_verified"] = True
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.experiments.report",
        description="Read a finished experiment's summary (no LLM, no state change).",
    )
    parser.add_argument("run_dir", help="experiments/<run_id> directory path")
    args = parser.parse_args(argv)
    try:
        report = read_report(args.run_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
