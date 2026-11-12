"""Offline futures simulation over closed candles, reusing runtime logic.

Plan §3: the engine replays decisions and exits through the real runtime
functions (risk guard, futures sizing chain, profit protection, scale-out)
with modeled costs — never a fork. Purely offline: no exchange client, no
venue URL, no network path; everything produced is labeled a simulation.
"""

from tradingagents.simulation.decision_replay import replay_recorded_decisions
from tradingagents.simulation.engine import (
    DISCLAIMER,
    SIMULATION_LABEL,
    Proposal,
    ProposalSource,
    SimulationResult,
    SizingCaps,
    futures_plan,
    run_simulation,
)
from tradingagents.simulation.fills import AMBIGUITY_POLICIES

__all__ = [
    "AMBIGUITY_POLICIES",
    "DISCLAIMER",
    "Proposal",
    "ProposalSource",
    "SIMULATION_LABEL",
    "SizingCaps",
    "SimulationResult",
    "futures_plan",
    "replay_recorded_decisions",
    "run_simulation",
]
