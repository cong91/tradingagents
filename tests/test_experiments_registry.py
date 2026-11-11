"""Experiment registry persistence (migration 0009): idempotent, digest-tied.

The registry stores runs, per-repeat results and paired comparisons as data;
writes are ON CONFLICT DO NOTHING, a re-registration under a different digest
is refused, and an empty registry reads ``unavailable`` — never zero runs.
"""

import pytest

from server import experiment_store

pytestmark = pytest.mark.unit


def _manifest(run_id: str = "exp_registry_test") -> dict:
    from tradingagents.experiments.runner import build_manifest

    manifest = build_manifest(
        run_id=run_id, hypothesis="h", window="2026-01-01:2026-02-01",
        holdout_start="2026-02-01", horizon_days=1, embargo_days=0,
        seed=7, repeat=3, variants=["no_trade", "rule"],
        data_source="fixture", config={"a": 1},
    )
    from tradingagents.experiments import artifacts
    manifest["manifest_digest"] = artifacts.manifest_digest(dict(manifest))
    return manifest


def _result_row(run_id: str, variant: str, repeat: int, digest: str,
                metric_value: float | None) -> dict:
    return {
        "variant": variant, "repeat": repeat, "seed": 7 + repeat,
        "manifest_digest": digest, "metric_name": "net_after_costs.final_equity",
        "metric_value": metric_value, "llm_tokens": 0, "llm_calls": 0,
        "llm_latency_ms": 0.0, "report": {"kind": "simulation", "fills": []},
    }


def test_empty_registry_reads_unavailable_not_zero(postgres_schema):
    payload = experiment_store.summary()
    assert payload["status"] == "unavailable"
    assert "empty" in payload["reason"]


def test_register_run_and_results_are_idempotent(postgres_schema):
    manifest = _manifest()
    digest = manifest["manifest_digest"]
    run_id = experiment_store.register_run(manifest, artifact_dir="/tmp/experiments/exp_registry_test")
    assert run_id == manifest["run_id"]
    rows = [
        _result_row(run_id, "no_trade", i, digest, 10000.0) for i in range(3)
    ] + [
        _result_row(run_id, "rule", i, digest, 10050.0) for i in range(3)
    ]
    first = experiment_store.record_results(run_id, rows)
    assert first == {"new": 6, "duplicates": 0}
    second = experiment_store.record_results(run_id, rows)
    assert second == {"new": 0, "duplicates": 6}

    detail = experiment_store.run_results(run_id)
    assert detail["status"] == "ok"
    assert len(detail["results"]) == 6
    assert detail["manifest_digest"] == digest
    assert detail["criteria"]["holdout_start"] == "2026-02-01"


def test_none_metric_value_stays_none_not_zero(postgres_schema):
    manifest = _manifest("exp_none_metric")
    digest = manifest["manifest_digest"]
    run_id = experiment_store.register_run(manifest, artifact_dir="/tmp/exp_none")
    experiment_store.record_results(
        run_id, [_result_row(run_id, "rule", 0, digest, None)],
    )
    detail = experiment_store.run_results(run_id)
    assert detail["results"][0]["metric_value"] is None


def test_reregistration_under_a_different_digest_is_refused(postgres_schema):
    manifest = _manifest("exp_locked")
    experiment_store.register_run(manifest, artifact_dir="/tmp/exp_locked")
    tampered = dict(manifest)
    tampered["hypothesis"] = "revised after results"
    from tradingagents.experiments import artifacts
    tampered["manifest_digest"] = artifacts.manifest_digest({
        k: v for k, v in tampered.items() if k != "manifest_digest"
    })
    with pytest.raises(ValueError, match="different manifest digest"):
        experiment_store.register_run(tampered, artifact_dir="/tmp/exp_locked")


def test_comparison_verdicts_pass_through_verbatim(postgres_schema):
    import random

    from tradingagents.experiments.stats import paired_difference

    manifest = _manifest("exp_comparisons")
    run_id = experiment_store.register_run(manifest, artifact_dir="/tmp/exp_cmp")
    comparison = {
        "challenger": "rule", "control": "no_trade",
        **paired_difference(
            [10.0, 10.0, 10.0], [1.0, 1.0, 1.0],
            rng=random.Random(7), metric="net_after_costs.final_equity",
        ),
    }
    assert comparison["verdict"] == "consistent_difference"
    stored = experiment_store.record_comparison(run_id, comparison)
    assert stored["new"] == 1
    detail = experiment_store.run_results(run_id)
    assert detail["comparisons"][0]["verdict"] == "consistent_difference"
    assert detail["comparisons"][0]["challenger"] == "rule"
    assert "NOT a promotion" in detail["comparisons"][0]["reason"]
    # Replaying the same comparison is a duplicate, not a second row.
    replay = experiment_store.record_comparison(run_id, comparison)
    assert replay["duplicates"] == 1


def test_unknown_run_id_is_unavailable_not_empty_ok(postgres_schema):
    detail = experiment_store.run_results("no-such-run")
    assert detail["status"] == "unavailable"
    assert "no experiment registered" in detail["reason"]
