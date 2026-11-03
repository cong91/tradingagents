import copy
from collections import UserDict
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace

import ccxt
import pytest

import server.futures_trade_history as history
from server import performance_history_cache
from server.futures_trade_history import read_futures_trade_history

WEEK_MS = 7 * 24 * 60 * 60 * 1000
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


@pytest.fixture(autouse=True)
def _reset_shared_cooldown(monkeypatch):
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)


def _fill(trade_id, timestamp, symbol=BTC, **changes):
    fill = {
        "id": str(trade_id),
        "order": "900",
        "symbol": symbol,
        "side": "buy",
        "timestamp": timestamp,
        "amount": 0.123456789,
        "price": 30000.0,
        "fee": {"cost": 0.12, "currency": "USDT"},
        "info": {
            "symbol": symbol.split(":")[0].replace("/", ""),
            "qty": "0.123456789123456789",
            "price": "30000.00000001",
            "commission": "0.12000000",
            "commissionAsset": "USDT",
            "realizedPnl": "-0.00000003",
        },
    }
    fill.update(changes)
    fill["info"] = {"symbol": symbol.split(":")[0].replace("/", ""),
                    "id": trade_id, "side": "BUY", **fill["info"]}
    return fill


class _Exchange:
    id = "binance"
    enableRateLimit = True

    def __init__(self, handler=None, *, profile="demo", markets=None):
        host = "demo-fapi.binance.com" if profile == "demo" else "fapi.binance.com"
        self.urls = {"api": {"fapiPrivate": f"https://{host}/fapi/v1"}}
        self.calls = []
        self.market_calls = 0
        self.options = {}
        self.handler = handler or (lambda *_: [])
        self.markets = markets if markets is not None else {
            symbol: {"id": symbol.split(":")[0].replace("/", ""), "symbol": symbol,
                     "contract": True, "linear": True, "swap": True}
            for symbol in (BTC, ETH)
        }

    def load_markets(self):
        self.market_calls += 1
        return self.markets

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        self.calls.append((symbol, since, limit, copy.deepcopy(params)))
        return self.handler(symbol, since, limit, params)


class _Bridge:
    exchange_id = "binance"

    def __init__(self, exchange, *, profile="demo"):
        self.exchange = exchange
        host = "demo-api.binance.com" if profile == "demo" else "api.binance.com"
        self.config = {"exec_base_url": f"https://{host}"}
        self.client_calls = 0

    def client(self):
        self.client_calls += 1
        return self.exchange


@pytest.mark.parametrize("profile", ["demo", "production"])
def test_normalizes_raw_fills_and_uses_selected_account_profile(profile):
    fills = [_fill(8, 200, ETH), _fill(11, 100), _fill(10, 100)]
    exchange = _Exchange(lambda symbol, *_: [f for f in fills if f["symbol"] == symbol],
                         profile=profile)
    result = read_futures_trade_history(_Bridge(exchange, profile=profile), [ETH, BTC], 50, 250)

    assert result["status"] == "partial"
    assert result["source"] == "venue_fills"
    assert result["venue_profile"] == profile
    assert result["start_at"] == "1970-01-01T00:00:00.050Z"
    assert result["end_at"] == "1970-01-01T00:00:00.250Z"
    assert result["observed_at"].endswith("Z")
    assert "requested_symbols_only" in result["coverage"]
    assert "starting_basis_unknown" in result["coverage"]
    assert "venue_retention_unknown" in result["coverage"]
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("normalized_history_unverified:")
    assert "missing timestamps" in result["warnings"][0]
    assert [fill["trade_id"] for fill in result["fills"]] == ["10", "11", "8"]
    assert result["fills"][0] == {
        "trade_id": "10",
        "order_id": "900",
        "symbol": BTC,
        "side": "buy",
        "timestamp": "1970-01-01T00:00:00.100Z",
        "contracts": "0.123456789123456789",
        "price": "30000.00000001",
        "fee": "0.12000000",
        "fee_currency": "USDT",
        "realized_pnl": "-0.00000003",
        "realized_pnl_currency": None,
    }
    assert all(call[3] == {
        "type": "future", "paginate": False, "papi": False, "until": 249,
    } for call in exchange.calls)


def test_missing_fee_and_pnl_stay_unknown_and_zero_commission_is_preserved():
    unknown = _fill(1, 10, fee=None, info={"qty": "1", "price": "2"})
    zero = _fill(2, 11, fee={"cost": 0, "currency": "BNB"}, info={})
    exchange = _Exchange(lambda *_: [zero, unknown])

    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)

    assert result["status"] == "partial"
    assert result["fills"][0]["fee"] is None
    assert result["fills"][0]["fee_currency"] is None
    assert result["fills"][0]["realized_pnl"] is None
    assert result["fills"][1]["fee"] == "0"
    assert result["fills"][1]["fee_currency"] == "BNB"
    assert result["fills"][1]["realized_pnl"] is None


def test_half_open_window_filters_and_deduplicates_boundary_records():
    records = [_fill(1, 0), _fill(2, WEEK_MS - 1), _fill(2, WEEK_MS - 1),
               _fill(3, WEEK_MS)]
    exchange = _Exchange(lambda *_: records)

    result = read_futures_trade_history(_Bridge(exchange), [BTC, BTC], 0, WEEK_MS)

    assert result["status"] == "partial"
    assert [fill["trade_id"] for fill in result["fills"]] == ["1", "2"]
    assert [(call[1], call[3]["until"]) for call in exchange.calls] == [(0, WEEK_MS - 1)]


def test_long_request_is_clipped_to_last_seven_days_and_warned():
    exchange = _Exchange()
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, WEEK_MS + 1)

    assert result["status"] == "partial"
    assert result["start_at"] == "1970-01-01T00:00:00.001Z"
    assert result["end_at"] == "1970-01-08T00:00:00.001Z"
    assert any("time_limit_reached" in warning for warning in result["warnings"])
    assert exchange.calls[0][1] == 1
    assert exchange.calls[0][3]["until"] == WEEK_MS


def test_full_initial_page_warns_of_older_and_same_timestamp_omissions_and_stops():
    # Earlier IDs at the same timestamp cannot be recovered by paging forward.
    exchange = _Exchange(lambda *_: [_fill(12, 100), _fill(13, 100)])
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 100, 200, page_limit=2)

    assert result["status"] == "partial"
    assert result["warnings"].count(
        "initial_page_may_be_truncated: BTC/USDT:USDT "
        "[1970-01-01T00:00:00.100Z, 1970-01-01T00:00:00.200Z)"
    ) == 1
    assert "older fills" in result["coverage"]
    assert "same timestamp" in result["coverage"]
    assert [fill["trade_id"] for fill in result["fills"]] == ["12", "13"]
    assert exchange.calls == [(BTC, 100, 2, {
        "type": "future", "paginate": False, "papi": False, "until": 199,
    })]


def test_symbol_limit_reports_partial_and_fetches_only_first_twenty():
    symbols = [f"COIN{i}/USDT:USDT" for i in reversed(range(21))]
    markets = {symbol: {"symbol": symbol, "contract": True, "linear": True, "swap": True}
               for symbol in symbols}
    exchange = _Exchange(markets=markets)
    result = read_futures_trade_history(_Bridge(exchange), [symbols[0], *symbols], 0, 10)

    assert result["status"] == "partial"
    assert len(exchange.calls) == 20
    assert any("symbol_limit_reached" in warning for warning in result["warnings"])
    assert [call[0] for call in exchange.calls] == symbols[:20]
    assert "symbols=" + ",".join(symbols[:20]) in result["coverage"]


def test_full_final_page_cannot_be_assumed_complete():
    exchange = _Exchange(lambda *_: [_fill(1, 10), _fill(2, 10)])
    result = read_futures_trade_history(
        _Bridge(exchange), [BTC], 1, 20, page_limit=2, max_pages_per_symbol=1,
    )

    assert result["status"] == "partial"
    assert len(result["fills"]) == 2
    assert any("initial_page_may_be_truncated" in warning for warning in result["warnings"])
    assert len(exchange.calls) == 1


def test_symbol_and_period_errors_are_partial_sanitized_and_do_not_stop_other_symbols():
    def pages(symbol, _since, _limit, _params):
        if symbol == BTC:
            raise RuntimeError("API_KEY=super-secret https://secret.invalid")
        return [_fill(7, 10, ETH)]

    exchange = _Exchange(pages)
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)

    assert result["status"] == "partial"
    assert [fill["trade_id"] for fill in result["fills"]] == ["7"]
    assert any(warning.startswith("fetch_failed: " + BTC) for warning in result["warnings"])
    assert "secret" not in repr(result)
    assert len(exchange.calls) == 2


@pytest.mark.parametrize("error", [
    ccxt.RateLimitExceeded("API_KEY=super-secret"),
    ccxt.DDoSProtection("API_KEY=super-secret"),
    ccxt.DDoSProtection("HTTP 429 API_KEY=super-secret"),
])
def test_rate_limit_aborts_batch_and_keeps_prior_fills(error):
    sol = "SOL/USDT:USDT"

    def pages(symbol, *_):
        if symbol == BTC:
            raise error
        return [_fill(7, 10, symbol)]

    exchange = _Exchange(pages)
    exchange.markets[sol] = {"id": "SOLUSDT", "symbol": sol, "contract": True,
                             "linear": True, "swap": True}
    result = read_futures_trade_history(_Bridge(exchange), [ETH, BTC, sol], 0, 20)

    assert result["status"] == "partial"
    assert [fill["trade_id"] for fill in result["fills"]] == ["7"]
    assert [call[0] for call in exchange.calls] == [ETH, BTC]
    assert any(warning.startswith("rate_limited: " + BTC) for warning in result["warnings"])
    assert not any("fetch_failed" in warning for warning in result["warnings"])
    assert "secret" not in repr(result)


@pytest.mark.parametrize("header,error_text,expected", [
    ("120", "HTTP 429 API_KEY=super-secret", 120.0),
    ("0", "HTTP 429 API_KEY=super-secret", 0.0),
    ("31536001", "HTTP 429 API_KEY=super-secret", 31536001.0),
    ("bad", "HTTP 429 API_KEY=super-secret", None),
    ("-1", "HTTP 429 API_KEY=super-secret", None),
    ("1.5", "HTTP 429 API_KEY=super-secret", None),
    (None, "binance IP banned until 2000000120000. API_KEY=super-secret", 120.0),
    (None, "binance IP banned until 2000000120000secret", None),
    (None, "binance IP banned until 20000001200000", None),
    ("60", "binance IP banned until 2000000120000. API_KEY=super-secret", 120.0),
])
def test_rate_limit_retry_duration_is_extracted_without_leaking_error(
    monkeypatch, header, error_text, expected,
):
    monkeypatch.setattr(history.time, "time", lambda: 2_000_000_000.0)
    exchange = _Exchange(lambda *_: (_ for _ in ()).throw(ccxt.DDoSProtection(error_text)))
    exchange.last_response_headers = {} if header is None else {"rEtRy-AfTeR": header}
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)
    assert result["status"] == "unavailable"
    assert result["retry_after_seconds"] == expected
    assert len(exchange.calls) == 1
    assert "super-secret" not in repr(result)


@pytest.mark.parametrize("status", [418, 429])
def test_http_status_rate_limit_aborts_symbols_and_starts_shared_cooldown(monkeypatch, status):
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)
    error = RuntimeError("redacted")
    error.response = SimpleNamespace(status_code=status, headers=UserDict({"Retry-After": "1200"}))

    def fail(*_):
        raise error

    exchange = _Exchange(fail)
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)
    assert [call[0] for call in exchange.calls] == [BTC]
    assert result["status"] == "unavailable"
    assert result["retry_after_seconds"] == 1200
    assert any(warning.startswith("rate_limited") for warning in result["warnings"])
    assert performance_history_cache._RATE_LIMIT_UNTIL == 1300


def test_shared_cooldown_started_during_first_symbol_blocks_next_symbol(monkeypatch):
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)

    def first_symbol(symbol, *_):
        performance_history_cache.start_cooldown()
        return [_fill(1, 10, symbol)]

    exchange = _Exchange(first_symbol)
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)
    assert [call[0] for call in exchange.calls] == [BTC]
    assert result["status"] == "partial"
    assert [fill["symbol"] for fill in result["fills"]] == [BTC]
    assert any("cooldown" in warning for warning in result["warnings"])


@pytest.mark.parametrize("stage", ["client", "markets"])
def test_http_status_rate_limit_in_setup_starts_shared_cooldown(monkeypatch, stage):
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)
    error = RuntimeError("redacted")
    error.response = SimpleNamespace(status=429, headers=UserDict({"Retry-After": "1300"}))
    exchange = _Exchange()
    bridge = _Bridge(exchange)

    def fail():
        raise error

    monkeypatch.setattr(bridge if stage == "client" else exchange,
                        "client" if stage == "client" else "load_markets", fail)
    result = read_futures_trade_history(bridge, [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["retry_after_seconds"] == 1300
    assert result["warnings"] == ["rate_limited"]
    assert performance_history_cache._RATE_LIMIT_UNTIL == 1400
    assert exchange.calls == []


def test_cooldown_after_market_load_prevents_first_private_read(monkeypatch):
    exchange = _Exchange()
    original = exchange.load_markets

    def start_during_load():
        markets = original()
        performance_history_cache.start_cooldown()
        return markets

    monkeypatch.setattr(exchange, "load_markets", start_during_load)
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["warnings"] == ["history_rate_limit_cooldown"]
    assert exchange.calls == []


def test_rate_limit_http_date_and_exception_response_headers(monkeypatch):
    monkeypatch.setattr(history.time, "time", lambda: 2_000_000_000.0)
    retry_at = datetime.fromtimestamp(2_000_000_125, UTC)
    error = ccxt.RateLimitExceeded("private-key")
    error.response = SimpleNamespace(headers={"Retry-After": format_datetime(retry_at, usegmt=True)})

    def fail(*_):
        raise error

    result = read_futures_trade_history(_Bridge(_Exchange(fail)), [BTC], 0, 20)
    assert result["retry_after_seconds"] == 125.0
    assert "private-key" not in repr(result)


def test_market_load_rate_limit_exposes_retry_duration():
    exchange = _Exchange()
    exchange.last_response_headers = {"Retry-After": "42"}

    def fail():
        raise ccxt.RateLimitExceeded("private-key")

    exchange.load_markets = fail
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["retry_after_seconds"] == 42.0
    assert result["status"] == "unavailable"
    assert exchange.calls == []
    assert "private-key" not in repr(result)


def test_client_setup_rate_limit_exposes_retry_duration():
    error = ccxt.DDoSProtection("private-key")
    error.response = SimpleNamespace(headers={"Retry-After": "43"})

    class BrokenBridge(_Bridge):
        def client(self):
            raise error

    result = read_futures_trade_history(BrokenBridge(_Exchange()), [BTC], 0, 20)
    assert result["retry_after_seconds"] == 43.0
    assert result["status"] == "unavailable"
    assert "private-key" not in repr(result)


def test_non_rate_errors_never_expose_stale_retry_headers():
    def fail(*_):
        raise RuntimeError("private-key")

    exchange = _Exchange(fail)
    exchange.last_response_headers = {"Retry-After": "3600"}
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["retry_after_seconds"] is None


def test_malformed_response_and_conflicting_trade_ids_never_appear_complete():
    first = _fill(1, 10)
    conflict = _fill(1, 10, price=1, info={"qty": "1", "price": "1"})
    malformed = _fill(2, 11, amount=None, info={"price": "1"})
    exchange = _Exchange(lambda *_: [first, conflict, malformed])

    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)

    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("conflicting_fill" in warning for warning in result["warnings"])
    assert any("invalid_fill" in warning for warning in result["warnings"])


@pytest.mark.parametrize("response", [None, {}, "failed"])
def test_invalid_exchange_page_is_not_treated_as_empty(response):
    exchange = _Exchange(lambda *_: response)
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert result["observed_at"] is None
    assert any("invalid_page" in warning for warning in result["warnings"])


def test_normalized_empty_history_is_partial_because_raw_drops_are_invisible():
    exchange = _Exchange()
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "partial"
    assert result["source"] == "venue_fills"
    assert result["fills"] == []
    assert any(warning.startswith("normalized_history_unverified:") for warning in result["warnings"])
    assert "missing timestamps" in result["warnings"][0]


def _ccxt_exchange():
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.urls["api"]["fapiPrivate"] = "https://demo-fapi.binance.com/fapi/v1"
    exchange.set_markets([{
        "id": "BTCUSDT", "symbol": BTC, "base": "BTC", "quote": "USDT",
        "settle": "USDT", "type": "swap", "contract": True, "linear": True,
        "inverse": False, "swap": True, "future": False, "spot": False, "option": False,
    }])
    return exchange


@pytest.mark.parametrize("response", [None, [{
    "id": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY",
    "qty": "1.000000000000000001", "price": "2.00000001",
}]])
def test_ccxt_normalized_empty_from_null_or_missing_timestamp_remains_partial(response):
    exchange = _ccxt_exchange()
    calls = []

    def fetch(request):
        calls.append(request)
        return response

    exchange.fapiPrivateGetUserTrades = fetch
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert len(calls) == 1
    assert calls[0]["symbol"] == "BTCUSDT"
    assert any("missing timestamps" in warning for warning in result["warnings"])


def test_ccxt_relabels_unknown_raw_symbol_but_reader_rejects_it():
    exchange = _ccxt_exchange()
    raw = {"id": 1, "orderId": 2, "symbol": "UNKNOWNUSDT", "side": "BUY",
           "qty": "1.000000000000000001", "price": "2.00000001", "time": 10}
    assert exchange.parse_trade(raw, exchange.market(BTC))["symbol"] == BTC
    exchange.fapiPrivateGetUserTrades = lambda _: [raw]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])


@pytest.mark.parametrize("changes,missing", [
    ({}, "symbol"),
    ({}, "side"),
    ({"tradeId": None}, "id"),
    ({"symbol": None}, None),
    ({"symbol": "UNKNOWNUSDT"}, None),
    ({"m": True}, None),
    ({"tradeId": 2}, None),
    ({"tradeId": "not-an-id"}, None),
    ({"id": "bad-id", "tradeId": 1}, None),
    ({"id": None, "tradeId": 1}, None),
    ({"side": None, "isBuyer": True}, None),
])
def test_ccxt_malformed_raw_identity_is_omitted(changes, missing):
    exchange = _ccxt_exchange()
    raw = {"id": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY",
           "qty": "1.000000000000000001", "price": "2.00000001", "time": 10}
    raw.update(changes)
    if missing:
        del raw[missing]
    parsed = exchange.parse_trade(raw, exchange.market(BTC))
    assert parsed["symbol"] == BTC
    if changes.get("m"):
        assert parsed["side"] == "sell" and raw["side"] == "BUY"
    exchange.fapiPrivateGetUserTrades = lambda _: [raw]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])


@pytest.mark.parametrize("raw_side", ["BUY", "SELL"])
def test_ccxt_valid_raw_futures_fill_preserves_decimal_strings(raw_side):
    exchange = _ccxt_exchange()
    raw = {"id": 1, "tradeId": "001", "orderId": 2, "symbol": "BTCUSDT", "side": raw_side,
           "qty": "1.000000000000000001", "price": "2.00000001", "time": 10}
    exchange.fapiPrivateGetUserTrades = lambda _: [raw]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert len(result["fills"]) == 1
    assert result["fills"][0]["side"] == raw_side.lower()
    assert result["fills"][0]["contracts"] == raw["qty"]
    assert result["fills"][0]["price"] == raw["price"]
    assert not any("invalid_fill" in warning for warning in result["warnings"])


def test_ccxt_raw_trade_id_without_id_is_accepted():
    exchange = _ccxt_exchange()
    raw = {"tradeId": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY",
           "qty": "1", "price": "2", "time": 10}
    exchange.fapiPrivateGetUserTrades = lambda _: [raw]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert [fill["trade_id"] for fill in result["fills"]] == ["1"]


@pytest.mark.parametrize("raw_value", ["1_000", "\uff11\uff12", "1__0", "1,000", "1 000"])
@pytest.mark.parametrize("field", ["qty", "price", "commission", "realizedPnl"])
def test_malformed_decimal_syntax_is_rejected(field, raw_value):
    fill = _fill(1, 10)
    fill["info"][field] = raw_value
    result = read_futures_trade_history(_Bridge(_Exchange(lambda *_: [fill])), [BTC], 0, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])


@pytest.mark.parametrize("field", ["qty", "price", "commission", "realizedPnl"])
def test_ccxt_parser_rejects_raw_decimal_with_underscores(field):
    exchange = _ccxt_exchange()
    raw = {"id": 1, "orderId": 2, "symbol": "BTCUSDT", "side": "BUY",
           "qty": "1", "price": "2", "commission": "0.1", "commissionAsset": "USDT",
           "realizedPnl": "0", "time": 10}
    raw[field] = "1_000"
    exchange.fapiPrivateGetUserTrades = lambda _: [raw]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 1, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])


@pytest.mark.parametrize("decimal", ["1e-9", "+1.20", ".5", "1."])
def test_valid_decimal_syntax_is_preserved(decimal):
    fill = _fill(1, 10)
    fill["info"]["qty"] = decimal
    result = read_futures_trade_history(_Bridge(_Exchange(lambda *_: [fill])), [BTC], 0, 20)
    assert result["fills"][0]["contracts"] == decimal


def test_mismatched_raw_symbol_is_omitted_even_if_ccxt_labels_it_as_requested():
    fill = _fill(1, 10)
    fill["info"]["symbol"] = "UNKNOWNUSDT"
    exchange = _Exchange(lambda *_: [fill])
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])
    assert any("normalized_history_unverified" in warning for warning in result["warnings"])


def test_raw_symbol_requires_a_validated_market_id():
    exchange = _Exchange(lambda *_: [_fill(1, 10)])
    del exchange.markets[BTC]["id"]
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])


def test_spot_inverse_and_unknown_markets_are_not_queried():
    markets = {
        BTC: {"symbol": BTC, "contract": True, "linear": False, "swap": True},
        ETH: {"symbol": ETH, "contract": False, "linear": True, "swap": True},
    }
    exchange = _Exchange(markets=markets)
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert sum("unsupported_symbol" in warning for warning in result["warnings"]) == 2
    assert exchange.calls == []


def test_non_binance_and_mismatched_profiles_do_not_fetch_or_switch_accounts():
    exchange = _Exchange(profile="production")
    bridge = _Bridge(exchange, profile="demo")
    result = read_futures_trade_history(bridge, [BTC], 0, 20)
    assert result["status"] == "error"
    assert any("profile_mismatch" in warning for warning in result["warnings"])
    assert exchange.calls == []

    bridge.exchange_id = "bybit"
    bridge.client_calls = 0
    result = read_futures_trade_history(bridge, [BTC], 0, 20)
    assert result["status"] == "unsupported"
    assert bridge.client_calls == 0


@pytest.mark.parametrize("options", [
    {"portfolioMargin": True},
    {"portfolioMargin": "true"},
    {"papi": True},
    {"defaultPortfolioMargin": True},
    {"defaultPapi": True},
    {"fetchMyTrades": {"portfolioMargin": True}},
    {"fetchMyTrades": {"papi": True}},
])
def test_portfolio_margin_configuration_is_refused_before_market_or_trade_calls(options):
    exchange = _Exchange()
    exchange.options = copy.deepcopy(options)
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "unsupported"
    assert result["source"] == "unavailable"
    assert "portfolio_margin_unsupported" in result["warnings"]
    assert exchange.options == options
    assert exchange.market_calls == 0
    assert exchange.calls == []


def test_unknown_profile_and_disabled_rate_limiter_fail_closed():
    exchange = _Exchange()
    bridge = _Bridge(exchange)
    bridge.config = {"exec_base_url": "https://unknown.invalid"}
    result = read_futures_trade_history(bridge, [BTC], 0, 20)
    assert result["status"] == "error"
    assert result["venue_profile"] is None
    assert any("unknown_profile" in warning for warning in result["warnings"])
    assert bridge.client_calls == 0

    bridge = _Bridge(exchange)
    exchange.enableRateLimit = False
    result = read_futures_trade_history(bridge, [BTC], 0, 20)
    assert result["status"] == "error"
    assert any("rate_limit_disabled" in warning for warning in result["warnings"])
    assert exchange.calls == []


@pytest.mark.parametrize("error", [
    RuntimeError("my-secret-key"),
    ccxt.RateLimitExceeded("my-secret-key"),
    ccxt.DDoSProtection("my-secret-key"),
])
def test_all_trade_fetches_failed_are_unavailable_and_sanitized(error):
    def fail(*_):
        raise error

    exchange = _Exchange(fail)
    result = read_futures_trade_history(_Bridge(exchange), [BTC, ETH], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert result["observed_at"] is None
    assert result["fills"] == []
    assert "my-secret-key" not in repr(result)
    if isinstance(error, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        assert len(exchange.calls) == 1
        assert any("rate_limited" in warning for warning in result["warnings"])
    else:
        assert len(exchange.calls) == 2
        assert sum("fetch_failed" in warning for warning in result["warnings"]) == 2


def test_client_setup_errors_are_sanitized():
    class BrokenBridge(_Bridge):
        def client(self):
            raise RuntimeError("my-secret-key")

    result = read_futures_trade_history(BrokenBridge(_Exchange()), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert any("client_unavailable" in warning for warning in result["warnings"])
    assert "my-secret-key" not in repr(result)


@pytest.mark.parametrize("error", [ccxt.RateLimitExceeded("my-secret-key"),
                                   ccxt.DDoSProtection("my-secret-key")])
def test_client_setup_rate_limit_is_distinct_and_sanitized(error):
    class BrokenBridge(_Bridge):
        def client(self):
            raise error

    result = read_futures_trade_history(BrokenBridge(_Exchange()), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert result["observed_at"] is None
    assert "rate_limited" in result["warnings"]
    assert "my-secret-key" not in repr(result)


def test_deadline_stops_before_more_pages_or_symbols(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(history.time, "monotonic", lambda: clock[0])

    def slow_page(*_):
        clock[0] = 6.0
        return [_fill(1, 10), _fill(2, 10)]

    exchange = _Exchange(slow_page)
    result = read_futures_trade_history(
        _Bridge(exchange), [BTC, ETH], 0, 20, page_limit=2, max_total_seconds=5,
    )

    assert result["status"] == "partial"
    assert len(result["fills"]) == 2
    assert len(exchange.calls) == 1
    assert any("deadline_reached" in warning for warning in result["warnings"])


def test_malformed_currency_is_not_silently_discarded():
    fill = _fill(1, 10, info={"qty": "1", "price": "2", "commission": "0.1",
                              "commissionAsset": {"secret": "bad"}})
    result = read_futures_trade_history(_Bridge(_Exchange(lambda *_: [fill])), [BTC], 0, 20)
    assert result["status"] == "partial"
    assert result["fills"] == []
    assert any("invalid_fill" in warning for warning in result["warnings"])
    assert "secret" not in repr(result)


def test_realized_pnl_currency_only_comes_from_venue_margin_asset():
    info = {"qty": "1", "price": "2", "realizedPnl": "0", "marginAsset": "USDT"}
    result = read_futures_trade_history(
        _Bridge(_Exchange(lambda *_: [_fill(1, 10, info=info)])), [BTC], 0, 20,
    )
    assert result["fills"][0]["realized_pnl"] == "0"
    assert result["fills"][0]["realized_pnl_currency"] == "USDT"


def test_endpoint_credentials_and_nonstandard_ports_are_rejected():
    exchange = _Exchange()
    exchange.urls["api"]["fapiPrivate"] = "https://secret@demo-fapi.binance.com:444/fapi/v1"
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "error"
    assert any("profile_mismatch" in warning for warning in result["warnings"])
    assert "secret" not in repr(result)
    assert exchange.calls == []


def test_client_is_not_started_after_deadline(monkeypatch):
    clock = iter([0.0, 6.0])
    monkeypatch.setattr(history.time, "monotonic", lambda: next(clock))
    bridge = _Bridge(_Exchange())
    result = read_futures_trade_history(bridge, [BTC], 0, 20, max_total_seconds=5)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert result["observed_at"] is None
    assert bridge.client_calls == 0
    assert any("deadline_reached" in warning for warning in result["warnings"])


def test_market_load_failure_is_sanitized():
    exchange = _Exchange()

    def fail():
        raise RuntimeError("private-key")

    exchange.load_markets = fail
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert any("markets_unavailable" in warning for warning in result["warnings"])
    assert "private-key" not in repr(result)


@pytest.mark.parametrize("error", [ccxt.RateLimitExceeded("private-key"),
                                   ccxt.DDoSProtection("private-key")])
def test_market_load_rate_limit_is_distinct_and_sanitized(error):
    exchange = _Exchange()

    def fail():
        raise error

    exchange.load_markets = fail
    result = read_futures_trade_history(_Bridge(exchange), [BTC], 0, 20)
    assert result["status"] == "unavailable"
    assert result["source"] == "unavailable"
    assert "rate_limited" in result["warnings"]
    assert "private-key" not in repr(result)
    assert exchange.calls == []


@pytest.mark.parametrize("symbols,since,until,kwargs", [
    ([], 0, 1, {}),
    (BTC, 0, 1, {}),
    (["BTC/USDT"], 0, 1, {}),
    ([BTC], -1, 1, {}),
    ([BTC], 1, 1, {}),
    ([BTC], True, 10, {}),
    ([BTC], 0, 10, {"page_limit": 0}),
    ([BTC], 0, 10, {"page_limit": 1001}),
    ([BTC], 0, 10, {"max_pages_per_symbol": 0}),
    ([BTC], 0, 10, {"max_pages_per_symbol": 21}),
    ([BTC], 0, 10, {"max_total_seconds": 0}),
    ([BTC], 0, 10, {"max_total_seconds": float("inf")}),
])
def test_invalid_arguments_fail_before_any_client_access(symbols, since, until, kwargs):
    bridge = _Bridge(_Exchange())
    with pytest.raises(ValueError):
        read_futures_trade_history(bridge, symbols, since, until, **kwargs)
    assert bridge.client_calls == 0
