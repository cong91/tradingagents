"""Rate-limit propagation from display-only futures reads."""

from types import SimpleNamespace

import ccxt
import pytest

from server import performance_history_cache, portfolio

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("error", [
    ccxt.RateLimitExceeded("HTTP 429 private-secret"),
    ccxt.DDoSProtection("HTTP 418 private-secret"),
    RuntimeError("HTTP 429 private-secret"),
])
def test_position_rate_limit_starts_shared_cooldown_and_defers_later_reads(monkeypatch, error):
    calls = []
    error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "1200"})

    class Exchange:
        urls = {"api": {
            "fapiPrivate": "https://demo-fapi.binance.com/fapi/v1",
            "fapiPrivateV2": "https://demo-fapi.binance.com/fapi/v2",
            "fapiPrivateV3": "https://demo-fapi.binance.com/fapi/v3",
        }}
        options = {"enableDemoTrading": True, "sandboxMode": False}

        def fetch_balance(self, params=None):
            calls.append("balance")
            return {"info": {"totalMarginBalance": "100", "positions": []}}

        def fapiPrivateV3GetPositionRisk(self, params):
            calls.append("positions")
            raise error

    client = Exchange()
    config = {"exec_live": True, "exec_base_url": "https://demo-api.binance.com"}

    def spot():
        calls.append("spot")
        return SimpleNamespace(cash=0.0, currency="USDT", positions=[])

    bridge = SimpleNamespace(
        exchange_id="binance", config=config, client=lambda: client,
        effective_mode=lambda: "live", sync_portfolio=spot,
    )
    monkeypatch.setattr(portfolio.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: 100.0)

    first = portfolio._get_portfolio(config, bridge=bridge)
    second = portfolio._get_portfolio(config, bridge=bridge)

    assert calls == ["spot", "balance", "positions"]
    assert performance_history_cache.is_rate_limited()
    assert performance_history_cache._RATE_LIMIT_UNTIL == 1300.0
    assert first["futures"]["status"] == "error"
    assert second["futures"]["status"] == "unavailable"
    assert second["positions_source"] == "unavailable"
    assert "private-secret" not in str((first, second))


@pytest.mark.parametrize("code", [418, 429])
def test_spot_rate_limit_defers_futures_within_same_request(monkeypatch, code):
    calls = []
    clock = SimpleNamespace(now=100.0)
    error = RuntimeError(f"HTTP {code} private-secret")
    error.response = SimpleNamespace(status_code=code, headers={"Retry-After": "1200"})

    def spot():
        calls.append("spot")
        raise error

    def futures(_bridge):
        calls.append("futures")
        pytest.fail("no futures request should follow a spot rate limit")

    bridge = SimpleNamespace(
        effective_mode=lambda: "live", sync_portfolio=spot, client=lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(portfolio.audit_log, "read_events", lambda: ([], 0))
    monkeypatch.setattr(portfolio, "read_futures_snapshot", futures)
    monkeypatch.setattr(performance_history_cache, "_RATE_LIMIT_UNTIL", 0.0)
    monkeypatch.setattr(performance_history_cache, "monotonic", lambda: clock.now)

    first = portfolio._get_portfolio({"exec_live": True}, bridge=bridge)
    second = portfolio._get_portfolio({"exec_live": True}, bridge=bridge)

    assert calls == ["spot"]
    assert performance_history_cache._RATE_LIMIT_UNTIL == 1300.0
    assert first["futures"]["status"] == second["futures"]["status"] == "unavailable"
    assert first["positions_source"] == second["positions_source"] == "unavailable"
    assert "private-secret" not in str((first, second))
