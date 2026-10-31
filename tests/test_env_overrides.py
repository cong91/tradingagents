"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import os

import pytest

import tradingagents.default_config as default_config_module


def _config_with_env(monkeypatch, **overrides):
    """The defaults as the environment given here would set them."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return default_config_module.build_default_config()


def test_no_env_uses_built_in_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["llm_provider"] == "openai"
    assert config["deep_think_llm"] == "gpt-6-sol"
    assert config["quick_think_llm"] == "gpt-6-luna"
    assert config["backend_url"] is None
    assert config["max_debate_rounds"] == 1
    assert config["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert config["llm_provider"] == "google"
    assert config["deep_think_llm"] == "gemini-3-pro-preview"
    assert config["quick_think_llm"] == "gemini-3-flash-preview"
    assert config["backend_url"] == "https://example.invalid/v1"
    assert config["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert config["max_debate_rounds"] == 3
    assert isinstance(config["max_debate_rounds"], int)
    assert config["max_risk_discuss_rounds"] == 2
    assert isinstance(config["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert config["checkpoint_enabled"] is expected


def test_reasoning_thinking_overrides(monkeypatch):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert config["openai_reasoning_effort"] == "high"
    assert config["google_thinking_level"] == "minimal"
    assert config["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    config = _config_with_env(monkeypatch)
    assert config["openai_reasoning_effort"] is None
    assert config["google_thinking_level"] is None
    assert config["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert config["llm_provider"] == "openai"
    assert config["max_debate_rounds"] == 1


def test_empty_path_value_keeps_the_default_path(monkeypatch):
    """.env.example lists the path variables blank; uncommenting one made the
    path empty, and the graph failed creating its directories."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_RESULTS_DIR="",
        TRADINGAGENTS_CACHE_DIR="",
        TRADINGAGENTS_MEMORY_LOG_PATH="",
    )
    home = default_config_module._TRADINGAGENTS_HOME
    assert config["results_dir"] == os.path.join(home, "logs")
    assert config["data_cache_dir"] == os.path.join(home, "cache")
    assert config["memory_log_path"] == os.path.join(home, "memory", "trading_memory.md")


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        default_config_module.build_default_config()


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(monkeypatch, bad):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_ENABLED", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_CHECKPOINT_ENABLED"):
        default_config_module.build_default_config()


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in config


# --- L2: risk guard limits and multi-exchange config -----------------------


def test_risk_and_execution_defaults(monkeypatch):
    config = _config_with_env(monkeypatch)
    assert config["risk_max_daily_loss_pct"] == 5.0
    assert config["risk_max_position_pct_per_asset"] == 25.0
    assert config["risk_max_total_exposure_pct"] == 80.0
    assert config["risk_max_consecutive_loss_count"] == 3
    assert isinstance(config["risk_max_consecutive_loss_count"], int)
    assert config["risk_max_derivatives_leverage"] == 1.0
    assert config["risk_max_derivatives_exposure_pct"] == 0.0
    assert config["risk_derivatives_max_risk_fraction"] == 0.02
    assert config["risk_derivatives_max_margin_fraction"] == 0.5
    assert config["risk_derivatives_risk_fraction"] == 0.01
    assert config["risk_derivatives_margin_fraction"] == 0.25
    assert config["risk_derivatives_profit_protection_enabled"] is True
    assert config["risk_derivatives_break_even_r"] == 0.5
    assert config["risk_derivatives_lock_profit_r"] == 1.0
    assert config["risk_derivatives_trail_start_r"] == 1.5
    assert config["risk_derivatives_trail_distance_r"] == 0.75
    assert config["risk_derivatives_target_extension_r"] == 1.0
    assert config["risk_derivatives_max_hold_hours"] == 72.0
    assert config["risk_derivatives_time_stop_min_r"] == 1.0
    assert config["risk_derivatives_round_trip_fee_rate"] == 0.001
    assert config["risk_derivatives_time_stop_postpone_minutes"] == 90.0
    assert config["risk_derivatives_min_stop_move_r"] == 0.1
    assert config["risk_derivatives_mark_max_age_seconds"] == 120.0
    assert config["risk_derivatives_scale_out_enabled"] is False
    assert config["risk_derivatives_scale_out_first_r"] == 1.0
    assert config["risk_derivatives_scale_out_first_fraction"] == 0.33
    assert config["risk_derivatives_scale_out_second_r"] == 2.0
    assert config["risk_derivatives_scale_out_second_fraction"] == 0.33
    assert config["exec_exchange_id"] == "binance"
    assert config["exec_symbol_overrides"] == {}
    assert config["exec_watchlist"] == ["BTC-USD", "ETH-USD"]
    assert config["exec_auto_confirm"] is False
    assert config["exec_derivatives"] is False


def test_risk_float_and_int_coercion(monkeypatch):
    config = _config_with_env(
        monkeypatch,
        TRADINGAGENTS_RISK_MAX_DAILY_LOSS_PCT="2.5",
        TRADINGAGENTS_RISK_MAX_POSITION_PCT_PER_ASSET="10",
        TRADINGAGENTS_RISK_MAX_TOTAL_EXPOSURE_PCT="60.5",
        TRADINGAGENTS_RISK_MAX_CONSECUTIVE_LOSS_COUNT="5",
        TRADINGAGENTS_RISK_MAX_DERIVATIVES_LEVERAGE="3",
        TRADINGAGENTS_RISK_MAX_DERIVATIVES_EXPOSURE_PCT="40.0",
        TRADINGAGENTS_RISK_DERIVATIVES_MAX_HOLD_HOURS="24.5",
        TRADINGAGENTS_RISK_DERIVATIVES_TIME_STOP_MIN_R="0.5",
        TRADINGAGENTS_RISK_DERIVATIVES_ROUND_TRIP_FEE_RATE="0.0004",
        TRADINGAGENTS_RISK_DERIVATIVES_TIME_STOP_POSTPONE_MINUTES="45",
        TRADINGAGENTS_EXEC_EXCHANGE_ID="okx",
    )
    assert config["risk_max_daily_loss_pct"] == 2.5
    assert isinstance(config["risk_max_daily_loss_pct"], float)
    assert config["risk_max_position_pct_per_asset"] == 10.0
    assert config["risk_max_total_exposure_pct"] == 60.5
    assert config["risk_max_consecutive_loss_count"] == 5
    assert isinstance(config["risk_max_consecutive_loss_count"], int)
    assert config["risk_max_derivatives_leverage"] == 3.0
    assert config["risk_max_derivatives_exposure_pct"] == 40.0
    assert config["risk_derivatives_max_hold_hours"] == 24.5
    assert config["risk_derivatives_time_stop_min_r"] == 0.5
    assert config["risk_derivatives_round_trip_fee_rate"] == 0.0004
    assert isinstance(config["risk_derivatives_round_trip_fee_rate"], float)
    assert config["risk_derivatives_time_stop_postpone_minutes"] == 45.0
    assert config["exec_exchange_id"] == "okx"


@pytest.mark.parametrize(
    "env_var",
    [
        "TRADINGAGENTS_RISK_MAX_DAILY_LOSS_PCT",
        "TRADINGAGENTS_RISK_MAX_CONSECUTIVE_LOSS_COUNT",
    ],
)
def test_invalid_risk_value_raises(monkeypatch, env_var):
    """Garbage numeric risk values must fail loudly at import."""
    monkeypatch.setenv(env_var, "not-a-number")
    with pytest.raises(ValueError, match=env_var):
        default_config_module.build_default_config()


@pytest.mark.parametrize("env_var", [
    "TRADINGAGENTS_RISK_MAX_DAILY_LOSS_PCT",
    "TRADINGAGENTS_RISK_MAX_CONSECUTIVE_LOSS_COUNT",
    "TRADINGAGENTS_RISK_MAX_DERIVATIVES_LEVERAGE",
    "TRADINGAGENTS_RISK_MAX_DERIVATIVES_EXPOSURE_PCT",
])
def test_non_finite_risk_value_raises(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "nan")
    with pytest.raises(ValueError, match=env_var):
        default_config_module.build_default_config()
    monkeypatch.delenv(env_var, raising=False)


def test_exchange_id_accepts_any_string(monkeypatch):
    config = _config_with_env(monkeypatch, TRADINGAGENTS_EXEC_EXCHANGE_ID="not-a-number")
    assert config["exec_exchange_id"] == "not-a-number"


def test_execution_gates_and_structured_keys_are_not_env_overridable(monkeypatch):
    """exec_auto_confirm/exec_derivatives follow the exec_live pattern (fail-closed
    call-time reads, never folded into config), and _coerce cannot build the
    dict/list keys, so none of them may be env-overridable."""
    from copy import deepcopy

    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_WATCHLIST", "SOL-USD")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_SYMBOL_OVERRIDES", "garbage")
    config = deepcopy(default_config_module.DEFAULT_CONFIG)
    default_config_module._apply_env_overrides(config)
    assert config["exec_auto_confirm"] is False
    assert config["exec_derivatives"] is False
    assert config["exec_watchlist"] == ["BTC-USD", "ETH-USD"]
    assert config["exec_symbol_overrides"] == {}
