"""E1: walk-forward splits with purge + embargo never leak labels (plan §4.3).

Purge = (horizon - 1) days between train and holdout, embargo extends the
no-reuse boundary past the holdout end; an overlapping multi-day candle whose
close reaches into the other side is dropped and counted, never kept. Every
test asserts no temporal overlap and no label-window reach.
"""

import pytest

from tradingagents.experiments.walkforward import (
    CLOSE_TIME_KEY,
    OPEN_TIME_KEY,
    embargo_boundary,
    holdout_start_ms_from_iso,
    split_walkforward,
)

pytestmark = pytest.mark.unit

_DAY_MS = 86_400_000


def _row(index: int, open_ms: int, close_ms: int) -> dict:
    return {"id": index, OPEN_TIME_KEY: open_ms, CLOSE_TIME_KEY: close_ms}


def _daily_rows(count: int, start_ms: int) -> list[dict]:
    return [
        _row(i, start_ms + i * _DAY_MS, start_ms + (i + 1) * _DAY_MS)
        for i in range(count)
    ]


def test_purge_removes_label_overlap_rows():
    """A train row's horizon-1 window cannot reach the holdout: purge drops it."""
    start = holdout_start_ms_from_iso("2026-01-01")
    rows = _daily_rows(50, start - 30 * _DAY_MS)
    horizon = 5
    split = split_walkforward(rows, holdout_start_ms=start, horizon_days=horizon)
    last_train_close = max(r[CLOSE_TIME_KEY] for r in split.train)
    first_holdout_open = min(r[OPEN_TIME_KEY] for r in split.holdout)
    assert last_train_close <= first_holdout_open - (horizon - 1) * _DAY_MS
    assert split.holdout == [r for r in rows if r[OPEN_TIME_KEY] >= start]
    assert len(split.purged_rows) == horizon - 1


def test_no_temporal_overlap_between_train_and_holdout():
    start = holdout_start_ms_from_iso("2026-02-01")
    rows = _daily_rows(40, start - 20 * _DAY_MS)
    split = split_walkforward(rows, holdout_start_ms=start, horizon_days=1)
    # horizon=1: a train row closing exactly AT the boundary has finished its
    # label before the holdout's first open — disjoint is close < open, and
    # close == boundary is legal. A train row closing AFTER it is leakage.
    first_holdout_open = min(r[OPEN_TIME_KEY] for r in split.holdout)
    assert all(r[CLOSE_TIME_KEY] <= first_holdout_open for r in split.train)
    assert all(r[OPEN_TIME_KEY] >= start for r in split.holdout)
    split.assert_no_leakage()


def test_multi_day_candle_spanning_the_boundary_is_dropped_not_kept():
    """The 4d-candle case: opens before the boundary, closes after it."""
    start = holdout_start_ms_from_iso("2026-03-01")
    rows = _daily_rows(20, start - 10 * _DAY_MS)
    four_day = _row(99, start - 2 * _DAY_MS, start + 2 * _DAY_MS)
    split = split_walkforward(rows + [four_day], holdout_start_ms=start, horizon_days=1)
    train_ids = {r["id"] for r in split.train}
    holdout_ids = {r["id"] for r in split.holdout}
    assert 99 not in train_ids and 99 not in holdout_ids
    assert [r["id"] for r in split.purged_rows] == [99]
    with pytest.raises(ValueError, match="leakage"):
        # If the candle HAD been kept in train, the assertion must fail.
        from tradingagents.experiments.walkforward import Split
        bad = Split(
            train=split.train + [four_day], holdout=split.holdout,
            horizon_days=1, embargo_days=0, train_start_ms=split.train_start_ms,
            train_end_ms=start, holdout_end_ms=split.holdout_end_ms,
            purged_end_ms=split.purged_end_ms,
        )
        bad.assert_no_leakage()


def test_embargo_extends_the_reuse_boundary_past_the_holdout():
    start = holdout_start_ms_from_iso("2026-04-01")
    rows = _daily_rows(30, start - 15 * _DAY_MS)
    split = split_walkforward(
        rows, holdout_start_ms=start, horizon_days=1, embargo_days=3,
    )
    boundary = embargo_boundary(split.holdout_end_ms, split.embargo_days)
    assert split.purged_end_ms == split.holdout_end_ms + 3 * _DAY_MS
    assert boundary.timestamp() * 1000 == split.purged_end_ms


def test_purge_removing_the_whole_train_side_is_refused():
    start = holdout_start_ms_from_iso("2026-05-01")
    rows = _daily_rows(6, start - 3 * _DAY_MS)
    with pytest.raises(ValueError, match="purge removed the whole train"):
        split_walkforward(rows, holdout_start_ms=start, horizon_days=30)


def test_holdout_start_at_or_after_the_first_row_is_refused():
    start = holdout_start_ms_from_iso("2026-06-01")
    rows = _daily_rows(5, start)
    # The boundary at/before the first row's open leaves nothing to train on.
    with pytest.raises(ValueError, match="nothing to train on"):
        split_walkforward(rows, holdout_start_ms=start - 5 * _DAY_MS, horizon_days=1)
    with pytest.raises(ValueError, match="nothing to train on"):
        split_walkforward(rows, holdout_start_ms=start, horizon_days=1)


def test_split_is_by_timestamps_not_index_order():
    """Out-of-order input sorts by timestamp; a broken row is refused."""
    start = holdout_start_ms_from_iso("2026-07-01")
    rows = _daily_rows(30, start - 20 * _DAY_MS)
    shuffled = list(reversed(rows))
    split = split_walkforward(shuffled, holdout_start_ms=start, horizon_days=1)
    assert [r[OPEN_TIME_KEY] for r in split.train] == sorted(
        r[OPEN_TIME_KEY] for r in split.train
    )
    broken = _row(0, start + _DAY_MS, start - _DAY_MS)
    with pytest.raises(ValueError, match="closes before it opens"):
        split_walkforward(rows + [broken], holdout_start_ms=start, horizon_days=1)
