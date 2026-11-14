"""Regression tests for the recorded confirmation-rule experiment."""

import json
from datetime import UTC, datetime

import pytest

from tradingagents.experiments.replay_experiment import run_replay_experiment

pytestmark = pytest.mark.unit


def _cycle(day: int, hour: int, reason: str | None, *, bullish: bool = True) -> dict:
    open_ms = int(datetime(2026, 10, day, hour, tzinfo=UTC).timestamp() * 1000)
    open_iso = datetime.fromtimestamp(open_ms / 1000, UTC).isoformat()
    symbol = "BTC/USDT:USDT"
    close = 101.0 if bullish else 99.0

    def candle(timeframe: str, duration: int) -> dict:
        return {
            "symbol": symbol, "timeframe": timeframe, "open_time": open_ms,
            "close_time": open_ms + duration, "open": 100.0, "high": 102.0,
            "low": 98.0, "close": close, "volume": 1.0, "is_closed": True,
        }

    return {
        "cycle_id": f"futures:{symbol}:{open_iso}:{open_iso}:1",
        "symbol": symbol, "status": "completed", "phase": "confirmation",
        "signal": "Buy", "confirmation_reason": reason,
        "snapshot": {"one_hour": candle("1h", 3_600_000),
                     "four_hour": candle("4h", 14_400_000)},
    }


def _run(tmp_path, cycles: list[dict]) -> dict:
    fixture = tmp_path / "cycles.json"
    fixture.write_text(json.dumps(cycles), encoding="utf-8")
    return run_replay_experiment(
        rules=["baseline"], window="2026-10-26:2026-10-26", seed=7,
        repeat=1, fixture_path=fixture, results_dir=tmp_path,
    )


def test_decision_agreement_includes_recorded_rejections_and_full_end_date(tmp_path):
    summary = _run(tmp_path, [
        _cycle(26, 0, "aligned_direction"),
        _cycle(26, 12, "direction_mismatch", bullish=False),
        _cycle(27, 0, "aligned_direction"),
    ])
    assert summary["cycles_in_window"] == 2
    assert summary["per_rule"]["baseline"]["confirmed_by_rule"] == 1
    assert summary["per_rule"]["baseline"]["rates"] == [1.0]


def test_decision_agreement_rejects_disagreement(tmp_path):
    summary = _run(tmp_path, [_cycle(26, 12, "direction_mismatch", bullish=True)])
    assert summary["per_rule"]["baseline"]["rates"] == [0.0]
    assert summary["per_rule"]["baseline"]["decision_agreements"] == 0


@pytest.mark.parametrize("reason", [None, "", "unknown_reason"])
def test_unrecognized_recorded_confirmation_makes_agreement_unavailable(tmp_path, reason):
    summary = _run(tmp_path, [_cycle(26, 12, reason)])
    result = summary["per_rule"]["baseline"]
    assert result["cycles_replayed"] == 1
    assert result["unknown_recorded_decisions"] == 1
    assert result["rates"] == [None]
