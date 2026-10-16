import ccxt
import pytest

from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides
from tradingagents.execution import bridge as bridge_module


class _FakeBinance(ccxt.Exchange):
    def __init__(self, options):
        self.apiKey = options["apiKey"]
        self.secret = options["secret"]
        self.urls = {
            "api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
                "fapiPublic": "https://fapi.binance.com/fapi/v1",
                "fapiPrivate": "https://fapi.binance.com/fapi/v1",
            }
        }
        self.demo_enabled = False

    def enable_demo_trading(self, enabled):
        self.demo_enabled = enabled
        self.urls["api"] = {
            key: value.replace("https://api.binance.com", "https://demo-api.binance.com")
            .replace("https://fapi.binance.com", "https://demo-fapi.binance.com")
            for key, value in self.urls["api"].items()
        }


def test_default_binance_venue_is_demo():
    assert DEFAULT_CONFIG["exec_base_url"] == "https://demo-api.binance.com"


def test_demo_url_selects_demo_credentials_and_demo_hosts(monkeypatch):
    captured = {}

    class Fake(_FakeBinance):
        def __init__(self, options):
            captured.update(options)
            super().__init__(options)

    monkeypatch.setattr(ccxt, "binance", Fake)
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "production-key")
    monkeypatch.setenv("BINANCE_SECRET", "production-secret")

    client = bridge_module._default_exchange_factory(
        "binance", base_url="https://demo-api.binance.com"
    )

    assert captured["apiKey"] == "demo-key"
    assert captured["secret"] == "demo-secret"
    assert client.demo_enabled is True
    assert all("demo-" in url for url in client.urls["api"].values())


def test_production_url_selects_production_credentials_without_demo(monkeypatch):
    captured = {}

    class Fake(_FakeBinance):
        def __init__(self, options):
            captured.update(options)
            super().__init__(options)

    monkeypatch.setattr(ccxt, "binance", Fake)
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")
    monkeypatch.setenv("BINANCE_API_KEY", "production-key")
    monkeypatch.setenv("BINANCE_SECRET", "production-secret")

    client = bridge_module._default_exchange_factory(
        "binance", base_url="https://api.binance.com"
    )

    assert captured["apiKey"] == "production-key"
    assert captured["secret"] == "production-secret"
    assert client.demo_enabled is False
    assert all("demo-" not in url for url in client.urls["api"].values())


def test_binance_profile_rejects_unapproved_url_before_client_creation(monkeypatch):
    monkeypatch.setattr(ccxt, "binance", _FakeBinance)

    with pytest.raises(ValueError, match="exec_base_url"):
        bridge_module._default_exchange_factory(
            "binance", base_url="https://evil.example.invalid"
        )


def test_binance_profile_rejects_client_host_mismatch(monkeypatch):
    class Fake(_FakeBinance):
        def enable_demo_trading(self, enabled):
            self.demo_enabled = enabled

    monkeypatch.setattr(ccxt, "binance", Fake)
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")

    with pytest.raises(RuntimeError, match="unexpected API hosts"):
        bridge_module._default_exchange_factory(
            "binance", base_url="https://demo-api.binance.com"
        )


def test_base_url_is_env_overridable(monkeypatch):
    config = dict(DEFAULT_CONFIG)
    monkeypatch.setenv("TRADINGAGENTS_EXEC_BASE_URL", "https://api.binance.com")
    _apply_env_overrides(config)
    assert config["exec_base_url"] == "https://api.binance.com"
