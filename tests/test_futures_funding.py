from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.execution.futures_funding import (
    evaluate_time_stop_postpone,
    fetch_funding_income,
    funding_schedule,
)


class _ScheduleExchange:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def fetch_funding_rate(self, symbol, params=None):
        self.calls.append(("funding_rate", symbol, params))
        if self.error is not None:
            raise self.error
        return self.payload


class _IncomeExchange:
    def __init__(self, rows=None, error=None):
        self.rows = rows
        self.error = error
        self.calls = []

    def fetch_funding_history(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("funding_history", symbol, since, limit, params))
        if self.error is not None:
            raise self.error
        return self.rows


def test_funding_schedule_parses_the_binance_shape():
    exchange = _ScheduleExchange({
        "fundingRate": 0.0001,
        "nextFundingTimestamp": None,
        "fundingTimestamp": 1_700_000_000_000,
        "interval": "8h",
    })

    schedule = funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC))

    assert exchange.calls == [("funding_rate", "BTC/USDT:USDT", {"subType": "linear"})]
    assert schedule["rate"] == pytest.approx(0.0001)
    assert schedule["next_funding_at"] == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert schedule["interval_hours"] == pytest.approx(8.0)


def test_funding_schedule_prefers_the_explicit_next_fields():
    soon = (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    exchange = _ScheduleExchange({
        "fundingRate": -0.0002,
        "nextFundingDatetime": soon,
        "fundingTimestamp": 1_000_000_000_000,
        "info": {"nextFundingTime": "1000000000000"},
    })

    schedule = funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC))

    # The next-* datetime wins over Binance's fundingTimestamp fallback and
    # the raw info string, which would read as year 2001.
    assert schedule["next_funding_at"] is not None
    assert schedule["next_funding_at"] > datetime.now(UTC)
    assert schedule["rate"] == pytest.approx(-0.0002)


def test_funding_schedule_reads_info_next_funding_time_as_last_resort():
    soon_ms = int((datetime.now(UTC) + timedelta(minutes=30)).timestamp() * 1000)
    exchange = _ScheduleExchange({
        "fundingRate": 0.0001,
        "info": {"nextFundingTime": str(soon_ms)},
    })

    schedule = funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC))

    assert schedule["next_funding_at"] is not None
    assert abs((schedule["next_funding_at"] - datetime.now(UTC)).total_seconds()) < 3600


def test_funding_schedule_tolerates_missing_interval_and_timestamp():
    exchange = _ScheduleExchange({"fundingRate": 0.0001})

    schedule = funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC))

    assert schedule == {
        "next_funding_at": None,
        "rate": pytest.approx(0.0001),
        "interval_hours": None,
    }


def test_funding_schedule_rejects_a_settlement_beyond_the_horizon():
    far = int((datetime.now(UTC) + timedelta(hours=72)).timestamp() * 1000)
    exchange = _ScheduleExchange({"fundingRate": 0.0001, "nextFundingTimestamp": far})

    assert funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC)) is None


def test_broken_schedule_reads_are_fail_closed():
    now = datetime.now(UTC)
    raising = _ScheduleExchange(error=RuntimeError("venue down"))
    assert funding_schedule(raising, "BTC/USDT:USDT", now) is None
    assert funding_schedule(_ScheduleExchange(payload="nope"), "BTC/USDT:USDT", now) is None
    assert funding_schedule(_ScheduleExchange(payload=None), "BTC/USDT:USDT", now) is None
    assert funding_schedule(_ScheduleExchange(payload={"fundingRate": "junk"}), "BTC/USDT:USDT", now) == {
        "next_funding_at": None,
        "rate": None,
        "interval_hours": None,
    }


@pytest.mark.parametrize("garbage", ["1e15", 10**20])
def test_out_of_range_settlement_timestamps_read_as_absent(garbage):
    # A millisecond count millennia long makes datetime.fromtimestamp raise
    # (OSError on Windows); the parse must swallow it, not let it escape the
    # fail-closed read and wedge a past-deadline position on error outcomes.
    exchange = _ScheduleExchange({"fundingRate": 0.0001, "fundingTimestamp": garbage})

    schedule = funding_schedule(exchange, "BTC/USDT:USDT", datetime.now(UTC))

    assert schedule == {
        "next_funding_at": None,
        "rate": pytest.approx(0.0001),
        "interval_hours": None,
    }


@pytest.mark.parametrize(
    ("postpone_minutes", "interval_hours", "expected"),
    [
        (90.0, 8.0, "postpone"),    # window well inside the settlement cycle
        (510.0, 8.0, "close"),      # window spans the cycle: would defer forever
        (90.0, 1.0, "close"),       # hourly symbol under the default 90m window
        (45.0, None, "postpone"),   # no published interval falls back to 24h
        (1500.0, None, "close"),    # a 25h window is refused even then
    ],
)
def test_a_window_spanning_the_funding_cycle_never_postpones(
    postpone_minutes, interval_hours, expected
):
    now = datetime.now(UTC)

    assert evaluate_time_stop_postpone(
        side="short",
        rate=0.0001,
        next_funding_at=now + timedelta(minutes=30),
        now=now,
        postpone_minutes=postpone_minutes,
        interval_hours=interval_hours,
    ) == expected


@pytest.mark.parametrize(
    ("side", "rate", "minutes_to_settlement", "postpone_minutes", "expected"),
    [
        ("long", 0.0001, 30.0, 90.0, "close"),      # long pays a positive rate
        ("short", 0.0001, 30.0, 90.0, "postpone"),  # short is paid
        ("long", -0.0001, 30.0, 90.0, "postpone"),  # long is paid a negative rate
        ("short", -0.0001, 30.0, 90.0, "close"),    # short pays a negative rate
        ("short", 0.0001, 120.0, 90.0, "close"),    # settlement outside the window
        ("short", 0.0001, -5.0, 90.0, "close"),     # already settled
        ("short", 0.0001, 90.0, 90.0, "postpone"),  # exactly on the window edge
        ("short", None, 30.0, 90.0, "close"),       # rate unknown
        ("short", "junk", 30.0, 90.0, "close"),     # rate malformed
        ("short", 0.0001, 30.0, 0.0, "close"),      # window disabled
        ("short", 0.0001, 30.0, -5.0, "close"),
        ("short", 0.0001, 30.0, float("inf"), "close"),  # junk window reads disabled
    ],
)
def test_time_stop_postpone_decisions(
    side, rate, minutes_to_settlement, postpone_minutes, expected
):
    now = datetime.now(UTC)
    next_funding_at = (
        None if rate is None else now + timedelta(minutes=minutes_to_settlement)
    )

    assert evaluate_time_stop_postpone(
        side=side,
        rate=rate,
        next_funding_at=next_funding_at,
        now=now,
        postpone_minutes=postpone_minutes,
    ) == expected


def test_missing_next_funding_at_closes():
    assert evaluate_time_stop_postpone(
        side="short",
        rate=0.0001,
        next_funding_at=None,
        now=datetime.now(UTC),
        postpone_minutes=90.0,
    ) == "close"


def test_naive_timestamps_are_read_as_utc():
    now = datetime.now(UTC).replace(tzinfo=None)

    assert evaluate_time_stop_postpone(
        side="short",
        rate=0.0001,
        next_funding_at=now + timedelta(minutes=30),
        now=now,
        postpone_minutes=90.0,
    ) == "postpone"


def test_invalid_side_is_rejected():
    with pytest.raises(ValueError, match="side"):
        evaluate_time_stop_postpone(
            side="both",
            rate=0.0001,
            next_funding_at=datetime.now(UTC),
            now=datetime.now(UTC),
            postpone_minutes=90.0,
        )


def test_funding_income_rows_pass_through_and_fail_closed():
    rows = [{"amount": 1.5}, {"amount": -0.5}]
    ok = _IncomeExchange(rows)
    assert fetch_funding_income(ok, "BTC/USDT:USDT", 1_000) == rows
    assert ok.calls == [("funding_history", "BTC/USDT:USDT", 1_000, 1000, {})]

    failing = _IncomeExchange(error=RuntimeError("venue down"))
    assert fetch_funding_income(failing, "BTC/USDT:USDT", 1_000) is None

    garbage = _IncomeExchange(rows="nope")
    assert fetch_funding_income(garbage, "BTC/USDT:USDT", 1_000) is None


def test_a_client_without_funding_history_reads_as_an_empty_ledger():
    class _Bare:
        pass

    assert fetch_funding_income(_Bare(), "BTC/USDT:USDT", 1_000) == []
