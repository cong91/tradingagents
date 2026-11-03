import ccxt
import pytest

pytestmark = pytest.mark.unit


def test_default_factory_reads_production_credentials_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "env-key")
    monkeypatch.setenv("BINANCE_SECRET", "env-secret")
    import tradingagents.execution.exchange_client as exchange_client_module

    captured = {}

    class FakeBinance(ccxt.Exchange):
        def __init__(self, options):
            captured.update(options)
            self.apiKey = options["apiKey"]
            self.secret = options["secret"]
            self.urls = {"api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
                "fapiPublic": "https://fapi.binance.com/fapi/v1",
                "fapiPrivate": "https://fapi.binance.com/fapi/v1",
            }}

    monkeypatch.setattr(ccxt, "binance", FakeBinance)
    exchange_client_module._default_exchange_factory(
        "binance", base_url="https://api.binance.com"
    )
    assert captured == {
        "apiKey": "env-key",
        "secret": "env-secret",
        "enableRateLimit": True,
        # Binance needs ccxt's measured clock offset: a host ~1s fast fails
        # every signed request with -1021 (test_binance_venue_config pins this).
        "options": {"adjustForTimeDifference": True},
    }


def test_default_factory_demo_uses_demo_credentials(monkeypatch):
    import tradingagents.execution.exchange_client as exchange_client_module

    captured = {}

    class FakeBinance(ccxt.Exchange):
        def __init__(self, options):
            captured.update(options)
            self.urls = {"api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
                "fapiPublic": "https://fapi.binance.com/fapi/v1",
                "fapiPrivate": "https://fapi.binance.com/fapi/v1",
            }}

        def enable_demo_trading(self, enabled):
            self.urls["api"] = {
                key: value.replace("https://api.binance.com", "https://demo-api.binance.com")
                .replace("https://fapi.binance.com", "https://demo-fapi.binance.com")
                for key, value in self.urls["api"].items()
            }

    monkeypatch.setenv("BINANCE_API_KEY", "wrong-key")
    monkeypatch.setenv("BINANCE_SECRET", "wrong-secret")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")
    monkeypatch.setattr(ccxt, "binance", FakeBinance)

    exchange_client_module._default_exchange_factory("binance", sandbox=True)

    assert captured["apiKey"] == "demo-key"
    assert captured["secret"] == "demo-secret"


def test_default_factory_demo_routes_to_demo_api(monkeypatch):
    """Sandbox binance must arm Demo Trading (its own demo endpoint), never
    fall back to Spot Testnet."""
    import tradingagents.execution.exchange_client as exchange_client_module

    calls = []

    class FakeBinance(ccxt.Exchange):
        def __init__(self, options):
            self.urls = {"api": "https://api.binance.com/api/v3", "demo": "https://demo-api.binance.com/api/v3"}

        def enable_demo_trading(self, enabled):
            calls.append((enabled, self.urls["demo"]))
            self.urls["api"] = self.urls["demo"]

        def set_sandbox_mode(self, enabled):
            raise AssertionError("Binance demo mode must not use Spot Testnet")

    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret")
    monkeypatch.setattr(ccxt, "binance", FakeBinance)
    exchange_client_module._default_exchange_factory("binance", sandbox=True)
    assert calls == [(True, "https://demo-api.binance.com/api/v3")]


def test_default_factory_sandbox_routes_other_exchange_to_sandbox(monkeypatch):
    import tradingagents.execution.exchange_client as exchange_client_module

    calls = []

    class FakeOkx(ccxt.Exchange):
        def __init__(self, options):
            pass

        def set_sandbox_mode(self, enabled):
            calls.append(enabled)

    monkeypatch.setattr(ccxt, "okx", FakeOkx)
    exchange_client_module._default_exchange_factory("okx", sandbox=True)
    assert calls == [True]


def test_default_factory_selects_exchange_class_by_id(monkeypatch):
    # Two fake exchange ids: each resolves its own ccxt class and its own
    # {ID}_API_KEY / {ID}_SECRET env names (generalized from the L1 Binance
    # pair, which keeps working unchanged for id "binance").
    import tradingagents.execution.exchange_client as exchange_client_module

    built = []

    def make_fake(name):
        class FakeVenue(ccxt.Exchange):
            def __init__(self, options):
                built.append((name, options))

        return FakeVenue

    monkeypatch.setattr(ccxt, "okx", make_fake("okx"))
    monkeypatch.setattr(ccxt, "kraken", make_fake("kraken"))
    monkeypatch.setenv("OKX_API_KEY", "okx-key")
    monkeypatch.setenv("OKX_SECRET", "okx-secret")
    monkeypatch.setenv("KRAKEN_API_KEY", "kraken-key")
    monkeypatch.setenv("KRAKEN_SECRET", "kraken-secret")

    exchange_client_module._default_exchange_factory("okx")
    exchange_client_module._default_exchange_factory("kraken")
    assert built == [
        ("okx", {"apiKey": "okx-key", "secret": "okx-secret", "enableRateLimit": True}),
        ("kraken", {"apiKey": "kraken-key", "secret": "kraken-secret", "enableRateLimit": True}),
    ]


def test_default_factory_rejects_unknown_exchange_id():
    import tradingagents.execution.exchange_client as exchange_client_module

    with pytest.raises(ValueError, match="does not name a ccxt exchange class"):
        exchange_client_module._default_exchange_factory("nosuchexchange")


def test_default_factory_rejects_non_exchange_attribute(monkeypatch):
    import tradingagents.execution.exchange_client as exchange_client_module

    monkeypatch.setattr(ccxt, "impostor", object(), raising=False)  # not an Exchange subclass
    with pytest.raises(ValueError, match="does not name a ccxt exchange class"):
        exchange_client_module._default_exchange_factory("impostor")


def test_default_factory_rejects_plain_function(monkeypatch):
    # A callable that is not a ccxt.Exchange subclass must fail the factory
    # validation, not ride duck typing into the live path.
    import tradingagents.execution.exchange_client as exchange_client_module

    monkeypatch.setattr(ccxt, "impostor", lambda options: object(), raising=False)
    with pytest.raises(ValueError, match="does not name a ccxt exchange class"):
        exchange_client_module._default_exchange_factory("impostor")
