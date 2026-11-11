"""The LLM-backed experiment entry (plan §4.2, command 2) — deliberately
unimplemented until the user activates it.

The command exists as a contract: running it explains what it would do and
exits non-zero rather than silently running an LLM spend, because LLM
experiments cost real money and the plan gates them behind explicit user
activation. The deterministic path is
``python -m tradingagents.experiments.replay_experiment``; the full runner
machinery (manifest, repeats, pairing, artifacts) is shared and already
usable from ``runner.run_experiment``.
"""

from __future__ import annotations

import sys

_ACTIVATION_HINT = (
    "LLM experiments cost real money and are gated behind explicit activation "
    "(plan §4.2, command 2). Nothing here runs an LLM spend silently.\n\n"
    "The deterministic comparison path is available now:\n"
    "  .venv\\Scripts\\python.exe -m tradingagents.experiments.replay_experiment "
    "--rule baseline --rule htf_dominance_bounded_pullback "
    "--window 2026-06-01:2026-08-31 --seed 7 --repeat 3\n\n"
    "The shared machinery (manifest-locked criteria, seeded repeats, paired "
    "stats, artifacts) is implemented in tradingagents/experiments/runner.py "
    "and is used by the deterministic path and tests; the LLM-backed variant "
    "runner is wired only after you activate it."
)


def main(argv: list[str] | None = None) -> int:
    _ = argv  # unused until activation; no partial parsing of an unimplemented CLI
    print(_ACTIVATION_HINT, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
