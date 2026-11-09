"""Market-specific automation mode state and legacy gate conversion."""

from __future__ import annotations

from enum import StrEnum

from server import control_store
from tradingagents.dataflows.config import get_config, set_config


class AutoMode(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"


MODE_KEYS = {
    AutoMode.SPOT: "auto_spot_enabled",
    AutoMode.FUTURES: "auto_futures_enabled",
}
MODE_DEFAULTS = dict.fromkeys(MODE_KEYS.values(), False)
LEGACY_KEYS = ("exec_auto_confirm", "exec_derivatives")


def enabled_modes(config: dict | None = None) -> frozenset[AutoMode]:
    """Return enabled entry-automation modes from the process config."""
    config = get_config() if config is None else config
    return frozenset(
        mode for mode, key in MODE_KEYS.items() if config.get(key) is True
    )


def mode_config(config: dict | None = None) -> dict[str, bool]:
    modes = enabled_modes(config)
    return {mode.value: mode in modes for mode in AutoMode}


def legacy_mode_values(stored: dict[str, object]) -> dict[str, bool]:
    """Map old switches without silently upgrading ambiguous state."""
    auto_confirm = stored.get("exec_auto_confirm") is True
    derivatives = stored.get("exec_derivatives") is True
    live = stored.get("exec_live") is True
    return {
        MODE_KEYS[AutoMode.SPOT]: live and auto_confirm and not derivatives,
        MODE_KEYS[AutoMode.FUTURES]: live and auto_confirm and derivatives,
    }


def ensure_modes_migrated(*, updated_by: str = "auto-mode-migration") -> dict[str, bool]:
    """Initialize mode rows once, converting legacy settings conservatively."""
    stored = control_store.load_operator_settings()
    mode_keys = tuple(MODE_KEYS.values())
    present = set(stored).intersection(mode_keys)
    if present:
        values = {
            key: stored[key] is True if key in present else False
            for key in mode_keys
        }
    else:
        values = legacy_mode_values(stored)

    missing = {key: value for key, value in values.items() if key not in present}
    if missing:
        inserted = control_store.insert_operator_settings_if_absent(
            missing, updated_by=updated_by
        )
        stored = control_store.load_operator_settings()
        values = {key: stored.get(key) is True for key in mode_keys}
        if inserted:
            from server import audit as audit_log

            audit_log.record(
                "auto_modes_migrated",
                legacy_present=sorted(key for key in LEGACY_KEYS if key in stored),
                modes=values,
            )
    set_config(values)
    return values


def set_modes(values: dict[str, bool], *, updated_by: str | None = None) -> None:
    """Persist and apply one or both mode switches atomically."""
    if not values or any(
        key not in MODE_KEYS.values() or not isinstance(value, bool)
        for key, value in values.items()
    ):
        raise ValueError("auto mode values must be booleans")
    from tradingagents.execution.auto_authorization import mode_update_lock

    affected_modes = [mode for mode, key in MODE_KEYS.items() if key in values]
    with mode_update_lock(affected_modes):
        control_store.set_operator_settings(values, updated_by=updated_by)
        set_config(values)
