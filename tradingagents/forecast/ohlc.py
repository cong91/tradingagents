"""Validate, correct and audit model-generated OHLC paths.

Model paths are observation data, not market candles. Raw values are retained
for audit, while the public path arrays use a deterministic wick-bound repair so
consumers can distinguish a corrected path from a malformed one.
"""

from __future__ import annotations

import math
from typing import Any

OHLC_FIELDS = ("opens", "highs", "lows", "closes")
OHLC_ALGORITHM = "ohlc_wick_bounds_v1"
OHLC_ALGORITHM_VERSION = 1


def ohlc_path_problem(path: Any, *, index: int, horizon_bars: int) -> str | None:
    """Return why a raw OHLC path is unusable, or ``None`` when valid.

    High/low ordering is deliberately not rejected here. Finite positive
    inverted or out-of-wick bounds are repaired by :func:`correct_ohlc_path`.
    """
    if not isinstance(path, dict):
        return f"path {index} is not an object"
    if isinstance(horizon_bars, bool) or not isinstance(horizon_bars, int) or horizon_bars < 1:
        return f"path {index} has an invalid horizon"
    for name in OHLC_FIELDS:
        values = path.get(name)
        problem = _series_problem(values, name=name, index=index, horizon_bars=horizon_bars)
        if problem is not None:
            return problem
    return None


def normalize_ohlc_path(
    path: Any, *, index: int, horizon_bars: int,
) -> dict[str, Any] | None:
    """Return a corrected, audited copy of ``path`` or ``None`` when malformed.

    A path carrying the formal raw snapshot is already normalized by the
    sampler. It is validated and copied without running correction a second
    time, preserving the original raw values. A legacy candidate path without
    a raw snapshot is treated as raw input and gains one without mutating the
    caller.
    """
    if not isinstance(path, dict):
        return None
    if ("raw_ohlc" in path) != ("ohlc_provenance" in path):
        return None
    if "raw_ohlc" in path:
        source = path.get("raw_ohlc")
        provenance = path.get("ohlc_provenance")
        if not isinstance(source, dict) or not _provenance_is_valid(provenance):
            return None
        if ohlc_path_problem(source, index=index, horizon_bars=horizon_bars) is not None:
            return None
        current = {name: path.get(name) for name in OHLC_FIELDS}
        for name, values in current.items():
            if _series_problem(values, name=name, index=index, horizon_bars=horizon_bars) is not None:
                return None
        try:
            expected, expected_provenance = correct_ohlc_path(source)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        if current != expected or provenance != expected_provenance:
            return None
        return {
            **{name: list(current[name]) for name in OHLC_FIELDS},
            "raw_ohlc": {name: list(source[name]) for name in OHLC_FIELDS},
            "ohlc_provenance": _copy_provenance(provenance),
            **({"seed": path["seed"]} if "seed" in path else {}),
        }

    if ohlc_path_problem(path, index=index, horizon_bars=horizon_bars) is not None:
        return None
    raw = {name: list(path[name]) for name in OHLC_FIELDS}
    try:
        corrected, provenance = correct_ohlc_path(raw)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return {
        **corrected,
        "raw_ohlc": raw,
        "ohlc_provenance": provenance,
        **({"seed": path["seed"]} if "seed" in path else {}),
    }


def correct_ohlc_path(path: dict[str, list[Any]]) -> tuple[dict[str, list[Any]], dict[str, Any]]:
    """Correct wick bounds without changing opens/closes or the input path."""
    if not isinstance(path, dict):
        raise ValueError("OHLC path must be an object")
    if any(not isinstance(path.get(name), list) for name in OHLC_FIELDS):
        raise ValueError("OHLC path must contain four lists")
    horizon = len(path["opens"])
    problem = ohlc_path_problem(path, index=0, horizon_bars=horizon)
    if problem is not None:
        raise ValueError(problem)
    corrected = {name: list(path[name]) for name in OHLC_FIELDS}
    changed_field_counts = dict.fromkeys(OHLC_FIELDS, 0)
    absolute_delta_by_field = {
        name: {"total": 0.0, "max": 0.0} for name in OHLC_FIELDS
    }
    corrected_bar_count = 0
    for bar in range(len(path["opens"])):
        opened = path["opens"][bar]
        close = path["closes"][bar]
        high = max(path["highs"][bar], opened, close)
        low = min(path["lows"][bar], opened, close)
        values = {"highs": high, "lows": low}
        changed = False
        for name, value in values.items():
            original = path[name][bar]
            delta = _absolute_delta(value, original)
            if delta:
                changed = True
                changed_field_counts[name] += 1
                total = absolute_delta_by_field[name]["total"] + delta
                if not math.isfinite(total):
                    raise ValueError("OHLC correction total is non-finite")
                absolute_delta_by_field[name]["total"] = total
                absolute_delta_by_field[name]["max"] = max(
                    absolute_delta_by_field[name]["max"], delta,
                )
            corrected[name][bar] = value
        if changed:
            corrected_bar_count += 1
    provenance = {
        "algorithm": OHLC_ALGORITHM,
        "version": OHLC_ALGORITHM_VERSION,
        "corrected_bar_count": corrected_bar_count,
        "changed_field_counts": changed_field_counts,
        "absolute_delta_by_field": absolute_delta_by_field,
    }
    return corrected, provenance


def ohlc_quality(paths: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate correction status and audit measurements for normalized paths."""
    provenance = aggregate_ohlc_provenance(paths)
    corrected_bars = provenance["corrected_bar_count"]
    return {
        "status": "corrected" if corrected_bars else "consistent",
        "algorithm": OHLC_ALGORITHM,
        "version": OHLC_ALGORITHM_VERSION,
        "path_count": len(paths),
        "corrected_path_count": provenance["corrected_path_count"],
        "corrected_bar_count": corrected_bars,
        "changed_field_counts": provenance["changed_field_counts"],
        "absolute_delta_by_field": provenance["absolute_delta_by_field"],
        "reason": (
            "Finite positive OHLC wick bounds were corrected; opens and closes were preserved."
            if corrected_bars else None
        ),
    }


def aggregate_ohlc_provenance(
    paths: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-path correction counts and absolute field deltas."""
    if not isinstance(paths, (list, tuple)):
        raise ValueError("OHLC paths must be a list or tuple")
    changed_field_counts = dict.fromkeys(OHLC_FIELDS, 0)
    absolute_delta_by_field = {
        name: {"total": 0.0, "max": 0.0} for name in OHLC_FIELDS
    }
    corrected_bar_count = 0
    corrected_path_count = 0
    for path in paths:
        if not isinstance(path, dict):
            raise ValueError("OHLC path must be an object")
        raw = path.get("raw_ohlc")
        provenance = path.get("ohlc_provenance")
        if not _provenance_is_valid(provenance):
            raise ValueError("OHLC provenance is malformed")
        expected, expected_provenance = correct_ohlc_path(raw)
        if any(path.get(name) != expected[name] for name in OHLC_FIELDS):
            raise ValueError("OHLC path does not match the approved correction")
        if provenance != expected_provenance:
            raise ValueError("OHLC provenance does not match the path arrays")
        path_provenance = provenance
        path_corrected_bars = path_provenance["corrected_bar_count"]
        corrected_bar_count += path_corrected_bars
        corrected_path_count += bool(path_corrected_bars)
        for name in OHLC_FIELDS:
            changed_field_counts[name] += int(
                path_provenance.get("changed_field_counts", {}).get(name, 0)
            )
            deltas = path_provenance.get("absolute_delta_by_field", {}).get(name, {})
            total = absolute_delta_by_field[name]["total"] + float(deltas.get("total", 0.0))
            if not math.isfinite(total):
                raise ValueError("OHLC aggregate delta is non-finite")
            absolute_delta_by_field[name]["total"] = total
            absolute_delta_by_field[name]["max"] = max(
                absolute_delta_by_field[name]["max"], float(deltas.get("max", 0.0)),
            )
    return {
        "algorithm": OHLC_ALGORITHM,
        "version": OHLC_ALGORITHM_VERSION,
        "path_count": len(paths),
        "corrected_path_count": corrected_path_count,
        "corrected_bar_count": corrected_bar_count,
        "changed_field_counts": changed_field_counts,
        "absolute_delta_by_field": absolute_delta_by_field,
    }


def _provenance_is_valid(provenance: Any) -> bool:
    fields = {"algorithm", "version", "corrected_bar_count", "changed_field_counts", "absolute_delta_by_field"}
    if not isinstance(provenance, dict) or set(provenance) != fields:
        return False
    if provenance.get("algorithm") != OHLC_ALGORITHM:
        return False
    if type(provenance.get("version")) is not int or provenance["version"] != OHLC_ALGORITHM_VERSION:
        return False
    corrected_bars = provenance.get("corrected_bar_count")
    if type(corrected_bars) is not int or corrected_bars < 0:
        return False
    counts = provenance.get("changed_field_counts")
    deltas = provenance.get("absolute_delta_by_field")
    if not isinstance(counts, dict) or set(counts) != set(OHLC_FIELDS):
        return False
    if not isinstance(deltas, dict) or set(deltas) != set(OHLC_FIELDS):
        return False
    for name in OHLC_FIELDS:
        if type(counts[name]) is not int or counts[name] < 0:
            return False
        values = deltas[name]
        if not isinstance(values, dict) or set(values) != {"total", "max"}:
            return False
        if any(type(value) is not float or not math.isfinite(value) or value < 0 for value in values.values()):
            return False
    return True


def _copy_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": provenance["algorithm"],
        "version": provenance["version"],
        "corrected_bar_count": int(provenance["corrected_bar_count"]),
        "changed_field_counts": {
            name: int(provenance["changed_field_counts"][name]) for name in OHLC_FIELDS
        },
        "absolute_delta_by_field": {
            name: {
                "total": float(provenance["absolute_delta_by_field"][name]["total"]),
                "max": float(provenance["absolute_delta_by_field"][name]["max"]),
            }
            for name in OHLC_FIELDS
        },
    }


def _series_problem(
    values: Any, *, name: str, index: int, horizon_bars: int,
) -> str | None:
    if not isinstance(values, list) or len(values) != horizon_bars:
        return (
            f"path {index} {name} does not cover the {horizon_bars}-bar horizon "
            f"(got {len(values) if isinstance(values, list) else type(values).__name__})"
        )
    for value in values:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"path {index} {name} carries a non-numeric value"
        try:
            numeric = float(value)
            valid = math.isfinite(numeric) and numeric > 0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            return f"path {index} {name} carries a non-finite or non-positive price {value!r}"
    return None


def _absolute_delta(corrected: int | float, original: int | float) -> float:
    try:
        delta = float(abs(corrected - original))
    except (OverflowError, TypeError, ValueError):
        raise ValueError("OHLC correction delta overflowed") from None
    if not math.isfinite(delta):
        raise ValueError("OHLC correction delta is non-finite")
    return delta


__all__ = [
    "OHLC_ALGORITHM",
    "OHLC_ALGORITHM_VERSION",
    "OHLC_FIELDS",
    "aggregate_ohlc_provenance",
    "correct_ohlc_path",
    "normalize_ohlc_path",
    "ohlc_path_problem",
    "ohlc_quality",
]
