"""Offline regression coverage for the Binance USD-M display reader."""

import json
import logging
import secrets
from contextlib import suppress
from datetime import UTC, datetime

import ccxt
import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

import server.futures_snapshot as snapshot_module
from server import performance_history_cache
from server.futures_snapshot import read_futures_snapshot

pytestmark = pytest.mark.unit


def _market(market_id="BTCUSDT", symbol="BTC/USDT:USDT", contract_size=1):
    return {
        "id": market_id, "symbol": symbol, "base": symbol.split("/")[0],
        "quote": "USDT", "settle": "USDT", "settleId": "USDT", "type": "swap",
        "spot": False, "swap": True, "future": False, "contract": True,
        "linear": True, "inverse": False, "contractSize": contract_size,
    }


def _row(**changes):
    return {
        "symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1",
        "entryPrice": "0", "markPrice": "100", "unRealizedProfit": "2.5",
        "liquidationPrice": "55", "notional": "100", "marginAsset": "USDT",
        "initialMargin": "15", "positionInitialMargin": "10", "updateTime": 123,
        **changes,
    }


class _Bridge:
    exchange_id = "binance"

    def __init__(self, client, profile="demo"):
        self.exchange = client
        self.config = {
            "exec_base_url": f"https://{'demo-api' if profile == 'demo' else 'api'}.binance.com",
        }

    def client(self):
        return self.exchange


def _exchange(monkeypatch, rows=None, *, profile="demo", markets=None):
    client = ccxt.binance()
    if profile == "demo":
        client.enable_demo_trading(True)
    client.set_markets(markets if markets is not None else [_market()])
    calls = []

    def balance(params=None):
        calls.append(("balance", params))
        return {"info": {"totalMarginBalance": "100", "positions": []}}

    def raw_positions(params):
        calls.append(("risk", params))
        if isinstance(rows, Exception):
            raise rows
        return [] if rows is None else rows

    def refuse(*args, **kwargs):
        pytest.fail("display reader must not place or cancel orders")

    monkeypatch.setattr(client, "fetch_balance", balance)
    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", raw_positions)
    monkeypatch.setattr(client, "fetch_positions", refuse)
    monkeypatch.setattr(client, "create_order", refuse)
    monkeypatch.setattr(client, "cancel_order", refuse)
    return client, calls


def _issues(snapshot):
    return [issue["reason"] for issue in snapshot["positions_read"]["issues"]]


def test_zero_entry_price_position_survives_real_ccxt_filter(monkeypatch):
    client, calls = _exchange(monkeypatch, [_row()])
    monkeypatch.setattr(client, "load_leverage_brackets", lambda *args: None)
    assert ccxt.binance.fetch_positions_risk(client, None, {"type": "future"}) == []
    calls.clear()

    snapshot = read_futures_snapshot(_Bridge(client))

    assert calls == [
        ("balance", {"type": "future", "subType": "linear"}), ("risk", {}),
    ]
    assert snapshot["positions_read"]["status"] == "partial"
    assert _issues(snapshot) == ["scope_unverified"]
    assert snapshot["positions_read"]["skipped"] == 0
    assert snapshot["positions"] == [{
        "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0, "quantity": 1.0,
        "entry_price": 0.0, "mark_price": 100.0, "liquidation_price": 55.0,
        "unrealized_pnl": 2.5, "notional": 100.0, "margin": 10.0,
        "leverage": None, "margin_mode": None, "price_currency": "USDT",
        "notional_currency": "USDT", "margin_currency": "USDT",
        "unrealized_pnl_currency": "USDT",
    }]


@pytest.mark.parametrize("profile", ["demo", "production"])
def test_matching_profile_reads_v3_without_symbol_filter(monkeypatch, profile):
    client, calls = _exchange(monkeypatch, [_row()], profile=profile)
    result = read_futures_snapshot(_Bridge(client, profile))
    assert calls[-1] == ("risk", {})
    assert result["venue_profile"] == profile
    assert result["positions_read"]["status"] == "partial"


@pytest.mark.parametrize("bad_url", [
    "https://fapi.binance.com/fapi/v3", "http://demo-fapi.binance.com/fapi/v3",
    "https://demo-fapi.binance.com:444/fapi/v3", "https://name@demo-fapi.binance.com/fapi/v3",
    "https://demo-fapi.binance.com/fapi/v2", "https://demo-fapi.binance.com/fapi/v3?q=secret",
])
def test_bad_v3_route_never_sends_private_request(monkeypatch, bad_url):
    client, calls = _exchange(monkeypatch)
    client.urls["api"]["fapiPrivateV3"] = bad_url
    with pytest.raises(ValueError, match="private routes") as exc:
        read_futures_snapshot(_Bridge(client))
    assert calls == []
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("bad_mode", [
    {"enableDemoTrading": False}, {"sandboxMode": True}, {"portfolioMargin": True},
    {"defaultPapi": True}, {"defaultPortfolioMargin": True},
    {"fetchBalance": {"papi": True}}, {"fetchPositionsRisk": {"portfolioMargin": True}},
    {"fetchBalance": {"defaultPapi": True}},
    {"fetchPositionsRisk": {"defaultPortfolioMargin": True}},
])
def test_wrong_or_ambiguous_account_mode_never_reads(monkeypatch, bad_mode):
    client, calls = _exchange(monkeypatch)
    client.options.update(bad_mode)
    with pytest.raises(ValueError):
        read_futures_snapshot(_Bridge(client))
    assert calls == []


def test_snapshot_private_redirect_does_not_send_api_key_to_another_host(monkeypatch):
    credential = secrets.token_hex(16)
    sent = []

    class RedirectAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            sent.append((request.url, request.headers.get("X-MBX-APIKEY")))
            response = requests.Response()
            response.status_code = 302 if len(sent) == 1 else 200
            response.headers = CaseInsensitiveDict({"Location": "https://other.example/collect"})
            response._content = b"{}"
            response.url = request.url
            response.request = request
            return response

    client, _ = _exchange(monkeypatch)
    client.apiKey = credential
    client.secret = secrets.token_hex(16)
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: RedirectAdapter())
    original_session = client.session
    monkeypatch.delattr(client, "fetch_balance")
    with suppress(Exception):
        read_futures_snapshot(_Bridge(client))
    assert len(sent) == 1
    assert sent[0][1] == credential
    assert client.session is original_session
    assert client.session.max_redirects == requests.sessions.DEFAULT_REDIRECT_LIMIT


@pytest.mark.parametrize("status", [418, 429])
def test_signed_http_rate_limit_starts_cooldown_before_retry(monkeypatch, status):
    client, _ = _exchange(monkeypatch)
    client.apiKey = secrets.token_hex(16)
    client.secret = secrets.token_hex(16)
    client.options["maxRetriesOnFailure"] = 2
    sent = []
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)

    class RateLimitAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            sent.append(request.url)
            response = requests.Response()
            response.status_code = status
            response.headers = CaseInsensitiveDict({"Retry-After": "1200"})
            response._content = b"{}"
            response.url = request.url
            response.request = request
            return response

    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: RateLimitAdapter())
    monkeypatch.delattr(client, "fetch_balance")
    with pytest.raises(RuntimeError, match="rate limited"):
        read_futures_snapshot(_Bridge(client))
    assert len(sent) == 1
    assert performance_history_cache._RATE_LIMIT_UNTIL == 1300.0


def test_snapshot_rechecks_cooldown_after_balance(monkeypatch):
    client, calls = _exchange(monkeypatch)
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)

    def balance(params=None):
        calls.append(("balance", params))
        performance_history_cache.start_cooldown()
        return {"info": {"totalMarginBalance": "100", "positions": []}}

    monkeypatch.setattr(client, "fetch_balance", balance)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert snapshot["status"] == "ok"
    assert snapshot["equity"] == 100
    assert snapshot["positions_read"]["issues"] == [{
        "symbol": None, "reason": "rate_limit_cooldown",
    }]
    assert snapshot["positions_read"]["observed_at"] is None
    assert [name for name, *_ in calls] == ["balance"]


def test_unverified_account_envelope_fails_before_position_read(monkeypatch):
    client, calls = _exchange(monkeypatch)

    def balance(params=None):
        calls.append(("balance", params))
        return {"info": {"totalMarginBalance": "100"}}

    monkeypatch.setattr(client, "fetch_balance", balance)
    with pytest.raises(ValueError, match="account type"):
        read_futures_snapshot(_Bridge(client))
    assert [name for name, *_ in calls] == ["balance"]


def test_route_change_during_balance_prevents_second_private_request(monkeypatch):
    client, calls = _exchange(monkeypatch)

    def balance(params=None):
        calls.append(("balance", params))
        client.urls["api"]["fapiPrivateV3"] = "https://fapi.binance.com/fapi/v3"
        return {"info": {"totalMarginBalance": "100", "positions": []}}

    monkeypatch.setattr(client, "fetch_balance", balance)
    with pytest.raises(ValueError, match="private routes"):
        read_futures_snapshot(_Bridge(client))
    assert [name for name, *_ in calls] == ["balance"]


@pytest.mark.parametrize(("rows", "expected", "skipped"), [
    ([], ["scope_unverified"], 0),
    ([_row(positionAmt="0", notional="0")], ["scope_unverified"], 0),
    ([_row(positionAmt=None)], ["scope_unverified", "missing_size"], 1),
    ([_row(positionAmt="NaN")], ["scope_unverified", "invalid_size"], 1),
    ([_row(positionAmt="1e-400")], ["scope_unverified", "invalid_size"], 1),
    ([_row(positionAmt="1_0")], ["scope_unverified", "invalid_size"], 1),
    ([_row(positionAmt="0", notional="1_0")], ["scope_unverified", "invalid_notional"], 1),
    ([_row(positionAmt="0", notional="4")], ["scope_unverified", "inconsistent_notional"], 1),
    (["private-secret"], ["scope_unverified", "invalid_row"], 1),
    ([_row(symbol="private-secret")], ["scope_unverified", "unknown_market"], 1),
    ([_row(positionSide="private-secret")], ["scope_unverified", "invalid_side"], 1),
    ([_row(nextPageCursor="private-secret")], ["scope_unverified", "invalid_row"], 1),
    ([_row(marginAsset="private-secret")], ["scope_unverified", "invalid_margin_currency"], 1),
    ([_row(positionSide="SHORT", positionAmt="1")], ["scope_unverified", "inconsistent_side"], 1),
    ([_row(), _row()], ["scope_unverified", "duplicate_position", "duplicate_position"], 2),
    ([_row(), _row(positionSide="LONG")],
     ["scope_unverified", "inconsistent_position_mode", "inconsistent_position_mode"], 2),
])
def test_raw_rows_are_classified_without_leaking_payload(monkeypatch, rows, expected, skipped):
    client, _ = _exchange(monkeypatch, rows)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert snapshot["positions"] == []
    assert _issues(snapshot) == expected
    assert snapshot["positions_read"]["skipped"] == skipped
    assert "private-secret" not in json.dumps(snapshot)


def test_net_and_two_hedge_legs_are_independent(monkeypatch):
    markets = [_market(), _market("ETHUSDT", "ETH/USDT:USDT", 0.1)]
    rows = [
        _row(positionSide="LONG", positionAmt="2", notional="200"),
        _row(positionSide="SHORT", positionAmt="-3", notional="-300"),
        _row(symbol="ETHUSDT", positionAmt="-4", notional="-400"),
    ]
    client, _ = _exchange(monkeypatch, rows, markets=markets)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert [(p["symbol"], p["side"], p["contracts"], p["quantity"]) for p in snapshot["positions"]] == [
        ("BTC/USDT:USDT", "long", 2.0, 2.0),
        ("BTC/USDT:USDT", "short", 3.0, -3.0),
        ("ETH/USDT:USDT", "short", 4.0, -0.4),
    ]


@pytest.mark.parametrize("size", [None, 0, -1, True, "NaN", "1_0"])
def test_unknown_contract_size_preserves_position_without_inventing_quantity(monkeypatch, size):
    client, _ = _exchange(monkeypatch, [_row()], markets=[_market(contract_size=size)])
    position = read_futures_snapshot(_Bridge(client))["positions"][0]
    assert position["contracts"] == 1
    assert position["quantity"] is None


def test_invalid_optional_fields_remain_unknown(monkeypatch):
    client, _ = _exchange(monkeypatch, [_row(
        entryPrice="NaN", markPrice=True, unRealizedProfit="NaN", liquidationPrice="bad",
        notional="1_0", positionInitialMargin="NaN", leverage=True,
    )])
    position = read_futures_snapshot(_Bridge(client))["positions"][0]
    assert all(position[key] is None for key in (
        "entry_price", "mark_price", "unrealized_pnl", "liquidation_price", "notional", "margin", "leverage",
    ))


def test_endpoint_error_is_sanitized_and_has_no_observation_time(monkeypatch):
    client, _ = _exchange(monkeypatch, RuntimeError("private-secret"))
    snapshot = read_futures_snapshot(_Bridge(client))
    assert _issues(snapshot) == ["read_failed"]
    assert snapshot["positions_read"]["observed_at"] is None
    assert snapshot["positions_read"]["skipped"] is None
    assert "private-secret" not in json.dumps(snapshot)


def test_observation_time_captured_immediately_after_endpoint(monkeypatch):
    client, calls = _exchange(monkeypatch, [_row()])
    observed = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    class Clock:
        @classmethod
        def now(cls, tz):
            calls.append(("clock", tz))
            return observed

    monkeypatch.setattr(snapshot_module, "datetime", Clock)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert calls[-2:] == [("risk", {}), ("clock", UTC)]
    assert snapshot["positions_read"]["observed_at"] == observed.isoformat()


@pytest.mark.parametrize("response", [{"private": "secret"}, None, "private-secret"])
def test_invalid_endpoint_envelope_is_not_a_complete_empty_result(monkeypatch, response):
    client, _ = _exchange(monkeypatch)
    monkeypatch.setattr(client, "fapiPrivateV3GetPositionRisk", lambda params: response)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert snapshot["positions"] == []
    assert _issues(snapshot) == ["invalid_response"]
    assert snapshot["positions_read"]["skipped"] is None
    assert datetime.fromisoformat(snapshot["positions_read"]["observed_at"]).utcoffset() == UTC.utcoffset(None)
    assert "secret" not in json.dumps(snapshot)


def test_portfolio_margin_balance_does_not_use_regular_usdm_endpoint(monkeypatch):
    client, calls = _exchange(monkeypatch, [_row()])

    def portfolio_balance(params=None):
        calls.append(("balance", params))
        return {"info": [{"asset": "USDT", "umWalletBalance": "2"}]}

    monkeypatch.setattr(client, "fetch_balance", portfolio_balance)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert [name for name, *_ in calls] == ["balance"]
    assert _issues(snapshot) == ["account_type_unverified"]
    assert snapshot["positions_read"]["observed_at"] is None
    assert snapshot["equity"] is None


@pytest.mark.parametrize("market_change", [
    {"linear": False}, {"inverse": True}, {"contract": False},
    {"symbol": None}, {"quote": None}, {"settle": None}, {"settleId": {"private": "secret"}},
])
def test_invalid_market_metadata_is_not_used_to_map_a_raw_symbol(monkeypatch, market_change):
    client, _ = _exchange(monkeypatch, [_row()])
    client.markets_by_id["BTCUSDT"][0].update(market_change)
    snapshot = read_futures_snapshot(_Bridge(client))
    assert snapshot["positions"] == []
    assert _issues(snapshot) == ["scope_unverified", "unknown_market"]


def test_ambiguous_market_id_is_not_guessed(monkeypatch):
    client, _ = _exchange(monkeypatch, [_row()])
    client.markets_by_id["BTCUSDT"].append(dict(client.markets_by_id["BTCUSDT"][0]))
    snapshot = read_futures_snapshot(_Bridge(client))
    assert snapshot["positions"] == []
    assert _issues(snapshot) == ["scope_unverified", "unknown_market"]


def test_missing_position_margin_does_not_use_total_initial_margin(monkeypatch):
    row = _row()
    row.pop("positionInitialMargin")
    client, _ = _exchange(monkeypatch, [row])
    assert read_futures_snapshot(_Bridge(client))["positions"][0]["margin"] is None


def test_wrong_production_profile_never_contacts_demo(monkeypatch):
    client, calls = _exchange(monkeypatch, profile="demo")
    with pytest.raises(ValueError):
        read_futures_snapshot(_Bridge(client, profile="production"))
    assert calls == []


@pytest.mark.parametrize("bad_base", [
    "https://demo-api.binance.com:444", "https://demo-api.binance.com/path",
    "https://user@demo-api.binance.com", "https://demo-api.binance.com?secret=1",
])
def test_untrusted_configured_host_never_sends_private_request(monkeypatch, bad_base):
    client, calls = _exchange(monkeypatch)
    bridge = _Bridge(client)
    bridge.config["exec_base_url"] = bad_base
    with pytest.raises(ValueError) as exc:
        read_futures_snapshot(bridge)
    assert calls == []
    assert "secret" not in str(exc.value)


def test_ccxt_verbose_mode_is_refused_before_private_reads(monkeypatch):
    client, calls = _exchange(monkeypatch)
    client.verbose = True
    with pytest.raises(ValueError):
        read_futures_snapshot(_Bridge(client))
    assert calls == []


def test_ccxt_debug_logging_is_refused_before_private_reads(monkeypatch):
    client, calls = _exchange(monkeypatch)
    logger = logging.getLogger("test.binance.private")
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    client.logger = logger
    with pytest.raises(ValueError, match="debug logging"):
        read_futures_snapshot(_Bridge(client))
    assert calls == []
