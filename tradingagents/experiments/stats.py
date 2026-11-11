"""Paired-comparison statistics for experiment variants (plan §4.1).

One concern: given per-repeat metric values for two variants measured over the
SAME holdout candles (a paired design), produce the difference with bootstrap
uncertainty and an explicit sample-coverage verdict. The module deliberately
has no "winner" output: when the sample is too small or the interval straddles
zero, the result is ``insufficient_evidence`` — declaring a winner would be
the one thing this layer must never do.

Bootstrap draws are seeded from the caller (a ``random.Random`` instance), so
a manifest's seed reproduces a report exactly. Percentile intervals are used
because a difference of equity/net PnL across few repeats is nowhere near
normally distributed. A deterministic variant repeats identical values, so
its bootstrap interval collapses to a single point; the ``consistent_difference``
reason then says so explicitly — that interval carries no sampling uncertainty
and the verdict rests on the relative clear margin alone.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

# The minimum paired sample for the bootstrap to be reported at all. Below it
# the verdict is insufficient_evidence by coverage, regardless of the gap.
MIN_PAIRED_REPEATS = 3
BOOTSTRAP_DRAWS = 2000
# The two-sided interval must clear zero by this margin (in metric units,
# computed as a fraction of the control mean when the metric is scale-free;
# for a scale-free comparison the fixed relative margin is used).
RELATIVE_CLEAR_MARGIN = 0.02


def _finite(values: Sequence[float]) -> list[float]:
    checked = []
    for value in values:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"metric values must be numbers, got {value!r}")
        if not math.isfinite(value):
            raise ValueError("metric values must be finite (None stays None upstream)")
        checked.append(float(value))
    return checked


def paired_difference(
    challenger: Sequence[float],
    control: Sequence[float],
    *,
    rng: random.Random,
    metric: str,
    draws: int = BOOTSTRAP_DRAWS,
) -> dict[str, Any]:
    """The paired difference of two variants over the same holdout.

    ``challenger`` and ``control`` are per-repeat values in repeat order; the
    pairs are index-aligned, so both sequences must have the same length and
    come from the same seeded repeats. The verdict is one of:
    ``insufficient_evidence_coverage`` (fewer than ``MIN_PAIRED_REPEATS``
    pairs), ``insufficient_evidence_interval`` (the interval straddles zero),
    or ``consistent_difference`` (the interval clears zero — still not a
    winner claim, just a difference the sample consistently shows).
    """
    if metric != metric.strip() or not metric:
        raise ValueError("metric must be a non-empty name")
    if not challenger or len(challenger) != len(control):
        raise ValueError(
            "paired comparison needs equal-length, non-empty sequences: "
            f"challenger={len(challenger)}, control={len(control)}"
        )
    chal = _finite(challenger)
    ctrl = _finite(control)
    n = len(chal)
    diffs = [a - b for a, b in zip(chal, ctrl, strict=True)]
    mean_diff = sum(diffs) / n

    if n < MIN_PAIRED_REPEATS:
        return {
            "metric": metric, "pairs": n,
            "mean_difference": mean_diff,
            "interval_low": None, "interval_high": None,
            "confidence": None,
            "verdict": "insufficient_evidence_coverage",
            "reason": f"only {n} paired repeat(s); the minimum is {MIN_PAIRED_REPEATS}",
        }

    if draws < 1:
        raise ValueError("bootstrap draws must be at least 1")
    stats = []
    for _ in range(draws):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        sample.sort()
        stats.append(sum(sample) / n)
    stats.sort()
    low = stats[int(0.025 * (draws - 1))]
    high = stats[int(0.975 * (draws - 1))]

    control_mean = sum(ctrl) / n
    scale = abs(control_mean) if control_mean != 0 else max(abs(v) for v in diffs) or 1.0
    clears = (low > 0 and high > 0) or (low < 0 and high < 0)
    clears_enough = clears and (min(abs(low), abs(high)) / scale) >= RELATIVE_CLEAR_MARGIN

    if not clears:
        verdict = "insufficient_evidence_interval"
        reason = (
            f"the 95% bootstrap interval [{low:.6g}, {high:.6g}] straddles zero; "
            "no direction is claimed"
        )
    elif not clears_enough:
        verdict = "insufficient_evidence_interval"
        reason = (
            f"the 95% bootstrap interval [{low:.6g}, {high:.6g}] clears zero by less "
            f"than {RELATIVE_CLEAR_MARGIN:.0%} of the scale; no direction is claimed"
        )
    else:
        verdict = "consistent_difference"
        degenerate = low == high
        reason = (
            f"the 95% bootstrap interval [{low:.6g}, {high:.6g}] clears zero; this is "
            "a difference the sample shows consistently, NOT a promotion or a winner claim"
        )
        if degenerate:
            reason += (
                "; NOTE the interval is a single point because the paired repeats "
                "are identical (a deterministic variant) — it carries no sampling "
                "uncertainty and only the relative margin distinguishes it from zero"
            )
    return {
        "metric": metric, "pairs": n,
        "mean_difference": mean_diff,
        "interval_low": low, "interval_high": high,
        "confidence": 0.95, "draws": draws,
        "verdict": verdict, "reason": reason,
    }


__all__ = ["paired_difference", "MIN_PAIRED_REPEATS", "BOOTSTRAP_DRAWS", "RELATIVE_CLEAR_MARGIN"]
