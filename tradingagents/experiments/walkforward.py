"""Temporal walk-forward splits with purge and embargo (plan §4.1).

One concern: given a dated series (closed candles or settled labels), split it
into a train window and a holdout window along time, removing every row whose
label window could overlap the other side:

- **purge** = ``horizon_days - 1`` days removed between train and holdout, so
  a train row's outcome (measured over ``horizon_days``) cannot read data the
  holdout also settles on;
- **embargo** = an extra ``embargo_days`` buffer after the holdout end before
  the series may be reused, guarding overlapping multi-day candles (e.g. 4d
  bars whose open_time sits before the boundary but whose close_time reaches
  into it).

Every split carries its own ``assert_no_leakage`` check; the runner asserts it
before running anything. Rows are compared by their timestamps, never by
index, and a row in the purge zone is dropped and counted — never silently
kept on either side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Every dated row must carry these keys; values are epoch-ms ints.
OPEN_TIME_KEY = "open_time"
CLOSE_TIME_KEY = "close_time"

_DAY_MS = 86_400_000


def _require_ms(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an epoch-ms int, got {value!r}")
    return value


def holdout_start_ms_from_iso(start: str) -> int:
    """Epoch ms of a ``YYYY-MM-DD`` holdout start (UTC midnight), strict format."""
    try:
        parsed = datetime.strptime(str(start), "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"holdout start must be YYYY-MM-DD, got {start!r}") from exc
    if parsed.strftime("%Y-%m-%d") != str(start):
        raise ValueError(f"holdout start must be YYYY-MM-DD, got {start!r}")
    return int(parsed.timestamp() * 1000)


def embargo_boundary(holdout_end_ms: int, embargo_days: int) -> datetime:
    """The moment after which the series may be reused: holdout end + embargo."""
    return datetime.fromtimestamp(
        (holdout_end_ms + embargo_days * _DAY_MS) / 1000, tz=UTC,
    )


@dataclass(frozen=True)
class Split:
    """One train/holdout partition of a dated series, in epoch ms bounds.

    ``train_end_ms`` is the holdout boundary (exclusive for train rows' open);
    ``purged_end_ms`` is holdout end plus the embargo, the boundary after
    which the series may be reused. ``purged_rows`` are the rows the purge
    dropped — published, not hidden.
    """

    train: list[dict]
    holdout: list[dict]
    horizon_days: int
    embargo_days: int
    train_start_ms: int
    train_end_ms: int
    holdout_end_ms: int
    purged_end_ms: int
    purged_rows: list[dict] = field(default_factory=list)

    def assert_no_leakage(self) -> None:
        """Fail loudly if any train row's label window reaches the holdout.

        The purge contract: no train row may close inside the purge zone
        (first holdout open minus (horizon - 1) days), no holdout row may
        open before the boundary, and the embargo boundary sits at holdout
        end plus the embargo. An overlapping multi-day candle whose open
        precedes the boundary but whose close does not is purge-dropped, so
        this check cannot pass while such a candle stays in train.
        """
        if not self.train or not self.holdout:
            raise ValueError("split needs a non-empty train and holdout")
        first_holdout_open = min(row[OPEN_TIME_KEY] for row in self.holdout)
        last_train_close = max(row[CLOSE_TIME_KEY] for row in self.train)
        purge_ms = (self.horizon_days - 1) * _DAY_MS
        # horizon=1 already forbids a train row closing AFTER the holdout's
        # first open (the candle spans the boundary); horizon>1 widens the
        # forbidden zone to the purge boundary.
        if last_train_close > first_holdout_open - purge_ms:
            raise ValueError(
                "label leakage: a train row closes inside the purge zone "
                f"(train close {last_train_close}, purge boundary "
                f"{first_holdout_open - purge_ms}; horizon={self.horizon_days}d)"
            )
        early = [row[OPEN_TIME_KEY] for row in self.holdout
                 if row[OPEN_TIME_KEY] < self.train_end_ms]
        if early:
            raise ValueError(
                f"holdout row opens before the train boundary: {early[0]} < {self.train_end_ms}"
            )
        expected_end = self.holdout_end_ms + self.embargo_days * _DAY_MS
        if self.purged_end_ms != expected_end:
            raise ValueError("purged_end_ms must be holdout end plus the embargo")


def split_walkforward(
    rows: list[dict],
    *,
    holdout_start_ms: int,
    horizon_days: int,
    embargo_days: int = 0,
) -> Split:
    """Split a dated series at ``holdout_start_ms`` with purge + embargo.

    Rows are dicts carrying ``open_time``/``close_time`` in epoch ms. TRAIN
    keeps rows that open before the boundary and close at or before
    ``holdout_start_ms - (horizon_days - 1) days``; HOLDOUT keeps rows that
    open at or after the boundary; everything between (the purge zone) is
    dropped and counted. Splitting before the first row's open is refused.
    """
    if horizon_days < 1:
        raise ValueError("horizon_days must be at least 1")
    if embargo_days < 0:
        raise ValueError("embargo_days must not be negative")
    for row in rows:
        _require_ms(row.get(OPEN_TIME_KEY), OPEN_TIME_KEY)
        _require_ms(row.get(CLOSE_TIME_KEY), CLOSE_TIME_KEY)
        if row[CLOSE_TIME_KEY] < row[OPEN_TIME_KEY]:
            raise ValueError("row closes before it opens; timestamps are broken")

    ordered = sorted(rows, key=lambda row: row[OPEN_TIME_KEY])
    if ordered and ordered[0][OPEN_TIME_KEY] >= holdout_start_ms:
        raise ValueError("holdout_start_ms is before the first row: nothing to train on")

    purge_ms = (horizon_days - 1) * _DAY_MS
    train: list[dict] = []
    holdout: list[dict] = []
    purged: list[dict] = []
    for row in ordered:
        open_ms, close_ms = row[OPEN_TIME_KEY], row[CLOSE_TIME_KEY]
        if open_ms >= holdout_start_ms:
            holdout.append(row)
        elif close_ms <= holdout_start_ms - purge_ms:
            train.append(row)
        else:
            purged.append(row)

    if not holdout:
        raise ValueError("no row reaches the holdout: the window is too short")
    if not train:
        raise ValueError(
            "the purge removed the whole train side: the window is too short for the horizon"
        )

    holdout_end_ms = max(row[CLOSE_TIME_KEY] for row in holdout)
    split = Split(
        train=train, holdout=holdout, horizon_days=horizon_days,
        embargo_days=embargo_days,
        train_start_ms=min(row[OPEN_TIME_KEY] for row in train),
        train_end_ms=holdout_start_ms,
        holdout_end_ms=holdout_end_ms,
        purged_end_ms=holdout_end_ms + embargo_days * _DAY_MS,
        purged_rows=purged,
    )
    split.assert_no_leakage()
    return split


__all__ = [
    "Split",
    "split_walkforward",
    "holdout_start_ms_from_iso",
    "embargo_boundary",
    "OPEN_TIME_KEY",
    "CLOSE_TIME_KEY",
]
