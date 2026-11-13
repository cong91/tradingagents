"""Synthetic offline tests for the frozen Kronos OHLC assessment."""

from __future__ import annotations

import copy
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tradingagents.dataflows.market_data import Candle
from tradingagents.forecast import artifact as artifact_registry
from tradingagents.simulation.fills import MarketRules, match_exit

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_kronos_ohlc.py"
_SPEC = importlib.util.spec_from_file_location("research_kronos_ohlc", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
research: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(research)

_NOW = datetime(2026, 11, 12, tzinfo=UTC)
_REAL_PROFILES = artifact_registry.PROFILES


@pytest.fixture(autouse=True)
def _fake_profile_pins(monkeypatch):
    profiles = {
        name: artifact_registry.ArtifactProfile(
            name=name,
            model_repo=f"fixture/{name}-model",
            tokenizer_repo=f"fixture/{name}-tokenizer",
            source_revision=f"{name}-source-revision",
            model_revision=f"{name}-model-revision",
            tokenizer_revision=f"{name}-tokenizer-revision",
            max_context=2048,
            source_hashes={"source.py": "a" * 64},
            model_hashes={"weights.bin": ("b" if name == "mini_2k" else "c") * 64},
        )
        for name in research.PROFILES
    }
    monkeypatch.setattr(artifact_registry, "PROFILES", profiles)


def _bar(symbol: str, open_time: int, index: int, *, close: float | None = None) -> dict[str, Any]:
    opened = 100.0 + index * 0.01
    closed = opened + 0.25 if close is None else close
    return {
        "symbol": symbol,
        "timeframe": "4h",
        "open_time": open_time,
        "close_time": open_time + research.BAR_MS,
        "open": opened,
        "high": max(opened, closed) + 0.5,
        "low": min(opened, closed) - 0.5,
        "close": closed,
        "volume": 10.0 + index,
        "closed": True,
    }


def _dataset() -> dict[str, Any]:
    windows = []
    for symbol_index, symbol in enumerate(research.SYMBOLS):
        for cutoff_text in research.CUTOFFS_UTC:
            cutoff_ms = int(research._parse_utc(cutoff_text).timestamp() * 1000)
            first_open = cutoff_ms - research.LOOKBACK_BARS * research.BAR_MS
            history = [
                _bar(symbol, first_open + index * research.BAR_MS, index + symbol_index)
                for index in range(research.LOOKBACK_BARS)
            ]
            future = [
                _bar(
                    symbol,
                    cutoff_ms + index * research.BAR_MS,
                    research.LOOKBACK_BARS + index + symbol_index,
                    close=100.0 + (index + 1) * 0.5,
                )
                for index in range(research.HORIZON_BARS)
            ]
            window = {
                "window_id": research.window_id(symbol, cutoff_ms),
                "symbol": symbol,
                "timeframe": research.TIMEFRAME,
                "cutoff_ms": cutoff_ms,
                "cutoff_utc": research._utc_iso(cutoff_ms),
                "status": "ok",
                "history": history,
                "future": future,
            }
            window["range"] = {
                "history_first_open_ms": history[0]["open_time"],
                "history_last_close_ms": history[-1]["close_time"],
                "future_first_open_ms": future[0]["open_time"],
                "future_last_close_ms": future[-1]["close_time"],
            }
            window["digest"] = research.window_digest(window)
            windows.append(window)
    dataset = {
        "schema": research.SCHEMA,
        "metadata": {
            "kind": "retrospective_public_ohlcv",
            "source": {
                "exchange_id": "synthetic",
                "venue": "synthetic test source",
                "endpoint": "fixture",
                "market_type": "swap",
                "authenticated": False,
                "page_limit": research.PAGE_LIMIT,
                "timeframe": research.TIMEFRAME,
            },
            "symbols": list(research.SYMBOLS),
            "cutoffs_utc": list(research.CUTOFFS_UTC),
            "timeframe": research.TIMEFRAME,
            "lookback_bars": research.LOOKBACK_BARS,
            "horizon_bars": research.HORIZON_BARS,
        },
        "windows": windows,
    }
    dataset["manifest"] = {
        "dataset_digest": research.dataset_digest(dataset),
        "status": "ok", "unavailable_windows": [],
    }
    return dataset


def _path(seed: int, *, inconsistent: bool = False) -> dict[str, Any]:
    opens = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5]
    closes = [100.25, 100.75, 101.25, 101.75, 102.25, 102.75]
    highs = [opened + 1.0 for opened in opens]
    lows = [opened - 1.0 for opened in opens]
    if inconsistent:
        highs[0] = 99.0
    return {
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "seed": seed,
    }


def _paths_artifact(
    dataset: dict[str, Any],
    *,
    inconsistent: bool = False,
    profile: str = "mini_2k",
    eval_stage: str = "before_model_eval",
) -> dict[str, Any]:
    plan = research.seed_plan(dataset["windows"])
    windows = []
    for dataset_window in sorted(dataset["windows"], key=research._window_sort_key):
        identifier = dataset_window["window_id"]
        base = plan[identifier]["seed_base"]
        windows.append({
            "window_id": identifier,
            "seed_base": base,
            "timing": {"duration_ms": 12.5},
            "paths": [
                _path(base + index, inconsistent=inconsistent and index == 0)
                for index in range(research.PATH_COUNT)
            ],
        })
    pin = artifact_registry.profile_for(profile)
    artifact = {
        "schema": research.PATH_SCHEMA,
        "metadata": {
            "dataset_digest": research.dataset_digest(dataset),
            "profile": profile,
            "timeframe": research.TIMEFRAME,
            "horizon_bars": research.HORIZON_BARS,
            "path_count": research.PATH_COUNT,
            "raw_generated_once": True,
            "generation": {
                "input_bars": research.LOOKBACK_BARS,
                "effective_context_bars": min(research.LOOKBACK_BARS, pin.max_context),
                "max_context": pin.max_context,
                "temperature": 0.5,
                "top_p": 0.9,
            },
            "runner": {
                "source_revision": pin.source_revision,
                "model_revision": pin.model_revision,
                "tokenizer_revision": pin.tokenizer_revision,
                "artifact_digest": artifact_registry.artifact_digest(profile),
                "model_eval_stage": eval_stage,
            },
        },
        "windows": windows,
    }
    artifact["manifest"] = {"path_artifact_digest": research.path_artifact_digest(artifact)}
    return artifact


def _corrector(raw_path):
    from tradingagents.forecast.ohlc import correct_ohlc_path

    return correct_ohlc_path(raw_path)


def test_validate_dataset_requires_exact_cutoff_and_horizon_without_lookahead():
    dataset = _dataset()
    summary = research.validate_dataset(dataset, now=_NOW)
    assert summary["windows"] == 9
    assert summary["symbols"] == sorted(research.SYMBOLS)
    assert summary["cutoffs_utc"] == sorted(research.CUTOFFS_UTC)

    leaked = copy.deepcopy(dataset)
    leaked_window = leaked["windows"][0]
    leaked_window["history"][-1]["close_time"] += research.BAR_MS
    with pytest.raises(research.ResearchError, match="cutoff|duration|contiguous"):
        research.validate_dataset(leaked, now=_NOW)


@pytest.mark.parametrize("removed", [0, 4, 8])
def test_validate_dataset_rejects_missing_cartesian_pair_with_matching_manifest(removed):
    dataset = _dataset()
    dataset["windows"].pop(removed)
    dataset["manifest"]["dataset_digest"] = research.dataset_digest(dataset)

    with pytest.raises(research.ResearchError, match="symbol/cutoff"):
        research.validate_dataset(dataset, now=_NOW)


def test_validate_dataset_rejects_duplicate_pair_with_matching_manifest():
    dataset = _dataset()
    dataset["windows"].append(copy.deepcopy(dataset["windows"][0]))
    dataset["manifest"]["dataset_digest"] = research.dataset_digest(dataset)

    with pytest.raises(research.ResearchError, match="duplicate"):
        research.validate_dataset(dataset, now=_NOW)


def test_validate_dataset_rejects_gap_nonpositive_and_unrealized_future():
    for mutation, needle in (
        (lambda window: window["history"].__setitem__(100, {**window["history"][100], "open_time": window["history"][100]["open_time"] + 1}), "contiguous"),
        (lambda window: window["history"][10].update(close=0.0), "finite and positive"),
        (lambda window: window["future"][0].update(closed=False), "explicitly marked closed"),
    ):
        dataset = _dataset()
        mutation(dataset["windows"][0])
        with pytest.raises(research.ResearchError, match=needle):
            research.validate_dataset(dataset, now=_NOW)


def test_seed_ranges_are_deterministic_disjoint_and_order_independent():
    dataset = _dataset()
    first = research.seed_plan(dataset["windows"])
    second = research.seed_plan(list(reversed(dataset["windows"])))
    assert first == second
    ranges = sorted((value["seed_first"], value["seed_last"]) for value in first.values())
    assert all(left[1] < right[0] for left, right in zip(ranges, ranges[1:], strict=False))


def test_validate_paths_rejects_missing_gap_seed_and_nonpositive_path():
    dataset = _dataset()
    artifact = _paths_artifact(dataset)
    assert research.validate_paths_artifact(artifact, dataset)["windows"] == 9

    bad_seed = copy.deepcopy(artifact)
    bad_seed["windows"][0]["paths"][1]["seed"] = bad_seed["windows"][0]["paths"][0]["seed"]
    with pytest.raises(research.ResearchError, match="seed"):
        research.validate_paths_artifact(bad_seed, dataset)

    bad_value = copy.deepcopy(artifact)
    bad_value["windows"][0]["paths"][0]["closes"][0] = 0.0
    with pytest.raises(research.ResearchError, match="non-finite or non-positive"):
        research.validate_paths_artifact(bad_value, dataset)


@pytest.mark.parametrize("profile", ["mini_2k", "small_base"])
def test_validate_paths_accepts_registry_profile_pins(monkeypatch, profile):
    monkeypatch.setattr(artifact_registry, "PROFILES", _REAL_PROFILES)
    dataset = _dataset()
    artifact = _paths_artifact(dataset, profile=profile)

    assert research.validate_paths_artifact(artifact, dataset, expected_profile=profile)["profile"] == profile


@pytest.mark.parametrize("profile", ["mini_2k", "small_base"])
@pytest.mark.parametrize(
    "field", ["source_revision", "model_revision", "tokenizer_revision", "artifact_digest"],
)
def test_validate_paths_rejects_mismatched_profile_pins_with_matching_manifest(profile, field):
    dataset = _dataset()
    artifact = _paths_artifact(dataset, profile=profile)
    assert research.validate_paths_artifact(artifact, dataset, expected_profile=profile)["profile"] == profile

    other_profile = "small_base" if profile == "mini_2k" else "mini_2k"
    other_pin = artifact_registry.profile_for(other_profile)
    artifact["metadata"]["runner"][field] = (
        artifact_registry.artifact_digest(other_profile)
        if field == "artifact_digest"
        else getattr(other_pin, field)
    )
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)
    with pytest.raises(research.ResearchError, match=field):
        research.validate_paths_artifact(artifact, dataset)


@pytest.mark.parametrize("profile", ["mini_2k", "small_base"])
@pytest.mark.parametrize("changed_hashes", ["source_hashes", "model_hashes"])
def test_validate_paths_rejects_stale_digest_when_registry_content_changes(
    monkeypatch, profile, changed_hashes,
):
    dataset = _dataset()
    artifact = _paths_artifact(dataset, profile=profile)
    old_pin = artifact_registry.profile_for(profile)
    new_pin = replace(old_pin, **{changed_hashes: {"new-content": "d" * 64}})
    monkeypatch.setitem(artifact_registry.PROFILES, profile, new_pin)

    with pytest.raises(research.ResearchError, match="artifact_digest"):
        research.validate_paths_artifact(artifact, dataset)


@pytest.mark.parametrize(
    "field,value", [
        ("input_bars", 512), ("effective_context_bars", 512), ("max_context", 512),
        ("temperature", float("nan")), ("top_p", 0),
    ],
)
def test_validate_paths_rejects_wrong_generation_metadata(field, value):
    dataset = _dataset()
    artifact = _paths_artifact(dataset)
    artifact["metadata"]["generation"][field] = value
    with pytest.raises(research.ResearchError, match="generation"):
        research.validate_paths_artifact(artifact, dataset)


def test_report_distinguishes_input_and_effective_context(monkeypatch):
    monkeypatch.setattr(artifact_registry, "PROFILES", _REAL_PROFILES)
    dataset = _dataset()
    artifact = _paths_artifact(dataset, profile="small_base")
    report = research.assess_dataset(dataset, artifact, now=_NOW)
    assert report["metadata"]["dataset_history_bars"] == 2048
    assert report["metadata"]["generation"]["input_bars"] == 2048
    assert report["metadata"]["generation"]["effective_context_bars"] == 512


def test_validate_paths_rejects_profile_relabeling_with_matching_manifest():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, profile="mini_2k")
    artifact["metadata"]["profile"] = "small_base"
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)

    with pytest.raises(research.ResearchError, match="revision|digest"):
        research.validate_paths_artifact(artifact, dataset)


@pytest.mark.parametrize(
    "opens,highs,lows,label,step,ambiguous,policy",
    [
        ([111.0], [115.0], [85.0], "target", 1, False, "open"),
        ([110.0], [115.0], [85.0], "target", 1, False, "open"),
        ([89.0], [115.0], [85.0], "stop", 1, False, "open"),
        ([90.0], [115.0], [85.0], "stop", 1, False, "open"),
        ([100.0], [115.0], [85.0], "stop", 1, True, "worst_first_stop"),
        ([100.0], [110.0], [95.0], "target", 1, False, "single_level"),
        ([100.0], [105.0], [90.0], "stop", 1, False, "single_level"),
        ([100.0], [105.0], [95.0], "none", None, False, "none"),
        ([100.0, 111.0], [105.0, 115.0], [95.0, 85.0], "target", 2, False, "open"),
        ([100.0, 89.0], [105.0, 115.0], [95.0, 85.0], "stop", 2, False, "open"),
        ([100.0, 100.0], [110.0, 115.0], [95.0, 85.0], "target", 1, False, "single_level"),
    ],
)
def test_first_touch_matches_long_shadow_exit(opens, highs, lows, label, step, ambiguous, policy):
    touch = research._first_touch({"opens": opens, "highs": highs, "lows": lows}, stop=90.0, target=110.0)
    assert (touch["label"], touch["step"], touch["ambiguous"], touch["policy"]) == (
        label, step, ambiguous, policy,
    )
    assert "Hypothetical" in touch["semantics"]

    matches = []
    for index, (opened, high, low) in enumerate(zip(opens, highs, lows, strict=True), start=1):
        candle = Candle(
            symbol="BTC/USDT:USDT", timeframe="4h", open_time=(index - 1) * research.BAR_MS,
            close_time=index * research.BAR_MS, open=opened, high=high, low=low,
            close=opened, volume=1.0, is_closed=True,
        )
        match = match_exit(
            candle, "long", stop=90.0, target=110.0, quantity=1.0, rules=MarketRules(),
        )
        if match.fill is not None:
            matches.append((match.fill.kind, index, match.ambiguous))
            break
    assert matches == ([] if step is None else [(label, step, ambiguous)])


@pytest.mark.parametrize("opened,label", [(122.5, "target"), (119.0, "stop")])
def test_assessment_uses_gap_open_for_model_and_realized_touches(opened, label):
    dataset = _dataset()
    window = dataset["windows"][0]
    window["future"][0].update(open=opened, high=123.0, low=119.0, close=120.5)
    window["digest"] = research.window_digest(window)
    dataset["manifest"]["dataset_digest"] = research.dataset_digest(dataset)
    artifact = _paths_artifact(dataset)
    path = artifact["windows"][0]["paths"][0]
    for field, value in (("opens", opened), ("highs", 123.0), ("lows", 119.0), ("closes", 120.5)):
        path[field][0] = value
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)

    report = research.assess_dataset(dataset, artifact, now=_NOW)
    first_window = report["windows"][0]
    actual = first_window["realized"]["touch"]
    pair = first_window["paths"][0]
    for touch in (actual, pair["raw_metrics"]["touch"], pair["corrected_metrics"]["touch"]):
        assert (touch["label"], touch["step"], touch["ambiguous"], touch["policy"]) == (
            label, 1, False, "open",
        )
    assert pair["raw_metrics"]["touch_timing_error_steps"] == 0
    assert pair["corrected_metrics"]["touch_timing_error_steps"] == 0
    assert "open" in report["protocol"]["stop_first_policy"].lower()
    assert "hypothetical" in report["protocol"]["stop_first_policy"].lower()


def test_assessment_keeps_exact_raw_draws_and_separates_corrected_and_baseline():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, inconsistent=True)
    report = research.assess_dataset(dataset, artifact, now=_NOW, level_pct=0.01)
    assert report["schema"] == research.REPORT_SCHEMA
    assert report["raw"]["paths"] == 9 * research.PATH_COUNT
    assert report["corrected"]["paths"] == 9 * research.PATH_COUNT
    assert report["persistence_baseline"]["paths"] == 9 * research.PATH_COUNT
    assert report["correction"]["status"] == "corrected"
    assert report["correction"]["changed_field_counts"]["highs"] == 9
    first_pair = report["windows"][0]["paths"][0]
    original = artifact["windows"][0]["paths"][0]
    assert first_pair["seed"] == original["seed"]
    assert first_pair["raw_ohlc"]["highs"] == original["highs"]
    assert first_pair["corrected_ohlc"]["highs"][0] > first_pair["raw_ohlc"]["highs"][0]
    assert first_pair["raw_metrics"] != first_pair["corrected_metrics"]
    assert first_pair["raw_metrics"]["touch"] is None
    assert report["raw"]["touch"]["status"] == "unavailable"
    assert report["raw"]["touch"]["label_counts"] is None
    assert report["windows"][0]["raw"]["touch"]["status"] == "unavailable"
    assert report["windows"][0]["raw"]["touch"]["label_counts"] is None
    assert report["corrected"]["touch"]["status"] == "ok"
    assert "Hypothetical" in report["corrected"]["touch"]["semantics"]
    assert report["metadata"]["retrospective_disclaimer"].startswith("Historical retrospective")


def test_one_invalid_raw_path_makes_only_its_window_and_aggregate_unavailable():
    dataset = _dataset()
    artifact = _paths_artifact(dataset)
    artifact["windows"][0]["paths"][0]["highs"][0] = 99.0
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)

    report = research.assess_dataset(dataset, artifact, now=_NOW)
    assert report["raw"]["touch"]["status"] == "unavailable"
    assert report["raw"]["touch"]["label_counts"] is None
    assert report["windows"][0]["raw"]["touch"]["status"] == "unavailable"
    assert report["windows"][1]["raw"]["touch"]["status"] == "ok"
    assert report["corrected"]["touch"]["status"] == "ok"


def test_assessment_rejects_corrector_that_mutates_raw_snapshot_or_bad_provenance():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, inconsistent=True)

    def mutating_corrector(raw_path):
        raw_path["closes"][0] = 999.0
        return _corrector(raw_path)

    with pytest.raises(research.ResearchError, match="changed open/close"):
        research.assess_dataset(dataset, artifact, corrector=mutating_corrector, now=_NOW)

    def malformed_corrector(raw_path):
        corrected, provenance = _corrector(raw_path)
        provenance["corrected_bar_count"] = 0
        return corrected, provenance

    with pytest.raises(research.ResearchError, match="corrected_bar_count"):
        research.assess_dataset(dataset, artifact, corrector=malformed_corrector, now=_NOW)



def test_assess_variants_keeps_profiles_and_eval_stages_separate():
    dataset = _dataset()
    variants = research.assess_variants(
        dataset,
        {
            "mini_2k_raw": _paths_artifact(dataset, profile="mini_2k", eval_stage="before_model_eval"),
            "mini_2k_eval": _paths_artifact(dataset, profile="mini_2k", eval_stage="after_model_eval"),
            "small_base_raw": _paths_artifact(dataset, profile="small_base", eval_stage="before_model_eval"),
        },
        corrector=_corrector,
        now=_NOW,
    )
    assert variants["metadata"]["native_outcomes_are_not_pooled"] is True
    assert set(variants["variants"]) == {"mini_2k_raw", "mini_2k_eval", "small_base_raw"}
    assert variants["variants"]["mini_2k_raw"]["metadata"]["model_eval_stage"] == "before_model_eval"
    assert variants["variants"]["mini_2k_eval"]["metadata"]["model_eval_stage"] == "after_model_eval"
    assert variants["variants"]["small_base_raw"]["metadata"]["profile"] == "small_base"
    assert all(
        report["raw"]["paths"] == 9 * research.PATH_COUNT
        for report in variants["variants"].values()
    )


def test_variants_reject_mismatched_sampling_parameters():
    dataset = _dataset()
    first = _paths_artifact(dataset)
    second = _paths_artifact(dataset)
    second["metadata"]["generation"]["temperature"] = 1.0
    second["manifest"]["path_artifact_digest"] = research.path_artifact_digest(second)
    with pytest.raises(research.ResearchError, match="different sampling parameters"):
        research.assess_variants(dataset, {"first": first, "second": second}, now=_NOW)


def test_dataset_and_paths_require_matching_frozen_manifests():
    dataset = _dataset()
    artifact = _paths_artifact(dataset)
    for invalid in (None, {}, {"dataset_digest": None}):
        candidate = copy.deepcopy(dataset)
        candidate["manifest"] = invalid
        with pytest.raises(research.ResearchError, match="manifest"):
            research.validate_dataset(candidate, now=_NOW)
    for invalid in (None, {}, {"path_artifact_digest": None}):
        candidate = copy.deepcopy(artifact)
        candidate["manifest"] = invalid
        with pytest.raises(research.ResearchError, match="digest"):
            research.validate_paths_artifact(candidate, dataset)
    tampered = copy.deepcopy(artifact)
    tampered["windows"][0]["paths"][0]["closes"][0] += 0.01
    with pytest.raises(research.ResearchError, match="digest"):
        research.validate_paths_artifact(tampered, dataset)
    mismatched = copy.deepcopy(artifact)
    mismatched["metadata"]["dataset_digest"] = "b" * 64
    mismatched["manifest"]["path_artifact_digest"] = research.path_artifact_digest(mismatched)
    with pytest.raises(research.ResearchError, match="dataset_digest"):
        research.validate_paths_artifact(mismatched, dataset)


def test_assessment_rejects_unapproved_wick_correction():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, inconsistent=True)

    def wider_corrector(raw_path):
        corrected, provenance = _corrector(raw_path)
        corrected["highs"][0] += 1.0
        return corrected, provenance

    with pytest.raises(research.ResearchError, match="exact wick bounds"):
        research.assess_dataset(dataset, artifact, corrector=wider_corrector, now=_NOW)
    with pytest.raises(research.ResearchError, match="version 1"):
        research.assess_dataset(dataset, artifact, now=_NOW, algorithm_version=2)


def test_assessment_accepts_eval_stage_in_artifact_metadata():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, eval_stage="after_model_eval")
    del artifact["metadata"]["runner"]["model_eval_stage"]
    artifact["metadata"]["model_eval_stage"] = "after_model_eval"
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)

    report = research.assess_dataset(dataset, artifact, now=_NOW)
    assert report["protocol"]["raw_model_eval_stage"] == "after_model_eval"
    assert report["protocol"]["paired_raw_eval_baseline_required"] is True


def test_assessment_rejects_conflicting_eval_stage_labels():
    dataset = _dataset()
    artifact = _paths_artifact(dataset, eval_stage="before_model_eval")
    artifact["metadata"]["model_eval_stage"] = "after_model_eval"
    artifact["manifest"]["path_artifact_digest"] = research.path_artifact_digest(artifact)
    with pytest.raises(research.ResearchError, match="disagrees"):
        research.assess_dataset(dataset, artifact, now=_NOW)


def test_validation_rejects_naive_clock():
    with pytest.raises(research.ResearchError, match="timezone-aware"):
        research.validate_dataset(_dataset(), now=datetime(2026, 11, 12))


def test_fetch_marks_a_window_unavailable_when_future_bar_is_not_closed(monkeypatch):
    class Exchange:
        def load_markets(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(research, "_public_exchange", lambda _id: Exchange())

    def rows(_exchange, _symbol, cutoff_ms):
        start = cutoff_ms - research.LOOKBACK_BARS * research.BAR_MS
        return [
            [start + index * research.BAR_MS, 100.0, 101.0, 99.0, 100.0, 1.0]
            for index in range(research.LOOKBACK_BARS + research.HORIZON_BARS)
        ], 5

    monkeypatch.setattr(research, "_fetch_rows", rows)
    first_cutoff = research._parse_utc(research.CUTOFFS_UTC[0])
    now = first_cutoff + timedelta(hours=20)
    dataset = research.fetch_dataset(now=now)
    first = dataset["windows"][0]
    assert first["status"] == "unavailable"
    assert "not realized" in first["reason"]
    assert dataset["manifest"]["status"] == "unavailable"


def test_output_path_cannot_be_inside_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(research, "_repo_root", lambda: repo)
    inside = repo / "_research_should_not_write.json"
    with pytest.raises(research.ResearchError, match="outside the repository"):
        research.write_json_once(inside, {"schema": "test"})
    external = tmp_path / "frozen.json"
    research.write_json_once(external, {"schema": "test"})
    with pytest.raises(research.ResearchError, match="overwrite"):
        research.write_json_once(external, {"schema": "test"})
