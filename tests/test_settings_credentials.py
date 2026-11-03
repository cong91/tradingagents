import pytest

from server import settings as settings_module
from tradingagents.default_config import DEFAULT_CONFIG


@pytest.mark.parametrize(
    ("base_url", "key_env", "secret_env"),
    [
        ("https://demo-api.binance.com", "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_SECRET"),
        ("https://api.binance.com", "BINANCE_API_KEY", "BINANCE_SECRET"),
    ],
)
def test_settings_payload_uses_profile_credentials(monkeypatch, base_url, key_env, secret_env):
    config = {**DEFAULT_CONFIG, "exec_exchange_id": "binance", "exec_base_url": base_url}
    monkeypatch.setattr(settings_module, "get_config", lambda: config)
    monkeypatch.setattr(settings_module, "effective_exec_mode", lambda: "dry")
    monkeypatch.setenv("BINANCE_API_KEY", "prod-key-1234")
    monkeypatch.setenv("BINANCE_SECRET", "prod-secret-5678")
    monkeypatch.setenv("BINANCE_DEMO_API_KEY", "demo-key-9876")
    monkeypatch.setenv("BINANCE_DEMO_SECRET", "demo-secret-5432")

    credentials = {
        item["env_var"]: item for item in settings_module._settings_payload()["credentials"]
    }
    binance_names = {name for name in credentials if name.startswith("BINANCE_")}
    assert binance_names == {key_env, secret_env}
    assert credentials[key_env]["configured"] is True
    assert credentials[key_env]["masked"].endswith("9876" if "DEMO" in key_env else "1234")
    assert credentials[secret_env]["masked"].endswith("5432" if "DEMO" in secret_env else "5678")
    assert "prod-key-1234" not in str(credentials)
    assert "demo-key-9876" not in str(credentials)
