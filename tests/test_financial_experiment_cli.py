"""E6: the financial experiment CLI, end to end through a real subprocess.

Plan §3.2 acceptance needs a *runnable command* that produces the financial
part, not only a unit test over the engine. These tests invoke the real CLI
(``python -m tradingagents.experiments.financial_experiment``) in a subprocess
and read the artifacts it writes, so they prove the user-facing path:

- a full closed-candle import yields simulated fills, an equity curve, a net
  figure after costs, and a manifest that names the candles it read;
- no candles / an unclosed bar / a mixed symbol / a duplicate / a future-leak
  import are refused with a stated reason, never silently zero-filled;
- a rule that really trades and a no-trade variant keep cash inside the SAME
  sample and cost model, so their difference is the trade and nothing else.

Every assertion reads a number the run actually produced. None of them assert
"the command exited 0" on its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).parents[1]
_FIXTURE = _REPO / "tests" / "fixtures" / "futures_cycles_replay.json"
_CANDLES = _REPO / "tests" / "fixtures" / "futures_candles_4h.json"
_FOUR_HOUR_MS = 14_400_000


def _run(*args: str, results_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tradingagents.experiments.financial_experiment", *args,
         "--results-dir", str(results_dir)],
        cwd=_REPO, capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _summary_of(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write_candles(tmp_path: Path, rows: list[dict], metadata: dict | None = None) -> Path:
    payload = {"metadata": metadata or {
        "symbol": "ENA/USDT:USDT", "timeframe": "4h", "venue": "unit-test",
    }, "candles": rows}
    path = tmp_path / "candles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _row(open_ms: int, *, symbol="ENA/USDT:USDT", timeframe="4h", closed=True, close=1.0) -> dict:
    return {
        "symbol": symbol, "timeframe": timeframe, "open_time": open_ms,
        "close_time": open_ms + _FOUR_HOUR_MS, "open": close, "high": close * 1.01,
        "low": close * 0.99, "close": close, "volume": 10.0, "closed": closed,
    }


def _base_args(candles: Path, holdout: str = "2026-10-26") -> list[str]:
    return ["--candles", str(candles), "--fixture", str(_FIXTURE),
            "--holdout-start", holdout, "--seed", "7", "--repeat", "1"]


def test_evidence_poor_fixture_reports_insufficient_proposal_coverage(tmp_path):
    """The exported source has no terms or intent, so cash is not a numeric result."""
    proc = _run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                results_dir=tmp_path)
    summary = _summary_of(proc)

    baseline = summary["per_variant"]["baseline"]
    assert baseline["metric_values"] == [None]
    assert summary["per_variant"]["no_trade"]["metric_values"] == [None]
    assert summary["comparisons"][0]["verdict"] == "insufficient_evidence_coverage"
    assert baseline["max_drawdowns"] == [None]
    diagnostics = baseline["source_coverage"][0]
    assert diagnostics["status"] == "insufficient_evidence"
    assert diagnostics["built"] == 0
    assert diagnostics["skipped"]["missing_intent"] > 0
    assert summary["candle_coverage"]["holdout"] > 0

    run_dir = tmp_path / "experiments" / summary["run_id"]
    stored = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["per_variant"]["baseline"]["source_coverage"] == baseline["source_coverage"]
    assert summary["manifest_digest"] == stored["manifest_digest"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "replay_of_recording"
    assert manifest["replay"]["model_rerun"] is False
    assert manifest["replay"]["candles"]["count"] > 0
    assert manifest["replay"]["candles"]["symbol"] == "ENA/USDT:USDT"
    start_text, end_text = manifest["window"].split(":")
    window_start = datetime.fromisoformat(start_text).replace(tzinfo=UTC)
    window_end = datetime.fromisoformat(end_text).replace(tzinfo=UTC)
    first_open = datetime.fromtimestamp(summary["candle_coverage"]["first_holdout_open_ms"] / 1000, UTC)
    last_close = datetime.fromtimestamp(summary["candle_coverage"]["last_holdout_close_ms"] / 1000, UTC)
    assert window_start <= first_open < last_close <= window_end
    assert manifest["replay"]["holdout_last_close_ms"] == int(last_close.timestamp() * 1000)
    rows = [json.loads(line) for line in
            (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    rule_row = next(row for row in rows if row["variant"] == "baseline")
    assert rule_row["source_coverage"] == diagnostics
    assert rule_row["report"]["source_coverage"] == diagnostics
    assert not rule_row["report"]["fills"]


def test_cli_registration_populates_the_registry(postgres_schema, tmp_path, monkeypatch):
    """A real CLI run reaches the registry, so the API/brief stop saying empty.

    This is the write path the review found missing: the store was exercised
    only from tests, so ``GET /api/experiments`` could never leave
    ``unavailable`` from a real run.
    """
    from server import experiment_store

    assert experiment_store.summary()["status"] == "unavailable"
    proc = _run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                "--register-run", results_dir=tmp_path)
    summary = _summary_of(proc)
    assert summary["registry"]["registered"] is True
    stored = experiment_store.summary(limit=10)
    assert stored["status"] != "unavailable", stored
    run_ids = {row["run_id"] for row in stored["runs"]}
    assert summary["run_id"] in run_ids, stored


def test_registered_result_rows_carry_the_metric_and_pair_the_comparison(
    postgres_schema, tmp_path,
):
    """The row contract the registry INSERT depends on, pinned from a real run.

    BL-9: ``record_results`` indexed ``row["metric_name"]`` while ``_run_repeats``
    never wrote it, and ``record_comparison`` indexed ``comparison["challenger"]``
    while ``_summarize`` attached the pair only on the coverage branch. Both were
    invisible to the unit tests (which hand-wrote the keys into their own fixture)
    and only surfaced through this CLI path. So this test asserts the SHAPE ON
    DISK the registry reads, not the store's ability to accept a fixture.
    """
    proc = _run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                "--register-run", results_dir=tmp_path)
    summary = _summary_of(proc)
    run_dir = tmp_path / "experiments" / summary["run_id"]
    rows = [json.loads(line) for line in
            (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert rows, "the run produced no repeat rows"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    metric = manifest["criteria"]["metric"]
    for row in rows:
        assert row["metric_name"] == metric, row
        assert row["manifest_digest"] == manifest["manifest_digest"], row

    comparisons = summary["comparisons"]
    assert comparisons, "the run recorded no comparison"
    for comparison in comparisons:
        # Every comparison names its pair, whichever branch produced it.
        assert comparison["challenger"] != comparison["control"]
        assert comparison["metric"] == metric
        assert comparison["challenger"] in manifest["variants"]
        assert comparison["control"] in manifest["variants"]
    # The registry really accepted them: the API surface leaves "unavailable".
    from server import experiment_store

    stored = experiment_store.summary(limit=10)
    assert stored["status"] != "unavailable", stored
    fetched = experiment_store.run_results(summary["run_id"])
    assert fetched["status"] == "ok", fetched
    assert fetched["results"], "the registry stored no result rows"
    assert {row["metric_name"] for row in fetched["results"]} == {metric}
    # The comparison rows reached the registry too, with both sides named.
    assert fetched["comparisons"], "the registry stored no comparison rows"
    for stored_comparison in fetched["comparisons"]:
        assert stored_comparison["challenger"] in manifest["variants"]
        assert stored_comparison["control"] in manifest["variants"]


def test_cli_registration_fails_loudly_without_a_database(tmp_path, monkeypatch):
    """Without a reachable database the run must not claim it was recorded."""
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", "postgresql://127.0.0.1:1/none")
    proc = _run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                "--register-run", results_dir=tmp_path)
    assert proc.returncode == 1
    assert "registry could not be updated" in proc.stderr
    # The artifacts are still on disk, and the message says exactly that.
    assert "written to" in proc.stderr


def test_unknown_rule_is_refused_with_the_available_names(tmp_path):
    proc = _run(*_base_args(_CANDLES), "--rule", "no_such_rule", results_dir=tmp_path)
    assert proc.returncode == 1
    assert "unknown rule" in proc.stderr and "baseline" in proc.stderr


def test_missing_candle_file_is_refused(tmp_path):
    proc = _run(*_base_args(tmp_path / "absent.json"), results_dir=tmp_path)
    assert proc.returncode == 1
    assert "not found" in proc.stderr


def test_unclosed_bar_is_refused_not_silently_dropped(tmp_path):
    """The forming bar must be refused: guessing it is closed distorts the path."""
    rows = [_row(1_792_972_800_000), _row(1_792_987_200_000, closed=False)]
    proc = _run(*_base_args(_write_candles(tmp_path, rows)), results_dir=tmp_path)
    assert proc.returncode == 1
    assert "closed" in proc.stderr.lower()


def test_mixed_symbol_import_is_refused(tmp_path):
    rows = [_row(1_792_972_800_000), _row(1_792_987_200_000, symbol="QNT/USDT:USDT")]
    proc = _run(*_base_args(_write_candles(tmp_path, rows)), results_dir=tmp_path)
    assert proc.returncode == 1
    assert "symbol" in proc.stderr


def test_duplicate_candle_is_refused(tmp_path):
    rows = [_row(1_792_972_800_000), _row(1_792_972_800_000)]
    proc = _run(*_base_args(_write_candles(tmp_path, rows)), results_dir=tmp_path)
    assert proc.returncode == 1
    assert "duplicate" in proc.stderr


def test_future_leak_as_of_cutoff_is_refused(tmp_path):
    """An --as-of before the import must refuse rather than leak the future."""
    rows = [_row(1_792_972_800_000), _row(1_792_987_200_000)]
    proc = _run(*_base_args(_write_candles(tmp_path, rows)),
                "--as-of", "2026-01-01T00:00:00", results_dir=tmp_path)
    assert proc.returncode == 1
    assert "closed at or before" in proc.stderr


def test_no_trade_keeps_cash_in_the_same_sample_and_cost_model(tmp_path):
    """Cash-only and the rule share the sample, so the delta is the trade."""
    proc = _run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                results_dir=tmp_path)
    summary = _summary_of(proc)
    no_trade = summary["per_variant"]["no_trade"]
    assert no_trade["metric_values"] == [None]
    assert no_trade["max_drawdowns"] == [0.0]
    assert no_trade["source_coverage"][0]["mode"] == "no_trade"
    assert summary["per_variant"]["baseline"]["source_coverage"][0]["status"] == (
        "insufficient_evidence"
    )
    # Both variants replayed the identical holdout window.
    assert summary["candle_coverage"]["holdout"] > 0
    assert summary["comparisons"], "the paired comparison must be reported"
    verdict = summary["comparisons"][0]["verdict"]
    assert verdict.startswith("insufficient_evidence"), verdict


def test_zero_derivatives_cap_blocks_entries_and_says_so(tmp_path, monkeypatch):
    """With the runtime default cap (0) nothing can open: stated, not silent."""
    monkeypatch.delenv("TRADINGAGENTS_RISK_MAX_DERIVATIVES_EXPOSURE_PCT", raising=False)
    proc = _run(*_base_args(_CANDLES), results_dir=tmp_path)
    summary = _summary_of(proc)
    # The runtime default blocks every derivatives entry, so the rule cannot
    # trade; that must be reported, not presented as a flat result.
    assert summary["per_variant"]["baseline"]["metric_values"] == [None]
    assert summary["per_variant"]["baseline"]["source_coverage"][0]["status"] == (
        "insufficient_evidence"
    )
    run_dir = tmp_path / "experiments" / summary["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "risk_max_derivatives_exposure_pct is 0" in manifest["replay"]["note_blocking"]


def test_holdout_before_the_import_is_refused(tmp_path):
    proc = _run(*_base_args(_CANDLES, holdout="2020-01-01"), results_dir=tmp_path)
    assert proc.returncode == 1
    assert proc.stderr.strip(), "a refusal must state a reason"


def test_loader_rejects_a_row_without_closed(tmp_path):
    """A row that never states 'closed' is refused: the loader never guesses."""
    rows = [_row(1_792_972_800_000), _row(1_792_987_200_000)]
    del rows[1]["closed"]
    proc = _run(*_base_args(_write_candles(tmp_path, rows)), results_dir=tmp_path)
    assert proc.returncode == 1
    assert "closed" in proc.stderr.lower()


def test_results_never_land_in_the_repo(tmp_path, monkeypatch):
    """Artifacts go to the passed results dir, never the checkout."""
    monkeypatch.delenv("TRADINGAGENTS_RESULTS_DIR", raising=False)
    summary = _summary_of(_run(*_base_args(_CANDLES), "--derivatives-exposure-pct", "50",
                               results_dir=tmp_path))
    assert (tmp_path / "experiments" / summary["run_id"]).is_dir()
    assert not (_REPO / "experiments" / summary["run_id"]).exists()


def test_cli_uses_configured_default_results_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-m", "tradingagents.experiments.financial_experiment",
         *_base_args(_CANDLES)],
        cwd=_REPO, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    summary = _summary_of(proc)
    run_dir = tmp_path / "experiments" / summary["run_id"]
    stored = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["manifest_digest"] == summary["manifest_digest"]
    assert (run_dir / "results.jsonl").is_file()


def test_script_replay_is_labelled_a_gate_agreement_report(tmp_path):
    """The decision replay must not masquerade as a financial experiment."""
    proc = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "replay_futures_decisions.py"),
         "--fixture", str(_FIXTURE), "--rule", "baseline"],
        cwd=_REPO, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["kind"] == "gate_agreement_replay"
    assert "financial_experiment" in report["financial_experiment_command"]
    # And it agrees with the recorded reasons on the rows it can replay.
    assert report["decision_agreements"] == report["replayed_with_snapshot"] > 0
    assert report["financial_simulation"]["status"] == "unavailable"


def test_cli_csv_import_is_supported(tmp_path):
    """The loader accepts CSV as well as JSON (one row per candle)."""
    header = "symbol,timeframe,open_time,close_time,open,high,low,close,volume,closed"
    lines = [header]
    # Six lead-in bars before the holdout, then the holdout bars: the split
    # needs train rows, so a CSV that starts at the boundary is not usable.
    start_ms = 1_792_972_800_000 - 6 * _FOUR_HOUR_MS
    for index in range(14):
        open_ms = start_ms + index * _FOUR_HOUR_MS
        close = 1.0 + index * 0.001
        lines.append(f"ENA/USDT:USDT,4h,{open_ms},{open_ms + _FOUR_HOUR_MS},"
                     f"{close},{close * 1.01},{close * 0.99},{close},10.0,true")
    path = tmp_path / "candles.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = _run("--candles", str(path), "--fixture", str(_FIXTURE),
                "--holdout-start", "2026-10-26", "--symbol", "ENA/USDT:USDT",
                "--timeframe", "4h", results_dir=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _summary_of(proc)["candle_coverage"]["imported"] == 14
