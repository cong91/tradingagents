"""F3: who owns each Kronos-active setting, and that the API enforces it.

The ownership contract (AGENTS.md):

- ``kronos_active_enabled`` is a MANAGED OPERATOR SWITCH, like the Auto modes:
  written through ``PUT /api/operator-config`` with ``confirm=true`` plus a
  written reason, persisted in ``control_operator_settings`` with audit, and
  deliberately ABSENT from ``_ENV_OVERRIDES`` so no env var can arm it.
- The tuning keys (mode/timeframe/horizon/paths/temperature/lookback/batch/
  failure policy/band spread) are ENGINE SETTINGS: editable through
  ``PUT /api/settings``, persisted in ``control_engine_settings``, and
  env-overridable through ``_ENV_OVERRIDES``.
- The shipped default is OFF, and nothing here can turn it on implicitly.

These tests assert the surfaces, not the handlers' internals: the allowlist
must contain the switch, the settings surface must know the tuning keys, and a
write without confirmation must be refused.
"""

from __future__ import annotations

import pytest

from tests.server_api_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)
from tradingagents.default_config import _ENV_OVERRIDES, DEFAULT_CONFIG

pytestmark = pytest.mark.unit

_TUNING_KEYS = (
    "kronos_active_profile",
    "kronos_active_mode",
    "kronos_active_timeframe",
    "kronos_active_horizon_hours",
    "kronos_active_paths",
    "kronos_active_temperature",
    "kronos_active_lookback_bars",
    "kronos_active_max_batch_rows",
    "kronos_active_failure_policy",
    "kronos_active_max_band_spread_ratio",
)


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
    assert kronos["settings_problem"]
    assert kronos["max_context"] is None
    assert kronos["model"]["repo"] == ""


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


def test_settings_api_validates_profile_and_lookback_as_one_prospective_config(client):
    response = client.put(
        "/api/settings",
        json={"settings": {
            "kronos_active_profile": "mini_2k",
            "kronos_active_lookback_bars": 2048,
        }},
    )
    assert response.status_code == 200, response.text
    from tradingagents.dataflows.config import get_config

    assert get_config()["kronos_active_profile"] == "mini_2k"
    assert get_config()["kronos_active_lookback_bars"] == 2048

    invalid = client.put(
        "/api/settings",
        json={"settings": {"kronos_active_profile": "small_base"}},
    )
    assert invalid.status_code == 422, invalid.text
    fields = invalid.json()["error"]["details"]["fields"]
    assert "kronos_active_lookback_bars" in fields
    assert get_config()["kronos_active_profile"] == "mini_2k"
    assert get_config()["kronos_active_lookback_bars"] == 2048


def test_unknown_profile_is_refused_before_settings_mutation(client):
    response = client.put(
        "/api/settings",
        json={"settings": {"kronos_active_profile": "unknown"}},
    )
    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fields"]
    assert "kronos_active_profile" in fields



def test_the_shipped_default_is_off():
    assert DEFAULT_CONFIG["kronos_active_enabled"] is False
    assert DEFAULT_CONFIG["kronos_active_mode"] == "context_only"
    for key in _TUNING_KEYS:
        assert key in DEFAULT_CONFIG, f"{key} has no shipped default"


def test_the_enabled_switch_is_not_env_armable():
    """The switch that changes what a decision cycle reads is operator-managed.

    An env var would arm unattended model inference without the confirmation,
    reason and audit the operator surface exists to record — the same rule the
    Auto modes follow.
    """
    assert "kronos_active_enabled" not in _ENV_OVERRIDES


def test_every_tuning_key_is_env_overridable():
    for key in _TUNING_KEYS:
        env = f"TRADINGAGENTS_{key.upper()}"
        assert _ENV_OVERRIDES.get(env) == key, f"{key} is not exposed via {env}"


def test_the_operator_allowlist_contains_the_switch_with_its_group():
    from server.operator_config import _ALLOWED, _GATE_KEYS

    meta = _ALLOWED["kronos_active_enabled"]
    assert meta["type"] == "bool"
    assert meta["group"] == "forecast"
    # The switch is a deliberate operator write (confirm + reason + audit) but
    # not an order-arming mode: it is in _GATE_KEYS for the confirmation rule
    # and deliberately absent from auto_modes.MODE_KEYS, so enabling it can
    # never arm live trading by itself.
    assert "kronos_active_enabled" in _GATE_KEYS
    from server.auto_modes import MODE_KEYS

    assert "kronos_active_enabled" not in MODE_KEYS


def test_env_override_coercion_matches_the_declared_types():
    """The coercion contract: a wrong type fails loudly, a right one applies."""
    from tradingagents.default_config import _coerce

    assert _coerce("16", DEFAULT_CONFIG["kronos_active_paths"]) == 16
    assert _coerce("0.5", DEFAULT_CONFIG["kronos_active_temperature"]) == 0.5
    assert _coerce("4h", DEFAULT_CONFIG["kronos_active_timeframe"]) == "4h"
    with pytest.raises(ValueError):
        _coerce("sixteen", DEFAULT_CONFIG["kronos_active_paths"])


def test_the_settings_api_types_the_tuning_keys():
    """PUT /api/settings can edit the tuning keys, so their JSON types resolve."""
    from server.settings import _type_name

    assert _type_name("kronos_active_paths", DEFAULT_CONFIG["kronos_active_paths"]) == "int"
    assert _type_name(
        "kronos_active_temperature", DEFAULT_CONFIG["kronos_active_temperature"]
    ) == "float"
    assert _type_name("kronos_active_mode", DEFAULT_CONFIG["kronos_active_mode"]) == "str"


def test_the_service_reads_the_operator_switch_from_config():
    """Enabled comes only from config, so the operator surface controls it."""
    from tradingagents.forecast.settings import settings_from_config

    assert settings_from_config({}).enabled is False
    assert settings_from_config({"kronos_active_enabled": True}).enabled is True


def test_the_operator_api_refuses_an_unconfirmed_switch_write(client):
    """The confirm + reason rule applies through the real app handler."""
    response = client.put(
        "/api/operator-config",
        json={"settings": {"kronos_active_enabled": True}},
    )
    assert response.status_code == 412, response.text
    body = response.json()
    assert body["error"]["code"] == "gate_confirmation_required", body
    assert "kronos_active_enabled" in body["error"]["details"]["keys"], body


def test_the_operator_api_accepts_a_confirmed_switch_write(client):
    """A confirmed write lands in the operator store with its reason, audited."""
    response = client.put(
        "/api/operator-config",
        json={
            "settings": {"kronos_active_enabled": True},
            "confirm": True,
            "reason": "observe kronos context in a test cycle",
            "actor": "kronos-active-test",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    row = next(row for row in payload["settings"] if row["key"] == "kronos_active_enabled")
    assert row["value"] is True
    from server import control_store

    stored = control_store.load_operator_setting_rows()
    assert any(row["key"] == "kronos_active_enabled" and row["value"] is True
               for row in stored), stored
    # Resetting is the safe direction and needs no confirmation.
    reset = client.request("DELETE", "/api/operator-config/kronos_active_enabled", json={})
    assert reset.status_code == 200, reset.text
    after = control_store.load_operator_setting_rows()
    assert not any(row["key"] == "kronos_active_enabled" for row in after), after


def test_the_settings_api_persists_a_tuning_key(client):
    """The tuning keys are engine settings: PUT /api/settings edits them."""
    response = client.put(
        "/api/settings",
        json={"settings": {"kronos_active_paths": 24}},
    )
    assert response.status_code == 200, response.text
    from tradingagents.dataflows.config import get_config

    assert get_config()["kronos_active_paths"] == 24
    from server import control_store

    assert control_store.load_engine_settings().get("kronos_active_paths") == 24


# -- the API boundary refuses values the forecast core cannot run ------------


@pytest.mark.parametrize("key,value,needle", [
    ("kronos_active_paths", 3, "paths"),
    ("kronos_active_paths", 64, "paths"),
    ("kronos_active_mode", "hard_filter", "mode"),
    ("kronos_active_timeframe", "8h", "timeframe"),
    ("kronos_active_horizon_hours", 25, "whole number"),
    ("kronos_active_horizon_hours", 300, "bars is outside"),
    ("kronos_active_lookback_bars", 8, "lookback_bars"),
    ("kronos_active_max_batch_rows", 0, "max_batch_rows"),
    ("kronos_active_failure_policy", "context_only", "failure policy"),
    ("kronos_active_temperature", 0.0, "temperature"),
    ("kronos_active_max_band_spread_ratio", 0, "positive"),
])
def test_the_settings_api_refuses_an_unrunnable_tuning_value(client, key, value, needle):
    """A bad write is refused per field BEFORE anything is applied."""
    response = client.put("/api/settings", json={"settings": {key: value}})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_error", body
    fields = body["error"]["details"]["fields"]
    assert key in fields, body
    assert needle in fields[key], fields
    # Nothing landed: memory and PostgreSQL are untouched.
    from tradingagents.dataflows.config import get_config

    assert get_config().get(key) == DEFAULT_CONFIG.get(key)
    from server import control_store

    assert control_store.load_engine_settings().get(key) is None


def test_the_settings_api_refuses_a_non_finite_number(client):
    """A lenient client can put a literal ``NaN`` on the wire (Python's json
    parses it); the boundary must refuse it as a non-finite number."""
    response = client.put(
        "/api/settings",
        content='{"settings": {"kronos_active_temperature": NaN}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fields"]
    assert "kronos_active_temperature" in fields
    assert "finite" in fields["kronos_active_temperature"]


def test_the_settings_api_refuses_a_cross_field_horizon_timeframe_pair(client):
    """A horizon that is not whole bars of the timeframe being written."""
    response = client.put(
        "/api/settings",
        json={"settings": {
            "kronos_active_timeframe": "4h",
            "kronos_active_horizon_hours": 25,
        }},
    )
    assert response.status_code == 422, response.text
    fields = response.json()["error"]["details"]["fields"]
    assert "kronos_active_horizon_hours" in fields
    assert "4h bars" in fields["kronos_active_horizon_hours"]


def test_a_rejected_write_does_not_leak_a_partial_mutation(client):
    """Two keys in one write, one invalid: NEITHER is applied."""
    response = client.put(
        "/api/settings",
        json={"settings": {
            "kronos_active_paths": 24,
            "kronos_active_failure_policy": "sometimes",
        }},
    )
    assert response.status_code == 422, response.text
    from tradingagents.dataflows.config import get_config

    assert get_config().get("kronos_active_paths") == DEFAULT_CONFIG["kronos_active_paths"]


def test_the_service_still_refuses_a_bad_env_value_at_read_time():
    """The boundary check guards the API; env-overridden values (boot time)
    are still refused by the settings layer, surfaced as unavailable."""
    from tradingagents.forecast.settings import settings_from_config

    broken = settings_from_config({"kronos_active_enabled": True, "kronos_active_paths": -4})
    assert broken.paths == -4, "a configured value is reported as given, never silently fixed"
    assert broken.unavailable_reason() is not None


def test_the_operator_payload_reports_the_kronos_status(client):
    """GET /api/operator-config carries the read-only Kronos status block: the
    values the next cycle would run under, plus the on-disk readiness probe."""
    response = client.get("/api/operator-config")
    assert response.status_code == 200, response.text
    kronos = response.json()["kronos"]
    assert kronos["enabled"] is False
    assert kronos["mode"] == "context_only"
    assert kronos["timeframe"] == "4h"
    assert kronos["horizon_hours"] == 24
    assert kronos["paths"] == 16
    assert kronos["failure_policy"] == "defer"
    # Shipped defaults run: no settings-level problem, and the readiness probe
    # answered honestly (ready, or unavailable WITH the reason).
    assert kronos["settings_problem"] is None
    assert kronos["readiness"]["status"] in {"ready", "unavailable"}
    if kronos["readiness"]["status"] == "unavailable":
        assert kronos["readiness"]["reason"]
    assert kronos["model"]["repo"]
