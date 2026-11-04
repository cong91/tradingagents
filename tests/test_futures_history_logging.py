"""Private trade-history requests must not enable CCXT request/response logging."""

import logging
import secrets
from types import SimpleNamespace

import ccxt
import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

from server.futures_trade_history import read_futures_trade_history

pytestmark = pytest.mark.unit

BTC = "BTC/USDT:USDT"


class Exchange:
    id = "binance"
    enableRateLimit = True
    verbose = False
    urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}
    options = {}

    def __init__(self):
        self.calls = []
        self.logger = logging.getLogger("test.private_history")
        self.logger.setLevel(logging.INFO)

    def load_markets(self):
        self.calls.append("markets")
        return {BTC: {"id": "BTCUSDT", "symbol": BTC, "contract": True,
                      "linear": True, "swap": True}}

    def fetch_my_trades(self, *args):
        self.calls.append("private")
        return []


def bridge(client):
    return SimpleNamespace(
        exchange_id="binance", config={"exec_base_url": "https://demo-api.binance.com"},
        client=lambda: client,
    )


@pytest.mark.parametrize("mode", ["verbose", "debug", "invalid_logger"])
def test_logging_configuration_is_rejected_before_private_reads(mode):
    exchange = Exchange()
    if mode == "verbose":
        exchange.verbose = True
    elif mode == "debug":
        exchange.logger.setLevel(logging.DEBUG)
    else:
        exchange.logger = object()

    result = read_futures_trade_history(bridge(exchange), [BTC], 1, 10)

    assert result["status"] == "error"
    assert result["source"] == "unavailable"
    assert result["warnings"] == ["private_logging_enabled"]
    assert exchange.calls == []


def test_history_redirect_is_refused_without_mutating_shared_transport(monkeypatch):
    sent = []
    credential = secrets.token_hex(16)

    class RedirectAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            sent.append((request.url, request.headers.get("X-MBX-APIKEY")))
            response = requests.Response()
            response.status_code = 307 if len(sent) == 1 else 200
            response.headers = CaseInsensitiveDict({"Location": "https://other.example/collect"})
            response._content = b"[]"
            response.url = request.url
            response.request = request
            return response

    exchange = ccxt.binance({"apiKey": credential, "secret": secrets.token_hex(16)})
    exchange.enable_demo_trading(True)
    exchange.set_markets([{
        "id": "BTCUSDT", "symbol": BTC, "base": "BTC", "quote": "USDT", "settle": "USDT",
        "type": "swap", "spot": False, "swap": True, "future": False,
        "contract": True, "linear": True, "inverse": False, "contractSize": 1,
    }])
    original_session = exchange.session
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: RedirectAdapter())

    result = read_futures_trade_history(bridge(exchange), [BTC], 1, 10)

    assert result["status"] == "unavailable"
    assert len(sent) == 1
    assert sent[0][1] == credential
    assert exchange.session is original_session
    assert exchange.session.max_redirects == requests.sessions.DEFAULT_REDIRECT_LIMIT


def test_successful_private_read_uses_isolated_session_and_preserves_client(monkeypatch):
    sent = []
    credential = secrets.token_hex(16)

    class SuccessAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            sent.append((request.url, request.headers.get("X-MBX-APIKEY")))
            response = requests.Response()
            response.status_code = 200
            response.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
            response._content = b"[]"
            response.url = request.url
            response.request = request
            return response

    exchange = ccxt.binance({"apiKey": credential, "secret": secrets.token_hex(16)})
    exchange.enable_demo_trading(True)
    exchange.set_markets([{
        "id": "BTCUSDT", "symbol": BTC, "base": "BTC", "quote": "USDT", "settle": "USDT",
        "type": "swap", "spot": False, "swap": True, "future": False,
        "contract": True, "linear": True, "inverse": False, "contractSize": 1,
    }])
    original_session = exchange.session
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: SuccessAdapter())
    result = read_futures_trade_history(bridge(exchange), [BTC], 1, 10)
    assert result["status"] == "partial"
    assert len(sent) == 1 and sent[0][1] == credential
    assert exchange.session is original_session
    assert exchange.session.max_redirects == requests.sessions.DEFAULT_REDIRECT_LIMIT


def test_logging_enabled_while_loading_markets_prevents_private_read():
    exchange = Exchange()
    load_markets = exchange.load_markets

    def enable_verbose():
        markets = load_markets()
        exchange.verbose = True
        return markets

    exchange.load_markets = enable_verbose
    result = read_futures_trade_history(bridge(exchange), [BTC], 1, 10)

    assert result["status"] == "unavailable"
    assert result["warnings"] == ["private_logging_enabled"]
    assert exchange.calls == ["markets"]
