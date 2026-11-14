"""Simulated order matching from closed OHLC candles.

Only closed candles are matched (``Candle.is_closed``); the forming bar never
enters the engine. A market decision fills at the next candle's open (after
the cost model's slippage); protective levels fill at their own price when
the candle's high/low touches them. When the stop AND the target both sit
inside one candle's high-low range, the candle cannot say which was touched
first — the engine publishes that ambiguity and applies the configured
policy (default ``worst-first``: assume the stop hit) instead of quietly
interpolating a resolution.
"""

from __future__ import annotations

from dataclasses import dataclass

from tradingagents.dataflows.market_data import Candle

AMBIGUITY_POLICIES = ("worst-first", "skip")


@dataclass(frozen=True)
class MarketRules:
    """Exchange precision rules applied to simulated quantities."""

    lot_step: float = 0.001
    min_notional: float = 5.0
    tick_size: float = 0.01

    def round_quantity(self, quantity: float) -> float:
        if quantity <= 0 or self.lot_step <= 0:
            return 0.0
        steps = quantity / self.lot_step
        return float(int(steps)) * self.lot_step

    def respects_notional(self, quantity: float, price: float) -> bool:
        return quantity > 0 and quantity * price >= self.min_notional


@dataclass(frozen=True)
class SimulatedFill:
    """One matched trade inside the simulation, with its simulation price."""

    kind: str  # "entry" | "stop" | "target" | "scale_out" | "open_level"
    side: str  # position side this fill acts on
    price: float
    quantity: float
    candle: Candle
    maker: bool = False
    ambiguous: bool = False


def touches_stop(candle: Candle, stop: float) -> bool:
    return candle.low <= stop <= candle.high


def touches_target(candle: Candle, target: float) -> bool:
    return candle.low <= target <= candle.high


def gaps_stop(candle: Candle, side: str, stop: float) -> bool:
    """True when the candle OPENED beyond the stop: the protective trigger
    activates at the open and the fill happens there (or worse), never at
    the stop price."""
    if side == "long":
        return candle.open <= stop
    return candle.open >= stop


def gaps_target(candle: Candle, side: str, target: float) -> bool:
    """True when the candle OPENED beyond the target: a resting exit limit
    fills at the open, which is at least as favorable as the level."""
    if side == "long":
        return candle.open >= target
    return candle.open <= target


def both_levels_touch(
    candle: Candle, stop: float | None, target: float | None
) -> bool:
    """True when one candle's range could have hit either level."""
    stop_hit = stop is not None and touches_stop(candle, stop)
    target_hit = target is not None and touches_target(candle, target)
    return stop_hit and target_hit


@dataclass(frozen=True)
class LevelMatch:
    """Which exit level a candle matched, under the configured policy."""

    fill: SimulatedFill | None
    ambiguous: bool
    policy: str


def match_exit(
    candle: Candle,
    side: str,
    *,
    stop: float | None,
    target: float | None,
    quantity: float,
    rules: MarketRules,
    ambiguity_policy: str = "worst-first",
) -> LevelMatch:
    """Match one candle against the position's protective levels.

    Gaps are matched first and are decisive: a candle that OPENED beyond the
    stop fills the stop at the open (the venue's trigger fires there — the
    position can never survive a candle that traded entirely past its stop);
    a candle that OPENED beyond the target fills the exit at the open, which
    is at least as favorable as the resting limit. Neither is ambiguous.

    Inside the candle's range, ``worst-first`` fills the stop when both
    levels sit within high-low — the conservative reading; ``skip`` trades
    nothing and reports the ambiguity for the caller to carry forward.
    """
    if ambiguity_policy not in AMBIGUITY_POLICIES:
        raise ValueError(f"unknown ambiguity policy: {ambiguity_policy!r}")
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    if quantity <= 0:
        return LevelMatch(None, False, ambiguity_policy)

    if stop is not None and gaps_stop(candle, side, stop):
        return LevelMatch(
            SimulatedFill("stop", side, candle.open, quantity, candle),
            False, ambiguity_policy,
        )
    if target is not None and gaps_target(candle, side, target):
        return LevelMatch(
            SimulatedFill("target", side, candle.open, quantity, candle, maker=True),
            False, ambiguity_policy,
        )

    if stop is not None and target is not None and both_levels_touch(candle, stop, target):
        if ambiguity_policy == "worst-first":
            return LevelMatch(
                SimulatedFill("stop", side, stop, quantity, candle, ambiguous=True),
                True,
                ambiguity_policy,
            )
        return LevelMatch(None, True, ambiguity_policy)

    if stop is not None and touches_stop(candle, stop):
        return LevelMatch(SimulatedFill("stop", side, stop, quantity, candle), False, ambiguity_policy)
    if target is not None and touches_target(candle, target):
        return LevelMatch(SimulatedFill("target", side, target, quantity, candle, maker=True), False, ambiguity_policy)
    return LevelMatch(None, False, ambiguity_policy)


def match_scale_out(
    candle: Candle,
    level_price: float,
    side: str,
    quantity: float,
) -> SimulatedFill | None:
    """Match one scale-out tranche: a resting limit fills at its level, or at
    the open when the candle opened beyond it (at least as favorable)."""
    if quantity <= 0:
        return None
    if gaps_target(candle, side, level_price):
        return SimulatedFill("scale_out", side, candle.open, quantity, candle, maker=True)
    if touches_target(candle, level_price):
        return SimulatedFill("scale_out", side, level_price, quantity, candle, maker=True)
    return None


def market_entry_fill(
    candle: Candle,
    side: str,
    quantity: float,
    rules: MarketRules,
) -> SimulatedFill | None:
    """A market entry filled at this candle's open, or None when below rules."""
    if quantity <= 0:
        return None
    rounded = rules.round_quantity(quantity)
    if not rules.respects_notional(rounded, candle.open):
        return None
    return SimulatedFill("entry", side, candle.open, rounded, candle)
