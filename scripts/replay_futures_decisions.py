"""Standalone offline replay of recorded futures-cycle DECISIONS (plan §3).

This is a **gate-agreement replay, not a financial experiment**: it replays the
exported ``futures_cycles`` rows through the REAL production confirmation rule
and reports how often the rule agrees with what was recorded. It produces no
equity curve, no net PnL and no exit distribution — those need a closed-candle
series the export does not carry.

For the financial part use the other entry point, which takes an imported
candle series:

    .venv/Scripts/python.exe -m tradingagents.experiments.financial_experiment \\
        --candles history.json --fixture tests/fixtures/futures_cycles_replay.json \\
        --holdout-start 2026-10-26

Usage:
    .venv/Scripts/python.exe scripts/replay_futures_decisions.py --fixture tests/fixtures/futures_cycles_replay.json [--rule baseline] [--report out.json]

The report states plainly what the recorded data cannot prove: the export
holds only the two decision candles per cycle, so exits, funding and net PnL
are ``unavailable`` with the reason — missing data is never presented as zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline recorded-DECISION replay for futures cycles: a gate-agreement "
            "report (rule vs recording), not a financial simulation (research only)"
        ),
    )
    parser.add_argument(
        "--fixture", required=True,
        help="exported futures_cycles JSON (e.g. tests/fixtures/futures_cycles_replay.json)",
    )
    parser.add_argument(
        "--rule", default="baseline",
        help="confirmation rule name from server.futures_replay.RULES",
    )
    parser.add_argument(
        "--report", default=None, help="write the JSON report here (default stdout)",
    )
    args = parser.parse_args(argv)

    from server.futures_replay import RULES
    from tradingagents.simulation.decision_replay import replay_recorded_decisions

    rule = RULES[args.rule]
    with open(args.fixture, encoding="utf-8") as handle:
        rows = json.load(handle)
    report = replay_recorded_decisions(rows, confirmation_rule=rule)
    # The report already names itself a gate-agreement replay; add the pointer
    # to the entry point that does produce the financial part, so a reader does
    # not mistake this decision comparison for a profitability experiment.
    report["financial_experiment_command"] = (
        "python -m tradingagents.experiments.financial_experiment "
        "--candles <closed-candle series> --fixture <recorded cycles> "
        "--holdout-start <YYYY-MM-DD>"
    )
    payload = json.dumps(report, indent=1, default=str)
    if args.report:
        Path(args.report).write_text(payload, encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
