"""Regression tests for OHLC correction and recorded provenance."""

import copy

import pytest

from tradingagents.forecast.ohlc import (
    aggregate_ohlc_provenance,
    correct_ohlc_path,
    normalize_ohlc_path,
)

pytestmark = pytest.mark.unit


def test_ohlc_correction_preserves_raw_values_and_exact_large_integer_delta():
    base = 2**53
    raw = {
        "opens": [base + 1], "highs": [base], "lows": [base], "closes": [base],
    }
    original = copy.deepcopy(raw)
    corrected, provenance = correct_ohlc_path(raw)
    assert raw == original
    assert corrected["opens"] == raw["opens"]
    assert corrected["closes"] == raw["closes"]
    assert corrected["highs"] == [base + 1]
    assert provenance["corrected_bar_count"] == 1
    assert provenance["absolute_delta_by_field"]["highs"] == {"total": 1.0, "max": 1.0}


def test_normalized_path_rejects_forged_provenance():
    raw = {
        "opens": [100.0], "highs": [98.0], "lows": [99.0],
        "closes": [100.0], "seed": 42,
    }
    normalized = normalize_ohlc_path(raw, index=0, horizon_bars=1)
    assert normalized is not None
    assert normalized["raw_ohlc"]["highs"] == [98.0]
    assert normalized["highs"] == [100.0]
    assert normalized["seed"] == 42
    forged = copy.deepcopy(normalized)
    forged["ohlc_provenance"]["corrected_bar_count"] = 0
    assert normalize_ohlc_path(forged, index=0, horizon_bars=1) is None
    with pytest.raises(ValueError, match="does not match"):
        aggregate_ohlc_provenance([forged])


def test_correction_rejects_missing_nonfinite_and_overflowing_values():
    for value in (None, True, 0, float("nan"), float("inf"), 10**400):
        raw = {"opens": [value], "highs": [101.0], "lows": [99.0], "closes": [100.0]}
        with pytest.raises(ValueError):
            correct_ohlc_path(raw)
        assert normalize_ohlc_path(raw, index=0, horizon_bars=1) is None
