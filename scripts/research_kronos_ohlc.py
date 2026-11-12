"""Offline paired Kronos OHLC assessment and public-data freezer.

The default mode is offline: it reads a frozen dataset and a frozen raw-path
artifact, then compares the exact same model draws before and after the shared
OHLC correction API.  ``--fetch`` is the only network-enabled mode.  It uses
an unauthenticated CCXT Binance USD-M client, requests at most 500 candles per
page, and writes only to the operator's research directory outside this repo.

This is retrospective historical research.  It does not establish prospective
out-of-sample skill, profitability, or a trading result.  Hypothetical levels
in the report are predeclared measurement probes, not orders or recommendations.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "kronos_ohlc_dataset_v1"
PATH_SCHEMA = "kronos_raw_paths_v1"
REPORT_SCHEMA = "kronos_paired_assessment_v1"
TIMEFRAME = "4h"
BAR_MS = 14_400_000
LOOKBACK_BARS = 2_048
HORIZON_BARS = 6
PATH_COUNT = 16
PAGE_LIMIT = 500
PROFILE = "mini_2k"
PROFILES = ("mini_2k", "small_base")
DEFAULT_LEVEL_PCT = 0.01
SEED_START = 10_000_000
SEED_STRIDE = 10_000
SYMBOLS = (
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
)
CUTOFFS_UTC = (
    "2024-01-01T00:00:00Z",
    "2025-01-01T00:00:00Z",
    "2025-12-01T00:00:00Z",
)
_OHLC_FIELDS = ("opens", "highs", "lows", "closes")
_BAR_FIELDS = ("open", "high", "low", "close")


class ResearchError(ValueError):
    """A dataset, path artifact, or assessment cannot be trusted."""


def _utc_iso(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchError(f"invalid UTC timestamp {text!r}") from exc
    if parsed.tzinfo is None:
        raise ResearchError(f"timestamp must be timezone-aware: {text!r}")
    return parsed.astimezone(UTC)


def _epoch_ms(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchError(f"{field} must be an integer epoch millisecond")
    return value


def _finite_positive(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchError(f"{field} must be a numeric price")
    try:
        numeric = float(value)
        valid = math.isfinite(numeric) and numeric > 0
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ResearchError(f"{field} must be finite and positive, got {value!r}")
    return numeric


def _finite_nonnegative(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchError(f"{field} must be a numeric volume")
    try:
        numeric = float(value)
        valid = math.isfinite(numeric) and numeric >= 0
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ResearchError(f"{field} must be finite and non-negative, got {value!r}")
    return numeric


def canonical_digest(payload: Any) -> str:
    """Hash JSON without accepting NaN/Infinity or platform-dependent spacing."""
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"payload cannot be canonically hashed: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path: Path) -> str:
    """Digest an input artifact exactly as stored on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_output_dir() -> Path:
    """Return the user-profile research directory, never the repository."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return root / "tradingagents-research" / "kronos-ohlc"


def window_id(symbol: str, cutoff_ms: int) -> str:
    return f"{symbol}|{cutoff_ms}"


def _dataset_material(dataset: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": dataset.get("schema"),
        "metadata": dataset.get("metadata"),
        "windows": dataset.get("windows"),
    }


def dataset_digest(dataset: Mapping[str, Any]) -> str:
    return canonical_digest(_dataset_material(dataset))


def _path_material(paths_artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": paths_artifact.get("schema"),
        "metadata": paths_artifact.get("metadata"),
        "windows": paths_artifact.get("windows"),
    }


def path_artifact_digest(paths_artifact: Mapping[str, Any]) -> str:
    return canonical_digest(_path_material(paths_artifact))


def window_digest(window: Mapping[str, Any]) -> str:
    material = {
        "symbol": window.get("symbol"),
        "timeframe": window.get("timeframe"),
        "cutoff_ms": window.get("cutoff_ms"),
        "history": window.get("history"),
        "future": window.get("future"),
    }
    return canonical_digest(material)


def _window_sort_key(window: Mapping[str, Any]) -> tuple[str, int]:
    return str(window.get("symbol")), int(window.get("cutoff_ms", -1))


def seed_plan(
    windows: Sequence[Mapping[str, Any]],
    *,
    path_count: int = PATH_COUNT,
    seed_start: int = SEED_START,
    seed_stride: int = SEED_STRIDE,
) -> dict[str, dict[str, int]]:
    """Assign disjoint deterministic ranges independent of input order."""
    if path_count < 1 or seed_stride < path_count:
        raise ResearchError("seed_stride must leave every path range disjoint")
    ordered = sorted(windows, key=_window_sort_key)
    plan: dict[str, dict[str, int]] = {}
    for rank, window in enumerate(ordered):
        identifier = str(window.get("window_id") or "")
        if not identifier:
            raise ResearchError("every window needs a window_id for seed planning")
        base = seed_start + rank * seed_stride
        plan[identifier] = {
            "seed_base": base,
            "seed_first": base,
            "seed_last": base + path_count - 1,
        }
    return plan


def _bar_problem(
    bar: Mapping[str, Any],
    *,
    symbol: str,
    expected_open: int,
    now_ms: int,
    label: str,
) -> None:
    if not isinstance(bar, Mapping):
        raise ResearchError(f"{label} is not an object")
    if bar.get("symbol") != symbol:
        raise ResearchError(f"{label} symbol does not match {symbol!r}")
    if bar.get("timeframe") != TIMEFRAME:
        raise ResearchError(f"{label} timeframe is not {TIMEFRAME!r}")
    if bar.get("closed") is not True:
        raise ResearchError(f"{label} is not explicitly marked closed")
    open_time = _epoch_ms(bar.get("open_time"), field=f"{label}.open_time")
    close_time = _epoch_ms(bar.get("close_time"), field=f"{label}.close_time")
    if open_time != expected_open:
        raise ResearchError(
            f"{label} has open_time={open_time}, expected contiguous {expected_open}"
        )
    if close_time - open_time != BAR_MS:
        raise ResearchError(f"{label} duration is not exactly one 4h bar")
    if close_time > now_ms:
        raise ResearchError(f"{label} is not realized by validation time")
    prices = {
        field: _finite_positive(bar.get(field), field=f"{label}.{field}")
        for field in _BAR_FIELDS
    }
    _finite_nonnegative(bar.get("volume"), field=f"{label}.volume")
    if prices["high"] < max(prices["open"], prices["close"]):
        raise ResearchError(f"{label} high is below open/close")
    if prices["low"] > min(prices["open"], prices["close"]):
        raise ResearchError(f"{label} low is above open/close")
    if prices["high"] < prices["low"]:
        raise ResearchError(f"{label} high is below low")


def validate_dataset(
    dataset: Mapping[str, Any],
    *,
    now: datetime | None = None,
    expected_symbols: Sequence[str] = SYMBOLS,
    expected_cutoffs: Sequence[str] = CUTOFFS_UTC,
) -> dict[str, Any]:
    """Validate a frozen, contiguous 2048+6 closed-bar dataset."""
    if not isinstance(dataset, Mapping) or dataset.get("schema") != SCHEMA:
        raise ResearchError(f"dataset schema must be {SCHEMA!r}")
    metadata = dataset.get("metadata")
    windows = dataset.get("windows")
    if not isinstance(metadata, Mapping) or not isinstance(windows, list):
        raise ResearchError("dataset needs metadata and a windows list")
    if metadata.get("timeframe") != TIMEFRAME:
        raise ResearchError(f"dataset timeframe must be {TIMEFRAME!r}")
    if metadata.get("lookback_bars") != LOOKBACK_BARS:
        raise ResearchError(f"dataset lookback must be {LOOKBACK_BARS}")
    if metadata.get("horizon_bars") != HORIZON_BARS:
        raise ResearchError(f"dataset horizon must be {HORIZON_BARS}")
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise ResearchError("dataset metadata must include source provenance")
    if source.get("authenticated") is not False:
        raise ResearchError("dataset source must explicitly be unauthenticated")
    if len(windows) < 2:
        raise ResearchError("dataset needs at least two decision cutoffs")

    expected_symbol_set = set(expected_symbols)
    expected_cutoff_set = {
        int(_parse_utc(text).timestamp() * 1000) for text in expected_cutoffs
    }
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, int]] = set()
    validated_symbols: set[str] = set()
    validated_cutoffs: set[int] = set()
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ResearchError("validation time must be timezone-aware")
    now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    now_ms = int(now_utc.timestamp() * 1000)

    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            raise ResearchError(f"window {index} is not an object")
        symbol = window.get("symbol")
        cutoff_ms = _epoch_ms(window.get("cutoff_ms"), field=f"window {index}.cutoff_ms")
        if not isinstance(symbol, str) or not symbol:
            raise ResearchError(f"window {index} has no symbol")
        identifier = window.get("window_id")
        expected_id = window_id(symbol, cutoff_ms)
        if identifier != expected_id:
            raise ResearchError(f"window {index} id is not {expected_id!r}")
        if identifier in seen_ids:
            raise ResearchError(f"duplicate window_id {identifier!r}")
        seen_ids.add(identifier)
        pair = (symbol, cutoff_ms)
        if pair in seen_pairs:
            raise ResearchError(f"duplicate symbol/cutoff pair {pair!r}")
        seen_pairs.add(pair)
        validated_symbols.add(symbol)
        validated_cutoffs.add(cutoff_ms)
        if expected_symbol_set and symbol not in expected_symbol_set:
            raise ResearchError(f"unexpected symbol {symbol!r}")
        if expected_cutoff_set and cutoff_ms not in expected_cutoff_set:
            raise ResearchError(f"unexpected cutoff {_utc_iso(cutoff_ms)}")
        if cutoff_ms % BAR_MS:
            raise ResearchError(f"cutoff {_utc_iso(cutoff_ms)} is not on a 4h boundary")
        if window.get("timeframe") != TIMEFRAME:
            raise ResearchError(f"window {identifier} timeframe is not {TIMEFRAME!r}")
        if window.get("status") != "ok":
            raise ResearchError(
                f"window {identifier} is unavailable: {window.get('reason') or 'no reason'}"
            )
        history = window.get("history")
        future = window.get("future")
        if not isinstance(history, list) or len(history) != LOOKBACK_BARS:
            raise ResearchError(f"window {identifier} must contain exactly {LOOKBACK_BARS} history bars")
        if not isinstance(future, list) or len(future) != HORIZON_BARS:
            raise ResearchError(f"window {identifier} must contain exactly {HORIZON_BARS} future bars")
        first_history_open = cutoff_ms - LOOKBACK_BARS * BAR_MS
        for bar_index, bar in enumerate(history):
            _bar_problem(
                bar, symbol=symbol, expected_open=first_history_open + bar_index * BAR_MS,
                now_ms=now_ms, label=f"{identifier}.history[{bar_index}]",
            )
            if bar["close_time"] > cutoff_ms:
                raise ResearchError(f"{identifier} history leaks beyond the cutoff")
        if history[-1]["close_time"] != cutoff_ms:
            raise ResearchError(f"{identifier} latest history close is not the cutoff")
        for bar_index, bar in enumerate(future):
            _bar_problem(
                bar, symbol=symbol, expected_open=cutoff_ms + bar_index * BAR_MS,
                now_ms=now_ms, label=f"{identifier}.future[{bar_index}]",
            )
            if bar["open_time"] < cutoff_ms or bar["close_time"] <= cutoff_ms:
                raise ResearchError(f"{identifier} future bar crosses the cutoff incorrectly")
        combined_opens = [bar["open_time"] for bar in [*history, *future]]
        if len(combined_opens) != len(set(combined_opens)):
            raise ResearchError(f"{identifier} contains duplicate bar open times")
        stored_range = window.get("range")
        expected_range = {
            "history_first_open_ms": history[0]["open_time"],
            "history_last_close_ms": history[-1]["close_time"],
            "future_first_open_ms": future[0]["open_time"],
            "future_last_close_ms": future[-1]["close_time"],
        }
        if stored_range != expected_range:
            raise ResearchError(f"{identifier} range metadata does not match its bars")
        digest = window.get("digest")
        expected_digest = window_digest(window)
        if digest != expected_digest:
            raise ResearchError(f"{identifier} digest does not match its bars")
        validated_cutoffs.add(cutoff_ms)

    if len(validated_symbols) < 3:
        raise ResearchError("assessment requires at least three symbols")
    if expected_symbol_set and validated_symbols != expected_symbol_set:
        raise ResearchError(
            f"dataset symbols {sorted(validated_symbols)} do not match {sorted(expected_symbol_set)}"
        )
    if expected_cutoff_set and validated_cutoffs != expected_cutoff_set:
        raise ResearchError("dataset cutoffs do not match the frozen assessment plan")
    expected_pairs = {
        (symbol, cutoff_ms)
        for symbol in (expected_symbol_set or validated_symbols)
        for cutoff_ms in (expected_cutoff_set or validated_cutoffs)
    }
    if seen_pairs != expected_pairs:
        missing = sorted(expected_pairs - seen_pairs)
        raise ResearchError(f"dataset is missing symbol/cutoff pairs: {missing!r}")
    manifest = dataset.get("manifest")
    expected_dataset_digest = dataset_digest(dataset)
    if not isinstance(manifest, Mapping) or manifest.get("dataset_digest") != expected_dataset_digest:
        raise ResearchError("dataset manifest digest is missing or does not match its content")
    if manifest.get("status") != "ok" or manifest.get("unavailable_windows") != []:
        raise ResearchError("dataset manifest does not describe a complete dataset")
    return {
        "schema": SCHEMA,
        "windows": len(windows),
        "symbols": sorted(validated_symbols),
        "cutoffs_utc": [_utc_iso(value) for value in sorted(validated_cutoffs)],
        "dataset_digest": expected_dataset_digest,
        "latest_future_close_ms": max(
            window["future"][-1]["close_time"] for window in windows
        ),
    }


def _raw_path_problem(path: Any, *, index: int, horizon_bars: int) -> str | None:
    """Validate raw path shape/finiteness while allowing OHLC bound violations."""
    try:
        from tradingagents.forecast.ohlc import ohlc_path_problem
    except (ImportError, AttributeError) as exc:
        raise ResearchError("tradingagents.forecast.ohlc.ohlc_path_problem is unavailable") from exc
    problem = ohlc_path_problem(path, index=index, horizon_bars=horizon_bars)
    if problem is not None:
        return problem
    if not isinstance(path, Mapping):
        return f"path {index} is not an object"
    seed = path.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        return f"path {index} has no integer seed"
    return None


def _corrected_path_problem(path: Mapping[str, Sequence[Any]], *, horizon_bars: int) -> str | None:
    if not isinstance(path, Mapping):
        return "corrected path is not an object"
    for field in _OHLC_FIELDS:
        values = path.get(field)
        if not isinstance(values, (list, tuple)) or len(values) != horizon_bars:
            return f"corrected path {field} does not cover the {horizon_bars}-bar horizon"
        for value in values:
            try:
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    and float(value) > 0
                )
            except (OverflowError, ValueError):
                valid = False
            if not valid:
                return f"corrected path {field} contains a non-finite or non-positive value"
    for opened, high, low, close in zip(
        path["opens"], path["highs"], path["lows"], path["closes"], strict=True,
    ):
        if high < max(opened, close) or low > min(opened, close) or high < low:
            return "corrected OHLC still violates high/open/close/low ordering"
    return None


def _ohlc_quality(path: Mapping[str, Sequence[float]]) -> dict[str, Any]:
    inconsistent_bars = sum(
        high < max(opened, close) or low > min(opened, close) or high < low
        for opened, high, low, close in zip(
            path["opens"], path["highs"], path["lows"], path["closes"], strict=True,
        )
    )
    return {
        "status": "inconsistent" if inconsistent_bars else "consistent",
        "inconsistent_bar_count": inconsistent_bars,
    }


def _load_corrector() -> Callable[..., tuple[dict[str, list[Any]], dict[str, Any]]]:
    try:
        from tradingagents.forecast.ohlc import correct_ohlc_path
    except (ImportError, AttributeError) as exc:
        raise ResearchError(
            "tradingagents.forecast.ohlc.correct_ohlc_path is unavailable"
        ) from exc
    return correct_ohlc_path


def _validate_provenance(
    raw: Mapping[str, Sequence[float]],
    corrected: Mapping[str, Sequence[float]],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(provenance, Mapping):
        raise ResearchError("correct_ohlc_path returned no ohlc_provenance mapping")
    algorithm = provenance.get("algorithm")
    version = provenance.get("version")
    if not isinstance(algorithm, str) or not algorithm:
        raise ResearchError("ohlc_provenance.algorithm is required")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ResearchError("ohlc_provenance.version must be an integer")
    changed_fields = {
        field: sum(a != b for a, b in zip(raw[field], corrected[field], strict=True))
        for field in _OHLC_FIELDS
    }
    corrected_bars = sum(
        any(raw[field][index] != corrected[field][index] for field in _OHLC_FIELDS)
        for index in range(len(raw["opens"]))
    )
    deltas = {
        field: {
            "total": sum(abs(float(a) - float(b)) for a, b in zip(raw[field], corrected[field], strict=True)),
            "max": max(
                (abs(float(a) - float(b)) for a, b in zip(raw[field], corrected[field], strict=True)),
                default=0.0,
            ),
        }
        for field in _OHLC_FIELDS
    }
    if provenance.get("corrected_bar_count") != corrected_bars:
        raise ResearchError("ohlc_provenance.corrected_bar_count disagrees with arrays")
    if provenance.get("changed_field_counts") != changed_fields:
        raise ResearchError("ohlc_provenance.changed_field_counts disagrees with arrays")
    raw_deltas = provenance.get("absolute_delta_by_field")
    if not isinstance(raw_deltas, Mapping):
        raise ResearchError("ohlc_provenance.absolute_delta_by_field is required")
    for field, expected in deltas.items():
        actual = raw_deltas.get(field)
        if not isinstance(actual, Mapping):
            raise ResearchError(f"absolute_delta_by_field.{field} is missing")
        for name in ("total", "max"):
            try:
                matches = math.isclose(float(actual.get(name)), expected[name], rel_tol=1e-12, abs_tol=1e-12)
            except (TypeError, ValueError, OverflowError):
                matches = False
            if not matches:
                raise ResearchError(f"absolute_delta_by_field.{field}.{name} disagrees with arrays")
    expected_status = "corrected" if corrected_bars else "consistent"
    status = provenance.get("status", expected_status)
    if status != expected_status:
        raise ResearchError(f"ohlc_provenance.status must be {expected_status!r}")
    return {
        **dict(provenance),
        "status": expected_status,
        "algorithm": algorithm,
        "version": version,
        "corrected_bar_count": corrected_bars,
        "changed_field_counts": changed_fields,
        "absolute_delta_by_field": deltas,
    }


def _call_corrector(
    raw_path: Mapping[str, Any],
    *,
    corrector: Callable[[dict[str, list[Any]]], tuple[Mapping[str, Sequence[Any]], Mapping[str, Any]]],
    algorithm_version: int,
) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, Any]]:
    raw = {field: list(raw_path[field]) for field in _OHLC_FIELDS}
    returned = corrector(copy.deepcopy(raw))
    if not isinstance(returned, tuple) or len(returned) != 2:
        raise ResearchError("correct_ohlc_path must return (corrected, provenance)")
    corrected_raw, provenance_raw = returned
    if not isinstance(corrected_raw, Mapping) or not isinstance(provenance_raw, Mapping):
        raise ResearchError("correct_ohlc_path returned malformed corrected/provenance values")
    corrected: dict[str, list[float]] = {}
    for field in _OHLC_FIELDS:
        values = corrected_raw.get(field)
        if not isinstance(values, list):
            raise ResearchError(f"correct_ohlc_path returned no corrected {field}")
        corrected[field] = list(values)
    if provenance_raw.get("algorithm") != "ohlc_wick_bounds_v1":
        raise ResearchError("correct_ohlc_path returned an unexpected algorithm")
    if provenance_raw.get("version") != algorithm_version:
        raise ResearchError("correct_ohlc_path returned an unexpected algorithm version")
    if len(corrected["opens"]) != len(raw["opens"]):
        raise ResearchError("correct_ohlc_path changed the horizon length")
    if corrected["opens"] != raw["opens"] or corrected["closes"] != raw["closes"]:
        raise ResearchError("correct_ohlc_path changed open/close values")
    expected_highs = [max(high, opened, close) for high, opened, close in zip(
        raw["highs"], raw["opens"], raw["closes"], strict=True,
    )]
    expected_lows = [min(low, opened, close) for low, opened, close in zip(
        raw["lows"], raw["opens"], raw["closes"], strict=True,
    )]
    if corrected["highs"] != expected_highs or corrected["lows"] != expected_lows:
        raise ResearchError("correct_ohlc_path did not apply exact wick bounds")
    problem = _corrected_path_problem(corrected, horizon_bars=len(raw["opens"]))
    if problem is not None:
        raise ResearchError(problem)
    provenance = _validate_provenance(raw, corrected, provenance_raw)
    return raw, corrected, provenance


def _first_touch(
    path: Mapping[str, Sequence[float]], *, stop: float, target: float,
) -> dict[str, Any]:
    """Measure the first hypothetical long-probe touch using candle-open precedence."""
    for index, (opened, high, low) in enumerate(
        zip(path["opens"], path["highs"], path["lows"], strict=True), start=1,
    ):
        if opened <= stop or opened >= target:
            return {
                "label": "stop" if opened <= stop else "target",
                "step": index,
                "ambiguous": False,
                "policy": "open",
                "semantics": "Hypothetical level touch; not an order, fill, or trade outcome.",
            }
        hit_stop = low <= stop
        hit_target = high >= target
        if not hit_stop and not hit_target:
            continue
        ambiguous = hit_stop and hit_target
        return {
            "label": "stop" if hit_stop else "target",
            "step": index,
            "ambiguous": ambiguous,
            "policy": "worst_first_stop" if ambiguous else "single_level",
            "semantics": "Hypothetical level touch; not an order, fill, or trade outcome.",
        }
    return {
        "label": "none", "step": None, "ambiguous": False, "policy": "none",
        "semantics": "Hypothetical level touch; not an order, fill, or trade outcome.",
    }


def _path_metrics(
    path: Mapping[str, Sequence[float]],
    *,
    reference_close: float,
    realized: Mapping[str, Any],
    levels: Mapping[str, float],
) -> dict[str, Any]:
    endpoint = float(path["closes"][-1])
    predicted_high = max(float(value) for value in path["highs"])
    predicted_low = min(float(value) for value in path["lows"])
    actual_touch = realized["touch"]
    model_touch = _first_touch(path, stop=levels["stop"], target=levels["target"])
    same_timed_touch = (
        model_touch["label"] == actual_touch["label"]
        and model_touch["step"] is not None
        and actual_touch["step"] is not None
        and not model_touch["ambiguous"]
        and not actual_touch["ambiguous"]
    )
    return {
        "endpoint_close": endpoint,
        "endpoint_signed_error": endpoint - realized["close"],
        "endpoint_abs_error": abs(endpoint - realized["close"]),
        "endpoint_relative_abs_error": abs(endpoint - realized["close"]) / realized["close"],
        "direction": "up" if endpoint > reference_close else "down" if endpoint < reference_close else "flat",
        "high": predicted_high,
        "high_signed_error": predicted_high - realized["high"],
        "high_abs_error": abs(predicted_high - realized["high"]),
        "low": predicted_low,
        "low_signed_error": predicted_low - realized["low"],
        "low_abs_error": abs(predicted_low - realized["low"]),
        "touch": model_touch,
        "touch_timing_error_steps": (
            model_touch["step"] - actual_touch["step"] if same_timed_touch else None
        ),
    }


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _variant_summary(
    records: Sequence[Mapping[str, Any]], *, raw: bool = False,
) -> dict[str, Any]:
    valid_touch = not raw or all(
        record.get("ohlc_quality", {}).get("status") == "consistent" for record in records
    )
    labels = Counter(record["touch"]["label"] for record in records) if valid_touch else Counter()
    timing = [
        float(record["touch_timing_error_steps"])
        for record in records
        if valid_touch and record["touch_timing_error_steps"] is not None
    ]
    return {
        "paths": len(records),
        "endpoint": {
            "mae": _mean([float(record["endpoint_abs_error"]) for record in records]),
            "mean_signed_error": _mean([float(record["endpoint_signed_error"]) for record in records]),
            "relative_mae": _mean([float(record["endpoint_relative_abs_error"]) for record in records]),
        },
        "high": {
            "mae": _mean([float(record["high_abs_error"]) for record in records]),
            "mean_signed_error": _mean([float(record["high_signed_error"]) for record in records]),
        },
        "low": {
            "mae": _mean([float(record["low_abs_error"]) for record in records]),
            "mean_signed_error": _mean([float(record["low_signed_error"]) for record in records]),
        },
        "touch": {
            "status": "ok" if valid_touch else "unavailable",
            "semantics": "Hypothetical path touch; not an order, fill, or trade outcome.",
            "reason": None if valid_touch else "at least one raw path has invalid OHLC geometry",
            "label_counts": dict(sorted(labels.items())) if valid_touch else None,
            "ambiguous_count": sum(bool(record["touch"]["ambiguous"]) for record in records) if valid_touch else None,
            "timing_compared_paths": len(timing),
            "mean_abs_timing_error_steps": _mean([abs(value) for value in timing]),
            "mean_signed_timing_error_steps": _mean(timing),
        },
    }


def _realized_summary(future: Sequence[Mapping[str, Any]], *, levels: Mapping[str, float]) -> dict[str, Any]:
    close = float(future[-1]["close"])
    high = max(float(bar["high"]) for bar in future)
    low = min(float(bar["low"]) for bar in future)
    actual_path = {
        "opens": [float(bar["open"]) for bar in future],
        "highs": [float(bar["high"]) for bar in future],
        "lows": [float(bar["low"]) for bar in future],
    }
    return {
        "close": close,
        "high": high,
        "low": low,
        "bars": len(future),
        "touch": _first_touch(actual_path, stop=levels["stop"], target=levels["target"]),
    }


def _aggregate_corrections(provenances: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    changed_counts = dict.fromkeys(_OHLC_FIELDS, 0)
    deltas = {field: {"total": 0.0, "max": 0.0} for field in _OHLC_FIELDS}
    corrected_bars = 0
    statuses = set()
    for provenance in provenances:
        corrected_bars += int(provenance["corrected_bar_count"])
        statuses.add(provenance["status"])
        for field in _OHLC_FIELDS:
            changed_counts[field] += int(provenance["changed_field_counts"][field])
            deltas[field]["total"] += float(provenance["absolute_delta_by_field"][field]["total"])
            deltas[field]["max"] = max(
                deltas[field]["max"], float(provenance["absolute_delta_by_field"][field]["max"]),
            )
    return {
        "status": "corrected" if "corrected" in statuses else "consistent",
        "corrected_bar_count": corrected_bars,
        "changed_field_counts": changed_counts,
        "absolute_delta_by_field": deltas,
    }


def validate_paths_artifact(
    paths_artifact: Mapping[str, Any],
    dataset: Mapping[str, Any],
    *,
    path_count: int = PATH_COUNT,
    expected_profile: str | None = None,
) -> dict[str, Any]:
    """Validate one frozen raw-path generation before any correction."""
    if not isinstance(paths_artifact, Mapping) or paths_artifact.get("schema") != PATH_SCHEMA:
        raise ResearchError(f"path artifact schema must be {PATH_SCHEMA!r}")
    metadata = paths_artifact.get("metadata")
    windows = paths_artifact.get("windows")
    if not isinstance(metadata, Mapping) or not isinstance(windows, list):
        raise ResearchError("path artifact needs metadata and windows")
    profile = metadata.get("profile")
    if profile not in PROFILES:
        raise ResearchError("path artifact profile must be mini_2k or small_base")
    if expected_profile is not None and profile != expected_profile:
        raise ResearchError(f"path artifact profile {profile!r} does not match {expected_profile!r}")
    expected_dataset_digest = dataset_digest(dataset)
    artifact_dataset_digest = metadata.get("dataset_digest")
    if artifact_dataset_digest != expected_dataset_digest:
        raise ResearchError("path artifact dataset_digest does not match the frozen dataset")
    if metadata.get("timeframe") != TIMEFRAME:
        raise ResearchError("path artifact timeframe must be 4h")
    if metadata.get("horizon_bars") != HORIZON_BARS or metadata.get("path_count") != path_count:
        raise ResearchError("path artifact horizon/path count does not match the assessment")
    if metadata.get("raw_generated_once") is not True:
        raise ResearchError("path artifact must explicitly declare one raw generation")
    generation = metadata.get("generation")
    if not isinstance(generation, Mapping):
        raise ResearchError("path artifact generation parameters are required")
    for key, upper in (("temperature", 2.0), ("top_p", 1.0)):
        value = generation.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResearchError(f"generation.{key} must be a finite positive number")
        if not math.isfinite(value) or not 0 < value <= upper:
            raise ResearchError(f"generation.{key} is outside the supported range")
    input_bars = generation.get("input_bars")
    if type(input_bars) is not int or input_bars != LOOKBACK_BARS:
        raise ResearchError(f"generation.input_bars must be {LOOKBACK_BARS}")
    runner = metadata.get("runner")
    if not isinstance(runner, Mapping):
        raise ResearchError("path artifact must include runner revisions and eval stage")
    for key in ("source_revision", "model_revision", "tokenizer_revision", "artifact_digest"):
        if not isinstance(runner.get(key), str) or not runner[key]:
            raise ResearchError(f"path artifact runner.{key} is required")
    from tradingagents.forecast.artifact import artifact_digest, profile_for

    pinned_profile = profile_for(profile)
    expected_effective = min(input_bars, pinned_profile.max_context)
    if type(generation.get("effective_context_bars")) is not int or generation["effective_context_bars"] != expected_effective:
        raise ResearchError("generation.effective_context_bars does not match the profile")
    if generation.get("max_context") != pinned_profile.max_context:
        raise ResearchError("generation.max_context does not match the pinned profile")
    pinned_values = {
        "source_revision": pinned_profile.source_revision,
        "model_revision": pinned_profile.model_revision,
        "tokenizer_revision": pinned_profile.tokenizer_revision,
        "artifact_digest": artifact_digest(profile),
    }
    for key, expected in pinned_values.items():
        if runner[key] != expected:
            raise ResearchError(f"path artifact runner.{key} does not match pinned profile {profile!r}")
    runner_stage = runner.get("model_eval_stage")
    artifact_stage = metadata.get("model_eval_stage")
    if runner_stage is not None and artifact_stage is not None and runner_stage != artifact_stage:
        raise ResearchError("runner model_eval_stage disagrees with artifact metadata")
    eval_stage = runner_stage if runner_stage is not None else artifact_stage
    if eval_stage not in ("before_model_eval", "after_model_eval"):
        raise ResearchError("runner.model_eval_stage must distinguish before/after model.eval")
    dataset_windows = dataset["windows"]
    dataset_ids = {window["window_id"] for window in dataset_windows}
    plan = seed_plan(dataset_windows, path_count=path_count)
    seen: set[str] = set()
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            raise ResearchError(f"path window {index} is not an object")
        identifier = window.get("window_id")
        if identifier not in dataset_ids:
            raise ResearchError(f"path window {identifier!r} is not in the dataset")
        if identifier in seen:
            raise ResearchError(f"duplicate path window {identifier!r}")
        seen.add(identifier)
        expected = plan[identifier]
        if window.get("seed_base") != expected["seed_base"]:
            raise ResearchError(f"{identifier} seed_base does not match the disjoint seed plan")
        paths = window.get("paths")
        if not isinstance(paths, list) or len(paths) != path_count:
            raise ResearchError(f"{identifier} does not contain exactly {path_count} paths")
        for path_index, path in enumerate(paths):
            problem = _raw_path_problem(path, index=path_index, horizon_bars=HORIZON_BARS)
            if problem is not None:
                raise ResearchError(f"{identifier}: {problem}")
            expected_seed = expected["seed_base"] + path_index
            if path["seed"] != expected_seed:
                raise ResearchError(f"{identifier} path seed does not match its range")
        timing = window.get("timing")
        if timing is not None:
            if not isinstance(timing, Mapping):
                raise ResearchError(f"{identifier} timing is not an object")
            duration = timing.get("duration_ms")
            if duration is not None and (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or float(duration) < 0
            ):
                raise ResearchError(f"{identifier} timing duration is invalid")
    if seen != dataset_ids:
        raise ResearchError("path artifact does not cover every dataset window exactly once")
    digest = path_artifact_digest(paths_artifact)
    manifest = paths_artifact.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("path_artifact_digest") != digest:
        raise ResearchError("path artifact digest is missing or does not match its content")
    return {
        "schema": PATH_SCHEMA,
        "windows": len(windows),
        "path_count": path_count,
        "profile": profile,
        "model_eval_stage": eval_stage,
        "generation": dict(generation),
        "runner": dict(runner),
        "path_artifact_digest": digest,
        "seed_plan": plan,
    }


def assess_dataset(
    dataset: Mapping[str, Any],
    paths_artifact: Mapping[str, Any],
    *,
    corrector: Callable[..., Mapping[str, Any]] | None = None,
    algorithm_version: int = 1,
    level_pct: float = DEFAULT_LEVEL_PCT,
    now: datetime | None = None,
    variant_label: str | None = None,
) -> dict[str, Any]:
    """Pair exact raw draws with corrected paths and evaluate retrospective outcomes."""
    if type(algorithm_version) is not int or algorithm_version != 1:
        raise ResearchError("only correction algorithm version 1 is supported")
    if not math.isfinite(level_pct) or not 0 < level_pct < 1:
        raise ResearchError("hypothetical level percentage must be finite and in (0, 1)")
    dataset_info = validate_dataset(dataset, now=now)
    path_info = validate_paths_artifact(paths_artifact, dataset)
    corrector_fn = corrector or _load_corrector()
    path_by_id = {window["window_id"]: window for window in paths_artifact["windows"]}
    all_raw_records: list[dict[str, Any]] = []
    all_corrected_records: list[dict[str, Any]] = []
    all_baseline_records: list[dict[str, Any]] = []
    all_provenances: list[Mapping[str, Any]] = []
    report_windows: list[dict[str, Any]] = []

    for dataset_window in sorted(dataset["windows"], key=_window_sort_key):
        identifier = dataset_window["window_id"]
        history = dataset_window["history"]
        future = dataset_window["future"]
        reference_close = float(history[-1]["close"])
        levels = {
            "stop": reference_close * (1.0 - level_pct),
            "target": reference_close * (1.0 + level_pct),
            "definition": f"reference_close +/- {level_pct:.4%}; hypothetical measurement only",
        }
        realized = _realized_summary(future, levels=levels)
        raw_records: list[dict[str, Any]] = []
        corrected_records: list[dict[str, Any]] = []
        paired_paths: list[dict[str, Any]] = []
        provenances: list[Mapping[str, Any]] = []
        for raw_path in path_by_id[identifier]["paths"]:
            raw_snapshot, corrected, provenance = _call_corrector(
                raw_path, corrector=corrector_fn, algorithm_version=algorithm_version,
            )
            raw_quality = _ohlc_quality(raw_snapshot)
            corrected_quality = _ohlc_quality(corrected)
            raw_metrics = _path_metrics(
                raw_snapshot, reference_close=reference_close, realized=realized, levels=levels,
            )
            corrected_metrics = _path_metrics(
                corrected, reference_close=reference_close, realized=realized, levels=levels,
            )
            raw_metrics["ohlc_quality"] = raw_quality
            if raw_quality["status"] != "consistent":
                raw_metrics["touch"] = None
                raw_metrics["touch_timing_error_steps"] = None
            corrected_metrics["ohlc_quality"] = corrected_quality
            raw_records.append(raw_metrics)
            corrected_records.append(corrected_metrics)
            provenances.append(provenance)
            all_provenances.append(provenance)
            paired_paths.append({
                "seed": raw_path["seed"],
                "raw_ohlc": raw_snapshot,
                "corrected_ohlc": corrected,
                "ohlc_provenance": provenance,
                "raw_metrics": raw_metrics,
                "corrected_metrics": corrected_metrics,
            })
        baseline_records = [
            _path_metrics(
                {"opens": [reference_close] * HORIZON_BARS,
                 "highs": [reference_close] * HORIZON_BARS,
                 "lows": [reference_close] * HORIZON_BARS,
                 "closes": [reference_close] * HORIZON_BARS},
                reference_close=reference_close, realized=realized, levels=levels,
            )
            for _ in range(PATH_COUNT)
        ]
        raw_summary = _variant_summary(raw_records, raw=True)
        corrected_summary = _variant_summary(corrected_records)
        baseline_summary = _variant_summary(baseline_records)
        report_windows.append({
            "window_id": identifier,
            "symbol": dataset_window["symbol"],
            "cutoff_utc": dataset_window["cutoff_utc"],
            "cutoff_ms": dataset_window["cutoff_ms"],
            "reference_close": reference_close,
            "hypothetical_levels": levels,
            "realized": realized,
            "raw": raw_summary,
            "corrected": corrected_summary,
            "persistence_baseline": baseline_summary,
            "paired_deltas": {
                "endpoint_mae_corrected_minus_raw": corrected_summary["endpoint"]["mae"] - raw_summary["endpoint"]["mae"],
                "high_mae_corrected_minus_raw": corrected_summary["high"]["mae"] - raw_summary["high"]["mae"],
                "low_mae_corrected_minus_raw": corrected_summary["low"]["mae"] - raw_summary["low"]["mae"],
            },
            "raw_ohlc_quality": {
                "status": "inconsistent" if any(
                    record["ohlc_quality"]["status"] == "inconsistent" for record in raw_records
                ) else "consistent",
                "inconsistent_bar_count": sum(
                    record["ohlc_quality"]["inconsistent_bar_count"] for record in raw_records
                ),
            },
            "corrected_ohlc_quality": _aggregate_corrections(provenances),
            "paths": paired_paths,
            "timing": path_by_id[identifier].get("timing"),
        })
        all_raw_records.extend(raw_records)
        all_corrected_records.extend(corrected_records)
        all_baseline_records.extend(baseline_records)

    runner = path_info["runner"]
    return {
        "schema": REPORT_SCHEMA,
        "metadata": {
            "assessment": "offline_paired_kronos_ohlc",
            "profile": path_info["profile"],
            "model_eval_stage": path_info["model_eval_stage"],
            "variant_label": variant_label or (
                f"{path_info['profile']}_{path_info['model_eval_stage']}"
            ),
            "profiles_supported": list(PROFILES),
            "timeframe": TIMEFRAME,
            "dataset_history_bars": LOOKBACK_BARS,
            "generation": path_info["generation"],
            "horizon_bars": HORIZON_BARS,
            "dataset_digest": dataset_info["dataset_digest"],
            "path_artifact_digest": path_info["path_artifact_digest"],
            "runner": runner,
            "pairing": "same raw path seed, same cutoff, raw arrays preserved exactly",
            "retrospective_disclaimer": (
                "Historical retrospective using pre-known realized bars; this is not truly "
                "out-of-sample evidence and does not prove PnL or profitability."
            ),
            "hypothetical_levels_disclaimer": (
                "Levels are fixed measurement probes at +/- the declared percentage from "
                "the cutoff close; no actual trade, order, or execution is represented."
            ),
            "generated_at_utc": datetime.now(UTC).isoformat(),
        },
        "protocol": {
            "level_pct": level_pct,
            "correction_algorithm_version": algorithm_version,
            "raw_model_eval_stage": path_info["model_eval_stage"],
            "paired_raw_eval_baseline_required": path_info["model_eval_stage"] == "after_model_eval",
            "stop_first_policy": (
                "Hypothetical long probe: a candle open at or beyond a level takes precedence; "
                "otherwise, a same-bar stop/target touch is ambiguous and measured worst-first as stop."
            ),
            "persistence_baseline": "endpoint prediction equals cutoff close",
        },
        "correction": _aggregate_corrections(all_provenances),
        "raw": _variant_summary(all_raw_records, raw=True),
        "corrected": _variant_summary(all_corrected_records),
        "persistence_baseline": _variant_summary(all_baseline_records),
        "paired_deltas": {
            "endpoint_mae_corrected_minus_raw": _variant_summary(all_corrected_records)["endpoint"]["mae"] - _variant_summary(all_raw_records, raw=True)["endpoint"]["mae"],
            "high_mae_corrected_minus_raw": _variant_summary(all_corrected_records)["high"]["mae"] - _variant_summary(all_raw_records, raw=True)["high"]["mae"],
            "low_mae_corrected_minus_raw": _variant_summary(all_corrected_records)["low"]["mae"] - _variant_summary(all_raw_records, raw=True)["low"]["mae"],
        },
        "timing": {
            "model_generation": [
                {
                    "window_id": window["window_id"],
                    "timing": window["timing"],
                }
                for window in report_windows
            ],
            "note": "Timing is copied from the one raw generation artifact; correction is not a second model run.",
        },
        "windows": report_windows,
    }


def assess_variants(
    dataset: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    *,
    corrector: Callable[..., Mapping[str, Any]] | None = None,
    algorithm_version: int = 1,
    level_pct: float = DEFAULT_LEVEL_PCT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assess labeled raw artifacts independently without pooling native outcomes.

    Every artifact is validated and corrected against its own exact raw paths.
    The returned ``variants`` mapping keeps profile/eval-stage results separate;
    only the dataset and declared seed plan are shared across variants.
    """
    if not artifacts:
        raise ResearchError("at least one labeled path artifact is required")
    dataset_info = validate_dataset(dataset, now=now)
    reports: dict[str, dict[str, Any]] = {}
    shared_plan: dict[str, dict[str, int]] | None = None
    shared_sampling: tuple[float, float] | None = None
    for label, artifact in artifacts.items():
        if not isinstance(label, str) or not label.strip():
            raise ResearchError("variant labels must be non-empty strings")
        path_info = validate_paths_artifact(artifact, dataset)
        sampling = (
            float(path_info["generation"]["temperature"]),
            float(path_info["generation"]["top_p"]),
        )
        if shared_sampling is None:
            shared_sampling = sampling
        elif sampling != shared_sampling:
            raise ResearchError(f"variant {label!r} uses different sampling parameters")
        if shared_plan is None:
            shared_plan = path_info["seed_plan"]
        elif path_info["seed_plan"] != shared_plan:
            raise ResearchError(f"variant {label!r} does not use the shared disjoint seed plan")
        reports[label] = assess_dataset(
            dataset,
            artifact,
            corrector=corrector,
            algorithm_version=algorithm_version,
            level_pct=level_pct,
            now=now,
            variant_label=label,
        )
    return {
        "schema": f"{REPORT_SCHEMA}_variants",
        "metadata": {
            "dataset_digest": dataset_info["dataset_digest"],
            "variant_labels": list(reports),
            "pairing": "each variant pairs its own exact raw paths with its own corrected paths",
            "native_outcomes_are_not_pooled": True,
            "retrospective_disclaimer": (
                "Historical retrospective with pre-known data; training/pretraining cutoff is "
                "unknown, so this is not a truly out-of-sample claim or proof of PnL."
            ),
        },
        "seed_plan": shared_plan,
        "variants": reports,
    }


def _bar_from_row(row: Sequence[Any], *, symbol: str, open_time: int) -> dict[str, Any]:
    if len(row) < 6:
        raise ResearchError("CCXT returned an OHLCV row with fewer than six fields")
    actual_open = _epoch_ms(int(row[0]), field="CCXT open_time")
    if actual_open != open_time:
        raise ResearchError(f"CCXT returned open_time={actual_open}, expected {open_time}")
    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "open_time": actual_open,
        "close_time": actual_open + BAR_MS,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "closed": True,
    }


def _fetch_rows(exchange: Any, symbol: str, cutoff_ms: int) -> tuple[list[Any], int]:
    start_ms = cutoff_ms - LOOKBACK_BARS * BAR_MS
    final_open_ms = cutoff_ms + (HORIZON_BARS - 1) * BAR_MS
    rows_by_open: dict[int, tuple[Any, ...]] = {}
    since = start_ms
    pages = 0
    max_pages = math.ceil((LOOKBACK_BARS + HORIZON_BARS) / PAGE_LIMIT) + 3
    while since <= final_open_ms and pages < max_pages:
        limit = min(PAGE_LIMIT, LOOKBACK_BARS + HORIZON_BARS)
        if limit > PAGE_LIMIT:
            raise ResearchError("fetch page limit exceeded the 500-row safety cap")
        rows = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since, limit=limit)
        pages += 1
        if not rows:
            break
        page_opens: list[int] = []
        for row in rows:
            if len(row) < 6:
                continue
            open_time = int(row[0])
            page_opens.append(open_time)
            if start_ms <= open_time <= final_open_ms:
                content = tuple(row[:6])
                previous = rows_by_open.get(open_time)
                if previous is not None and previous != content:
                    raise ResearchError(f"CCXT returned conflicting duplicate {open_time}")
                rows_by_open[open_time] = content
        if not page_opens:
            raise ResearchError("CCXT returned no timestamp progress")
        next_since = max(page_opens) + BAR_MS
        if next_since <= since:
            raise ResearchError("CCXT pagination made no timestamp progress")
        since = next_since
        if max(page_opens) >= final_open_ms:
            break
        time.sleep(0.01)
    rows = [rows_by_open[key] for key in sorted(rows_by_open)]
    expected = LOOKBACK_BARS + HORIZON_BARS
    if len(rows) != expected:
        raise ResearchError(f"CCXT returned {len(rows)} rows, expected {expected}")
    return rows, pages


def _public_exchange(exchange_id: str) -> Any:
    try:
        import ccxt
    except ImportError as exc:
        raise ResearchError("CCXT is required only for explicit --fetch mode") from exc
    exchange_type = getattr(ccxt, exchange_id, None)
    if exchange_type is None:
        raise ResearchError(f"unknown CCXT exchange {exchange_id!r}")
    exchange = exchange_type({"enableRateLimit": True, "options": {"defaultType": "future"}})
    for credential_name in ("apiKey", "secret", "password", "uid"):
        if getattr(exchange, credential_name, None):
            raise ResearchError("public fetch refused because a credential field is populated")
    return exchange


def fetch_dataset(
    *,
    exchange_id: str = "binanceusdm",
    symbols: Sequence[str] = SYMBOLS,
    cutoffs_utc: Sequence[str] = CUTOFFS_UTC,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the approved public windows, preserving unavailable windows explicitly."""
    if tuple(symbols) != SYMBOLS or tuple(cutoffs_utc) != CUTOFFS_UTC:
        raise ResearchError("the frozen assessment plan only permits the approved symbols/cutoffs")
    if exchange_id != "binanceusdm":
        raise ResearchError("the frozen assessment requires Binance USD-M")
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ResearchError("validation time must be timezone-aware")
    now_utc = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    exchange = _public_exchange(exchange_id)
    try:
        exchange.load_markets()
        source = {
            "exchange_id": exchange_id,
            "venue": "Binance USD-M public OHLCV",
            "endpoint": "ccxt.fetch_ohlcv",
            "market_type": "swap",
            "authenticated": False,
            "page_limit": PAGE_LIMIT,
            "timeframe": TIMEFRAME,
        }
        windows: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []
        for symbol in symbols:
            for cutoff_text in cutoffs_utc:
                cutoff_ms = int(_parse_utc(cutoff_text).timestamp() * 1000)
                identifier = window_id(symbol, cutoff_ms)
                try:
                    rows, pages = _fetch_rows(exchange, symbol, cutoff_ms)
                    bars = [
                        _bar_from_row(
                            row, symbol=symbol,
                            open_time=cutoff_ms - LOOKBACK_BARS * BAR_MS + index * BAR_MS,
                        )
                        for index, row in enumerate(rows)
                    ]
                    for index, bar in enumerate(bars):
                        _bar_problem(
                            bar, symbol=symbol,
                            expected_open=cutoff_ms - LOOKBACK_BARS * BAR_MS + index * BAR_MS,
                            now_ms=int(now_utc.timestamp() * 1000),
                            label=f"{identifier}.bar[{index}]",
                        )
                    window = {
                        "window_id": identifier,
                        "symbol": symbol,
                        "timeframe": TIMEFRAME,
                        "cutoff_ms": cutoff_ms,
                        "cutoff_utc": _utc_iso(cutoff_ms),
                        "status": "ok",
                        "history": bars[:LOOKBACK_BARS],
                        "future": bars[LOOKBACK_BARS:],
                        "source": {**source, "pages": pages},
                    }
                    window["range"] = {
                        "history_first_open_ms": window["history"][0]["open_time"],
                        "history_last_close_ms": window["history"][-1]["close_time"],
                        "future_first_open_ms": window["future"][0]["open_time"],
                        "future_last_close_ms": window["future"][-1]["close_time"],
                    }
                    window["digest"] = window_digest(window)
                    windows.append(window)
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {exc}"
                    unavailable.append({"window_id": identifier, "reason": reason})
                    windows.append({
                        "window_id": identifier,
                        "symbol": symbol,
                        "timeframe": TIMEFRAME,
                        "cutoff_ms": cutoff_ms,
                        "cutoff_utc": _utc_iso(cutoff_ms),
                        "status": "unavailable",
                        "reason": reason,
                    })
        payload = {
            "schema": SCHEMA,
            "metadata": {
                "kind": "retrospective_public_ohlcv",
                "source": source,
                "symbols": list(symbols),
                "cutoffs_utc": list(cutoffs_utc),
                "timeframe": TIMEFRAME,
                "lookback_bars": LOOKBACK_BARS,
                "horizon_bars": HORIZON_BARS,
                "fetched_at_utc": datetime.now(UTC).isoformat(),
                "latest_validation_time_utc": now_utc.isoformat(),
                "disclaimer": (
                    "Pre-known historical data for retrospective research; not truly out-of-sample "
                    "and not evidence of PnL or profitability."
                ),
            },
            "windows": windows,
            "manifest": {
                "dataset_digest": None,
                "unavailable_windows": unavailable,
                "status": "unavailable" if unavailable else "ok",
            },
        }
        payload["manifest"]["dataset_digest"] = dataset_digest(payload)
        return payload
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ResearchError(f"input JSON does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchError(f"could not read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResearchError(f"input JSON {path} must contain an object")
    return payload


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_external_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    repo = _repo_root().resolve()
    try:
        resolved.relative_to(repo)
    except ValueError:
        return resolved
    raise ResearchError(f"artifact output must stay outside the repository: {resolved}")


def write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    """Freeze an artifact without silently replacing a previous run."""
    destination = _ensure_external_output(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except FileExistsError as exc:
        raise ResearchError(f"refusing to overwrite frozen artifact: {destination}") from exc


def _default_fetch_output() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return default_output_dir() / f"dataset-{stamp}.json"


def _default_assessment_output() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return default_output_dir() / f"assessment-{stamp}.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="explicitly fetch public unauthenticated OHLCV")
    parser.add_argument("--dataset", type=Path, help="frozen local dataset JSON")
    parser.add_argument("--paths", type=Path, help="frozen local raw-path artifact JSON")
    parser.add_argument("--output", type=Path, help="external output JSON path")
    parser.add_argument("--validate", action="store_true", help="validate a local dataset only")
    parser.add_argument("--assess", action="store_true", help="assess a local dataset/path pair")
    parser.add_argument("--algorithm-version", type=int, default=1)
    parser.add_argument("--hypothetical-level-pct", type=float, default=DEFAULT_LEVEL_PCT)
    args = parser.parse_args(argv)

    try:
        if args.fetch:
            if args.dataset or args.paths or args.validate or args.assess:
                raise ResearchError("--fetch cannot be combined with offline assessment flags")
            payload = fetch_dataset()
            output = args.output or _default_fetch_output()
            write_json_once(output, payload)
            print(json.dumps({
                "mode": "fetch",
                "output": str(output.resolve()),
                "status": payload["manifest"]["status"],
                "dataset_digest": payload["manifest"]["dataset_digest"],
                "unavailable_windows": payload["manifest"]["unavailable_windows"],
            }, indent=2))
            return 0 if payload["manifest"]["status"] == "ok" else 2
        if not args.dataset:
            raise ResearchError("offline mode requires --dataset, or explicit --fetch")
        dataset = load_json(args.dataset)
        if args.validate or not args.assess:
            summary = validate_dataset(dataset)
            print(json.dumps({"mode": "validate", **summary}, indent=2))
            if not args.assess:
                return 0
        if args.assess:
            if not args.paths:
                raise ResearchError("--assess requires --paths")
            paths = load_json(args.paths)
            report = assess_dataset(
                dataset,
                paths,
                algorithm_version=args.algorithm_version,
                level_pct=args.hypothetical_level_pct,
            )
            output = args.output or _default_assessment_output()
            write_json_once(output, report)
            print(json.dumps({
                "mode": "assess",
                "output": str(output.resolve()),
                "dataset_digest": report["metadata"]["dataset_digest"],
                "path_artifact_digest": report["metadata"]["path_artifact_digest"],
                "correction": report["correction"],
                "raw": report["raw"],
                "corrected": report["corrected"],
                "persistence_baseline": report["persistence_baseline"],
            }, indent=2))
            return 0
    except ResearchError as exc:
        print(f"research unavailable: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
