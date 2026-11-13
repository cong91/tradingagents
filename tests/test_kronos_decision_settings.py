"""The active Kronos model-tokenizer profile and its API contract."""

from __future__ import annotations

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.default_config import _ENV_OVERRIDES, DEFAULT_CONFIG

pytestmark = pytest.mark.unit


def test_operator_status_reports_invalid_profile_as_unavailable(client, monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG

    config = {**DEFAULT_CONFIG, "kronos_active_profile": "invalid-profile"}
    monkeypatch.setattr("server.operator_config.get_config", lambda: config)
    response = client.get("/api/operator-config")
    assert response.status_code == 200, response.text
    kronos = response.json()["kronos"]
    assert kronos["profile_id"] == "invalid-profile"
    assert kronos["readiness"]["status"] == "unavailable"
    assert "unknown Kronos profile" in kronos["readiness"]["reason"]
    assert "enabled" not in kronos
    assert "mode" not in kronos
    assert "failure_policy" not in kronos


def test_supported_kronos_profiles_are_closed_model_tokenizer_pairs():
    from tradingagents.forecast.artifact import PROFILES, profile_for

    assert set(PROFILES) == {"small_base", "mini_2k"}
    assert profile_for("small_base").model_repo == "NeoQuasar/Kronos-small"
    assert profile_for("small_base").tokenizer_repo == "NeoQuasar/Kronos-Tokenizer-base"
    assert profile_for("small_base").max_context == 512
    assert profile_for("mini_2k").model_repo == "NeoQuasar/Kronos-mini"
    assert profile_for("mini_2k").tokenizer_repo == "NeoQuasar/Kronos-Tokenizer-2k"
    assert profile_for("mini_2k").max_context == 2048
    with pytest.raises(ValueError, match="unknown Kronos profile"):
        profile_for("model-only")


def test_profile_controls_lookback_bound_and_cache_parameters():
    from tradingagents.forecast.settings import ForecastSettings

    assert ForecastSettings(profile="small_base", lookback_bars=512).settings_problem() is None
    assert ForecastSettings(profile="small_base", lookback_bars=513).settings_problem()
    assert ForecastSettings(profile="mini_2k", lookback_bars=2048).settings_problem() is None
    assert ForecastSettings(profile="mini_2k", lookback_bars=2049).settings_problem()
    assert ForecastSettings(profile="mini_2k").as_params()["profile"] == "mini_2k"


def test_active_profile_is_the_only_shipped_forecast_setting():
    assert DEFAULT_CONFIG["kronos_active_profile"] == "small_base"
    assert {key for key in DEFAULT_CONFIG if key.startswith("kronos_active_")} == {
        "kronos_active_profile",
    }


def test_only_the_model_tokenizer_profile_is_env_overridable():
    assert {
        key for key in _ENV_OVERRIDES.values() if key.startswith("kronos_active_")
    } == {"kronos_active_profile"}


def test_retired_forecast_keys_are_not_in_settings_api(client):
    from server.settings import _ENV_BY_KEY

    assert {key for key in _ENV_BY_KEY if key.startswith("kronos_active_")} == {
        "kronos_active_profile",
    }
    for key, value in (
        ("kronos_active_paths", 24),
        ("kronos_active_mode", "context_only"),
        ("kronos_active_enabled", True),
    ):
        response = client.put("/api/settings", json={"settings": {key: value}})
        assert response.status_code == 403, response.text


def test_settings_api_validates_profile_selection(client):
    response = client.put(
        "/api/settings",
        json={"settings": {"kronos_active_profile": "mini_2k"}},
    )
    assert response.status_code == 200, response.text
    from tradingagents.dataflows.config import get_config

    assert get_config()["kronos_active_profile"] == "mini_2k"

    invalid = client.put(
        "/api/settings",
        json={"settings": {"kronos_active_profile": "unknown"}},
    )
    assert invalid.status_code == 422, invalid.text
    fields = invalid.json()["error"]["details"]["fields"]
    assert "kronos_active_profile" in fields
    assert get_config()["kronos_active_profile"] == "mini_2k"


def test_operator_config_rejects_the_removed_kronos_enable_switch(client):
    from server.operator_config import _ALLOWED

    assert "kronos_active_enabled" not in _ALLOWED
    response = client.put(
        "/api/operator-config",
        json={"settings": {"kronos_active_enabled": False}},
    )
    assert response.status_code == 403, response.text


def test_active_forecast_settings_are_derived_from_profile_and_position_limit():
    from tradingagents.forecast.settings import settings_from_config

    settings = settings_from_config({
        "kronos_active_profile": "mini_2k",
        "risk_derivatives_max_hold_hours": 50.5,
    })

    assert settings.profile == "mini_2k"
    assert settings.timeframe == "4h"
    assert settings.lookback_bars == 2048
    assert settings.horizon_hours == 48
    assert settings.paths == 16
    assert settings.temperature == 0.5
    assert not hasattr(settings, "enabled")
    assert not hasattr(settings, "mode")
    assert not hasattr(settings, "failure_policy")


@pytest.mark.parametrize(
    ("max_hold_hours", "expected_horizon"),
    [(0, 24), (2, 4), (72, 72), (500, 288)],
)
def test_forecast_horizon_is_derived_and_bounded(max_hold_hours, expected_horizon):
    from tradingagents.forecast.settings import settings_from_config

    settings = settings_from_config({"risk_derivatives_max_hold_hours": max_hold_hours})

    assert settings.horizon_hours == expected_horizon


def test_operator_payload_exposes_profile_readiness_without_a_switch(client):
    response = client.get("/api/operator-config")
    assert response.status_code == 200, response.text
    kronos = response.json()["kronos"]
    assert kronos["profile_id"] in {"small_base", "mini_2k"}
    assert kronos["timeframe"] == "4h"
    assert kronos["paths"] == 16
    assert kronos["temperature"] == 0.5
    assert kronos["readiness"]["status"] in {"ready", "unavailable"}
    assert "enabled" not in kronos
    assert "mode" not in kronos
    assert "failure_policy" not in kronos
    assert "settings_problem" not in kronos
