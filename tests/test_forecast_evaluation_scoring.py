"""Scoring tests for forward Kronos forecast evaluation."""

from datetime import UTC, datetime, timedelta

import ccxt
import pytest

from server import forecast_evaluation_scoring as scoring
from tradingagents.dataflows.market_data import Candle

pytestmark = pytest.mark.unit

_HOUR_MS = 3_600_000
_DATA_OPEN_TIME = 1_800_000_000_000
_TARGET_CLOSE_TIME = _DATA_OPEN_TIME + 3 * _HOUR_MS
_TARGET_AS_OF = datetime.fromtimestamp(_TARGET_CLOSE_TIME / 1000, UTC)


def _cycle() -> dict:
    return {
        "cycle_id": "cycle-1", "symbol": "BTC/USDT:USDT", "payload": {},
        "cycle_timeframe": "1h", "cycle_selected_open": _DATA_OPEN_TIME,
    }


def _block(**overrides) -> dict:
    block = {
        "symbol": "BTC/USDT:USDT",
        "status": "ok", "generation_kind": "forward", "timeframe": "1h",
        "data_open_time": _DATA_OPEN_TIME, "horizon_bars": 2, "horizon_hours": 2,
        "as_of": datetime.fromtimestamp((_DATA_OPEN_TIME + _HOUR_MS) / 1000, UTC).isoformat(),
        "created_at": datetime.fromtimestamp((_DATA_OPEN_TIME + _HOUR_MS + 1_000) / 1000, UTC).isoformat(),
        "recorded_at": datetime.fromtimestamp((_DATA_OPEN_TIME + _HOUR_MS + 2_000) / 1000, UTC).isoformat(),
        "reference_close": 100.0, "direction": "up",
        "direction_ratio": {"up": 0.75, "down": 0.25, "flat": 0.0},
        "band": {"low": 95.0, "p50": 110.0, "high": 120.0},
        "effective_lookback_bars": 64,
        "profile": "small_base",
    }
    block.update(overrides)
    return block


def _target_candle(close: float = 116.0) -> Candle:
    return Candle(
        symbol="BTC/USDT:USDT", timeframe="1h",
        open_time=_TARGET_CLOSE_TIME - _HOUR_MS, close_time=_TARGET_CLOSE_TIME,
        open=114.0, high=max(116.0, close), low=min(114.0, close),
        close=close, volume=10.0, is_closed=True,
    )


def _bybit_exchange(monkeypatch, open_times):
    exchange = ccxt.bybit()
    exchange.set_markets([{
        "id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC",
        "quote": "USDT", "settle": "USDT", "settleId": "USDT",
        "type": "swap", "spot": False, "swap": True, "future": False,
        "contract": True, "linear": True, "inverse": False, "contractSize": 1,
    }])
    calls = []

    def fetch(params):
        calls.append(dict(params))
        start, end = params.get("start", 0), params.get("end", float("inf"))
        selected = sorted((stamp for stamp in open_times if start <= stamp <= end), reverse=True)
        return {"result": {"list": [
            [str(stamp), "114", "116", "114", "116", "10", "1160"]
            for stamp in selected[:params["limit"]]
        ]}}

    monkeypatch.setattr(exchange, "publicGetV5MarketKline", fetch)
    return exchange, calls


def test_bybit_retry_fetches_exact_historical_candle_offline(monkeypatch):
    target_open = _TARGET_CLOSE_TIME - _HOUR_MS
    exchange, calls = _bybit_exchange(monkeypatch, [target_open, target_open + _HOUR_MS])

    row = scoring.evaluate(
        _cycle(), _block(), exchange=exchange, now=_TARGET_AS_OF + timedelta(hours=2),
    )

    assert row["status"] == "evaluated"
    assert row["target_close"] == 116.0
    assert len(calls) == 1
    assert calls[0]["start"] == target_open
    assert calls[0]["end"] == target_open
    assert calls[0]["limit"] == 1
    assert calls[0]["category"] == "linear"


def test_bybit_missing_target_does_not_score_newer_candle_offline(monkeypatch):
    target_open = _TARGET_CLOSE_TIME - _HOUR_MS
    exchange, calls = _bybit_exchange(monkeypatch, [target_open + _HOUR_MS])

    row = scoring.evaluate(
        _cycle(), _block(), exchange=exchange, now=_TARGET_AS_OF + timedelta(hours=2),
    )

    assert row["status"] == "unavailable"
    assert row["evaluated_at"] is None
    assert "target_close" not in row
    assert len(calls) == 1
    assert calls[0]["start"] == calls[0]["end"] == target_open


def test_bybit_old_candle_is_not_substituted_for_target(monkeypatch):
    target_open = _TARGET_CLOSE_TIME - _HOUR_MS
    exchange, calls = _bybit_exchange(monkeypatch, [target_open - _HOUR_MS])

    row = scoring.evaluate(
        _cycle(), _block(), exchange=exchange, now=_TARGET_AS_OF + timedelta(hours=2),
    )

    assert row["status"] == "unavailable"
    assert row["evaluated_at"] is None
    assert "target_close" not in row
    assert calls[0]["start"] == calls[0]["end"] == target_open


def test_bybit_unsupported_params_fails_closed_without_retry():
    class UnsupportedBybit:
        id = "bybit"

        def __init__(self):
            self.calls = []

        def fetch_ohlcv(self, symbol, *, timeframe, since=None, limit=None, params=None):
            self.calls.append({"since": since, "limit": limit, "params": params})
            if params is not None:
                raise TypeError("end parameter unsupported")
            return [[_TARGET_CLOSE_TIME - _HOUR_MS, 114, 116, 114, 116, 10]]

    exchange = UnsupportedBybit()
    row = scoring.evaluate(
        _cycle(), _block(), exchange=exchange, now=_TARGET_AS_OF + timedelta(hours=2),
    )

    assert row["status"] == "unavailable"
    assert row["evaluated_at"] is None
    assert "target_close" not in row
    assert exchange.calls == [{
        "since": _TARGET_CLOSE_TIME - _HOUR_MS, "limit": 1,
        "params": {"end": _TARGET_CLOSE_TIME - _HOUR_MS},
    }]


def test_evaluate_is_pending_until_target_horizon():
    row = scoring.evaluate(
        _cycle(), _block(), exchange=None, now=_TARGET_AS_OF - timedelta(seconds=1),
    )
    assert row["status"] == "pending"
    assert row["target_as_of"] == _TARGET_AS_OF


def test_evaluate_scores_exact_target_and_p50_metrics():
    seen = {}

    def fetch(exchange, symbol, timeframe, *, as_of, limit):
        seen.update(exchange=exchange, symbol=symbol, timeframe=timeframe, as_of=as_of, limit=limit)
        return [_target_candle()]

    exchange = object()
    evaluated_at = _TARGET_AS_OF + timedelta(minutes=1)
    row = scoring.evaluate(_cycle(), _block(), exchange=exchange, now=evaluated_at, candle_fetcher=fetch)
    assert row["status"] == "evaluated"
    assert row["target_close"] == 116.0
    assert row["predicted_close"] == 110.0
    assert row["absolute_error"] == 6.0
    assert row["absolute_error_pct"] == pytest.approx(6 / 110 * 100)
    assert row["target_return"] == pytest.approx(0.16)
    assert row["directional_correct"] is True
    assert row["band_contains_target"] is True
    assert row["evaluated_at"] == evaluated_at
    assert row["provenance"]["timestamp_evidence"] == "self_reported_unverified"
    assert seen == {
        "exchange": exchange, "symbol": "BTC/USDT:USDT", "timeframe": "1h",
        "as_of": _TARGET_AS_OF, "limit": 1,
    }


def test_evaluate_requires_exact_target_candle():
    target = _target_candle()
    wrong = Candle(
        symbol=target.symbol, timeframe=target.timeframe,
        open_time=target.open_time + _HOUR_MS, close_time=target.close_time + _HOUR_MS,
        open=target.open, high=target.high, low=target.low, close=target.close,
        volume=target.volume, is_closed=True,
    )
    row = scoring.evaluate(
        _cycle(), _block(), exchange=object(), now=_TARGET_AS_OF + timedelta(seconds=1),
        candle_fetcher=lambda *_args, **_kwargs: [wrong],
    )
    assert row["status"] == "unavailable"
    assert row["evaluated_at"] is None


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ({"status": "ok", "generation_kind": "retrospective"}, "invalid"),
        ({"status": "unavailable", "reason": "no data"}, "unavailable"),
        ({"status": "timeout", "reason": "timeout"}, "unavailable"),
        ({"status": "error", "reason": "error"}, "unavailable"),
        ({"status": "unknown", "reason": "unknown"}, "invalid"),
    ],
)
def test_evaluate_maps_non_forward_and_non_ok_blocks(block, expected):
    row = scoring.evaluate(_cycle(), _block(**block), exchange=None)
    assert row["status"] == expected


def test_evaluate_rejects_mismatched_symbol():
    row = scoring.evaluate(_cycle(), _block(symbol="ETH/USDT:USDT"), exchange=None)
    assert row["status"] == "invalid"
    assert "symbol" in row["reason"]


@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        ({"cycle_timeframe": "15m"}, "timeframe"),
        ({"cycle_selected_open": _DATA_OPEN_TIME - _HOUR_MS}, "candle"),
        ({"cycle_timeframe": None}, "identity"),
        ({"cycle_selected_open": None}, "identity"),
    ],
)
def test_evaluate_requires_forecast_to_match_recorded_cycle_identity(identity, reason):
    cycle = {**_cycle(), **identity}
    row = scoring.evaluate(cycle, _block(), exchange=None)
    assert row["status"] == "invalid"
    assert reason in row["reason"]


def test_evaluate_rejects_forecast_recorded_after_first_outcome_bar():
    late = datetime.fromtimestamp((_DATA_OPEN_TIME + 2 * _HOUR_MS) / 1000, UTC).isoformat()
    row = scoring.evaluate(_cycle(), _block(recorded_at=late), exchange=None)
    assert row["status"] == "invalid"
    assert "first predicted candle" in row["reason"]


def test_evaluate_rejects_recording_before_creation_within_same_millisecond():
    created = datetime.fromtimestamp((_DATA_OPEN_TIME + _HOUR_MS + 1_000) / 1000, UTC)
    row = scoring.evaluate(
        _cycle(),
        _block(created_at=(created + timedelta(microseconds=900)).isoformat(),
               recorded_at=(created + timedelta(microseconds=100)).isoformat()),
        exchange=None,
    )
    assert row["status"] == "invalid"
    assert "recorded" in row["reason"]


def test_evaluate_rejects_creation_before_cutoff_within_same_millisecond():
    cutoff = datetime.fromtimestamp((_DATA_OPEN_TIME + _HOUR_MS + 1_000) / 1000, UTC)
    row = scoring.evaluate(
        _cycle(),
        _block(as_of=(cutoff + timedelta(microseconds=900)).isoformat(),
               created_at=(cutoff + timedelta(microseconds=100)).isoformat()),
        exchange=None,
    )
    assert row["status"] == "invalid"
    assert "creation" in row["reason"]


def test_evaluate_accepts_recording_just_before_first_outcome_bar_closes():
    first_close = datetime.fromtimestamp((_DATA_OPEN_TIME + 2 * _HOUR_MS) / 1000, UTC)
    row = scoring.evaluate(
        _cycle(), _block(recorded_at=(first_close - timedelta(microseconds=1)).isoformat()),
        exchange=None, now=_TARGET_AS_OF - timedelta(seconds=1),
    )
    assert row["status"] == "pending"


def test_evaluate_rejects_median_outside_band():
    row = scoring.evaluate(
        _cycle(), _block(band={"low": 95.0, "p50": 125.0, "high": 120.0}), exchange=None,
    )
    assert row["status"] == "invalid"
    assert "median" in row["reason"]


def test_evaluate_rejects_future_cutoff_and_malformed_shapes():
    future_cutoff = datetime.fromtimestamp((_DATA_OPEN_TIME + 4 * _HOUR_MS) / 1000, UTC).isoformat()
    assert scoring.evaluate(_cycle(), _block(as_of=future_cutoff), exchange=None)["status"] == "invalid"
    assert scoring.evaluate(_cycle(), _block(direction_ratio=[]), exchange=None)["status"] == "invalid"
    assert scoring.evaluate(_cycle(), _block(effective_lookback_bars=-1), exchange=None)["status"] == "invalid"


@pytest.mark.parametrize("field", ["effective_lookback_bars", "horizon_bars"])
def test_invalid_evidence_keeps_out_of_range_integer_out_of_database_columns(field):
    row = scoring.evaluate(_cycle(), _block(**{field: 2 ** 40}), exchange=None)
    assert row["status"] == "invalid"
    assert row[field] is None


@pytest.mark.parametrize(
    "stamp", [
        "9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00",
        "2026-11-16T00:00:00+16:00", "2026-11-16 00:00:00+00:00",
    ],
)
def test_invalid_recorded_timestamp_is_not_evaluated(stamp):
    row = scoring.evaluate(_cycle(), _block(recorded_at=stamp), exchange=None)
    assert row["status"] == "invalid"
    assert "recording timestamp" in row["reason"]


def test_malformed_evidence_has_stable_quarantine_key():
    row = scoring.malformed(_cycle(), "bad block")
    assert row["status"] == "invalid"
    assert row["horizon_key"] == "malformed"
    assert row["warnings"] == ["malformed_forecast_evidence"]
