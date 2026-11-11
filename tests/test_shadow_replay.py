"""Offline shadow replay: runnable on the recorded export, honest about its limits.

The replay is the operator-facing proof that the shadow pipeline works without a
live venue. These tests run it for real over the committed fixture and check what
it may and may not claim:

- every row is ``retrospective`` (the replay runs long after the decisions);
- a filter that needs more history than the export carries reports ``error``
  with the count it got — never a fabricated verdict;
- with an injected contiguous candle history the same code produces real
  verdicts, so the machinery is shown to work rather than assumed;
- nothing in the report claims profit, and the disclaimer rides along.
"""

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradingagents.dataflows.market_data import Candle

REPO = Path(__file__).parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "futures_cycles_replay.json"

pytestmark = pytest.mark.unit


def _replay_module():
    import importlib.util

    path = REPO / "scripts" / "replay_shadow_observations.py"
    spec = importlib.util.spec_from_file_location("replay_shadow_observations", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _recorded_rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _contiguous_history(symbol: str, *, start_ms: int, bars: int = 60) -> list[Candle]:
    """A deterministic contiguous 1h series ending at ``start_ms``."""
    step = 3_600_000
    candles = []
    for index in range(bars):
        open_time = start_ms - (bars - index) * step
        price = 100.0 + math.sin(index / 4.0) * 3.0 + index * 0.4
        candles.append(Candle(
            symbol=symbol, timeframe="1h", open_time=open_time,
            close_time=open_time + step, open=price - 0.2,
            high=price + 0.9, low=price - 0.9, close=price,
            volume=100.0 + index, is_closed=True,
        ))
    return candles


def test_replay_runs_offline_over_the_real_export():
    module = _replay_module()
    report = module.replay(
        _recorded_rows(), ["filter:donchian_breakout"],
        timeframe="1h", horizon_hours=24,
    )
    assert report["kind"] == "shadow_observation_replay"
    assert report["cycles_seen"] == 551
    assert report["cycles_with_snapshot"] == 77
    # The export keeps two decision candles per cycle, never adjacent across
    # cycles, so the contiguous tail is a single bar — and the report says so.
    assert report["cycles_by_closed_candle_count"] == {"1": 77}
    assert report["observations"] == 77
    assert report["status_counts"] == {"error": 77}
    assert report["failure_reasons"]["filter:donchian_breakout"] == {
        "needs at least 4 closed candles, got 1": 77,
    }
    # No verdict is claimed from a series that could not be read.
    assert report["verdict_counts"] == {}


def test_every_replayed_row_is_labelled_retrospective():
    module = _replay_module()
    rows = [row for row in _recorded_rows() if row.get("snapshot")][:20]
    report = module.replay(
        rows, ["filter:momentum_zscore"], timeframe="1h", horizon_hours=24,
    )
    assert report["observations"] == 20
    assert report["generation_counts"] == {"retrospective": report["observations"]}
    for row in report["rows"]:
        assert row["generation_kind"] == "retrospective"
        assert "already known" in row["provenance"]["generation_kind_basis"]
        assert row["provenance"]["source"] == "replay_shadow_observations"


def test_outcome_groups_are_split_from_the_exported_status():
    module = _replay_module()
    report = module.replay(
        _recorded_rows(), ["filter:donchian_breakout"],
        timeframe="4h", horizon_hours=12,
    )
    groups = report["outcome_group_counts"]
    # The export holds a mix of completed entries and rejections; each lands in
    # its own group instead of everything defaulting to "abstained".
    assert groups.get("rejected", 0) > 0
    assert set(groups) <= {"executed", "rejected", "abstained"}


def test_a_contiguous_candle_history_turns_the_replay_into_real_verdicts():
    """The same code path produces verdicts once the history is real."""
    module = _replay_module()
    # Same-candle cycles: the export's snapshot carries only the decision candle,
    # and the decisions span several symbols and hours, so the injected history
    # must cover every symbol involved.
    stamp = "2026-10-26T00:00:00+00:00"
    rows = [
        row for row in _recorded_rows()
        if row.get("snapshot") and stamp in str(row["cycle_id"])
    ][:5]
    assert len(rows) == 5
    # The synthetic history ends just BEFORE each symbol's earliest snapshot
    # candle, so the snapshot's real prices are the last bar for that symbol's
    # first cycle and the two sources never disagree about a shared bar.
    earliest: dict[str, int] = {}
    for row in rows:
        symbol = str(row["symbol"])
        opened = int(row["snapshot"]["one_hour"]["open_time"])
        earliest[symbol] = min(earliest.get(symbol, opened), opened)
    history = {
        symbol: _contiguous_history(symbol, start_ms=opened, bars=80)
        for symbol, opened in earliest.items()
    }
    report = module.replay(
        rows, ["filter:donchian_breakout", "filter:momentum_zscore"],
        timeframe="1h", horizon_hours=24, history=history,
    )
    assert report["cycles_with_a_closed_candle_tail"] == 5
    assert report["observations"] == 10
    # AAVE decides twice an hour apart; the synthetic history was built to end at
    # that symbol's FIRST snapshot candle, so the later AAVE cycle has a real
    # one-hour gap and gets the snapshot bar only. That is the honest outcome — a
    # gap is never bridged — and the report shows both tail lengths, keyed by
    # length with the number of cycles that got it.
    tail_lengths = {
        int(bars): cycles
        for bars, cycles in report["cycles_by_closed_candle_count"].items()
    }
    assert tail_lengths.get(1) == 1
    assert sum(cycles for bars, cycles in tail_lengths.items() if bars >= 3) == 4
    ok = [row for row in report["rows"] if row["status"] == "ok"]
    assert ok, report["failure_reasons"]
    assert report["verdict_counts"]
    for provider, counts in report["verdict_counts"].items():
        assert provider.startswith("filter:")
        assert sum(counts.values()) <= len(ok)
    # The label still says a deterministic rule has no distribution.
    assert all(row["calibration_label"] == "deterministic_rule_no_distribution" for row in ok)
    assert all(row["paths"] == [] for row in ok)


def test_contiguous_tail_never_bridges_a_gap():
    module = _replay_module()
    symbol = "BTC/USDT:USDT"
    series = _contiguous_history(symbol, start_ms=1_700_000_000_000, bars=10)
    gapped = series[:5] + series[7:]  # drop two bars in the middle
    as_of = datetime.fromtimestamp(gapped[-1].close_time / 1000, UTC)
    tail = module.contiguous_tail(gapped, as_of, "1h", max_bars=50)
    # Only the run after the gap survives.
    assert len(tail) == 3
    assert [candle.open_time for candle in tail] == [
        candle.open_time for candle in gapped[-3:]
    ]


def test_contiguous_tail_excludes_candles_closing_after_the_moment():
    module = _replay_module()
    symbol = "BTC/USDT:USDT"
    series = _contiguous_history(symbol, start_ms=1_700_000_000_000, bars=8)
    as_of = datetime.fromtimestamp(series[4].close_time / 1000, UTC)
    tail = module.contiguous_tail(series, as_of, "1h", max_bars=50)
    assert len(tail) == 5
    assert tail[-1].close_time <= as_of.timestamp() * 1000


def test_replay_seed_is_stable_across_processes():
    """F4: a builtin-hash seed changes every run; the replay must not.

    Run the derivation in two SEPARATE processes and require identical output —
    that is the only way to catch a per-process salted hash, which looks stable
    inside one interpreter.
    """
    import subprocess
    import sys

    script = (
        f"import sys; sys.path.insert(0, {str(REPO)!r});"
        "from tradingagents.shadow.provider import stable_seed;"
        "print(stable_seed('futures:BTC/USDT:USDT:2026-10-26T00:00:00+00:00:2026-10-26T03:00:00+00:00:1',"
        " 'filter:donchian_breakout'))"
    )
    outputs = set()
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, f"the seed changed between processes: {outputs}"
    # And it is a real integer, not a degenerate constant.
    assert outputs.pop().isdigit()


def test_replay_uses_the_worker_seed_derivation():
    """The replay and the worker must agree, or the same cycle draws differently."""
    module = _replay_module()
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "stable_seed(" in source
    assert "hash((" not in source, "a builtin hash() would be per-process"


def test_missing_model_provider_is_reported_not_simulated(monkeypatch, tmp_path):
    """A requested provider that cannot run is listed with its reason."""
    module = _replay_module()
    import tradingagents.shadow.kronos as kronos_module

    monkeypatch.setattr(
        kronos_module.KronosProvider, "unavailable_reason",
        lambda self: "the isolated Kronos interpreter is absent (test)",
    )
    provider, reason = module._build_provider("kronos")
    assert provider is None
    assert "interpreter is absent" in reason


def test_unknown_provider_is_refused():
    module = _replay_module()
    provider, reason = module._build_provider("not-a-provider")
    assert provider is None
    assert "unknown provider" in reason


def test_report_carries_the_disclaimer_and_no_profit_claim():
    module = _replay_module()
    report = module.replay(
        _recorded_rows()[:20], ["filter:donchian_breakout"],
        timeframe="1h", horizon_hours=24,
    )
    assert "not financial advice" in report["disclaimer"].lower()
    assert report["limitations"]
    blob = json.dumps(report).lower()
    for forbidden in ("profit", "pnl", "sharpe", "win_rate"):
        assert forbidden not in blob, f"the replay must not claim {forbidden}"


def test_cli_writes_a_report_file(tmp_path):
    module = _replay_module()
    output = tmp_path / "shadow_replay.json"
    exit_code = module.main([
        "--fixture", str(FIXTURE),
        "--provider", "filter:donchian_breakout",
        "--max-cycles", "30",
        "--report", str(output),
    ])
    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["cycles_seen"] == 30
    assert payload["kind"] == "shadow_observation_replay"
