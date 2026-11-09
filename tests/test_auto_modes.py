"""Unit tests for automation mode mapping and migration."""

from unittest.mock import Mock, call

import pytest

from server import audit, auto_modes

pytestmark = pytest.mark.unit


LEGACY_CASES = [
    pytest.param(True, False, {"auto_spot_enabled": True, "auto_futures_enabled": False}, id="spot"),
    pytest.param(True, True, {"auto_spot_enabled": False, "auto_futures_enabled": True}, id="futures"),
    pytest.param(False, True, {"auto_spot_enabled": False, "auto_futures_enabled": False}, id="no-confirm"),
    pytest.param(False, False, {"auto_spot_enabled": False, "auto_futures_enabled": False}, id="neither"),
]

MISSING_LEGACY_CASES = [
    pytest.param(
        {"exec_live": True},
        {"auto_spot_enabled": False, "auto_futures_enabled": False},
        id="both-missing",
    ),
    pytest.param(
        {"exec_live": True, "exec_derivatives": True},
        {"auto_spot_enabled": False, "auto_futures_enabled": False},
        id="confirm-missing",
    ),
    pytest.param(
        {"exec_live": True, "exec_auto_confirm": True},
        {"auto_spot_enabled": True, "auto_futures_enabled": False},
        id="derivatives-missing",
    ),
    pytest.param(
        {"exec_live": True, "exec_auto_confirm": False},
        {"auto_spot_enabled": False, "auto_futures_enabled": False},
        id="derivatives-missing-confirm-disabled",
    ),
]


@pytest.fixture
def mode_store(monkeypatch):
    stored = {}
    store = Mock(spec=auto_modes.control_store)
    store.load_operator_settings.side_effect = lambda: dict(stored)

    def insert_if_absent(updates, *, updated_by=None):
        inserted = set(updates) - stored.keys()
        for key in inserted:
            stored[key] = updates[key]
        return inserted

    def set_settings(updates, *, updated_by=None):
        stored.update(updates)

    store.insert_operator_settings_if_absent.side_effect = insert_if_absent
    store.set_operator_settings.side_effect = set_settings
    set_config = Mock()
    record = Mock()
    monkeypatch.setattr(auto_modes, "control_store", store)
    monkeypatch.setattr(auto_modes, "set_config", set_config)
    monkeypatch.setattr(audit, "record", record)
    return stored, store, set_config, record


@pytest.mark.parametrize("auto_confirm,derivatives,expected", LEGACY_CASES)
def test_legacy_mode_values_maps_each_switch_combination(auto_confirm, derivatives, expected):
    legacy = {
        "exec_live": True,
        "exec_auto_confirm": auto_confirm,
        "exec_derivatives": derivatives,
    }

    assert auto_modes.legacy_mode_values(legacy) == expected
    assert legacy == {
        "exec_live": True,
        "exec_auto_confirm": auto_confirm,
        "exec_derivatives": derivatives,
    }


@pytest.mark.parametrize("legacy,expected", MISSING_LEGACY_CASES)
def test_legacy_mode_values_treats_missing_rows_as_disabled(legacy, expected):
    assert auto_modes.legacy_mode_values(legacy) == expected


@pytest.mark.parametrize("value", [1, "true", None])
def test_legacy_mode_values_requires_actual_booleans(value):
    assert auto_modes.legacy_mode_values({
        "exec_live": True,
        "exec_auto_confirm": value,
        "exec_derivatives": True,
    }) == {"auto_spot_enabled": False, "auto_futures_enabled": False}
    assert auto_modes.legacy_mode_values({
        "exec_live": True,
        "exec_auto_confirm": True,
        "exec_derivatives": value,
    }) == {"auto_spot_enabled": True, "auto_futures_enabled": False}


@pytest.mark.parametrize(
    "config,enabled,rendered",
    [
        ({}, frozenset(), {"spot": False, "futures": False}),
        ({"auto_spot_enabled": True}, frozenset({auto_modes.AutoMode.SPOT}),
         {"spot": True, "futures": False}),
        ({"auto_futures_enabled": True}, frozenset({auto_modes.AutoMode.FUTURES}),
         {"spot": False, "futures": True}),
        ({"auto_spot_enabled": True, "auto_futures_enabled": True},
         frozenset({auto_modes.AutoMode.SPOT, auto_modes.AutoMode.FUTURES}),
         {"spot": True, "futures": True}),
        ({"auto_spot_enabled": 1, "auto_futures_enabled": "true"}, frozenset(),
         {"spot": False, "futures": False}),
    ],
)
def test_mode_mapping_requires_true_for_each_enabled_mode(config, enabled, rendered):
    assert auto_modes.enabled_modes(config) == enabled
    assert auto_modes.mode_config(config) == rendered


def test_mode_mapping_uses_process_config_when_no_config_is_supplied(monkeypatch):
    get_config = Mock(return_value={"auto_futures_enabled": True})
    monkeypatch.setattr(auto_modes, "get_config", get_config)

    assert auto_modes.mode_config() == {"spot": False, "futures": True}
    get_config.assert_called_once_with()


@pytest.mark.parametrize("key", ["auto_spot_enabled", "auto_futures_enabled"])
def test_mode_default_is_disabled(key):
    assert auto_modes.MODE_DEFAULTS[key] is False


@pytest.mark.parametrize(
    "auto_confirm,derivatives", [(False, False), (False, True), (True, False), (True, True)],
)
@pytest.mark.parametrize(
    "live_setting",
    [{}, {"exec_live": False}, {"exec_live": None}, {"exec_live": 1}, {"exec_live": "true"}],
    ids=["missing", "false", "null", "integer", "string"],
)
def test_dry_or_ambiguous_legacy_state_never_enables_modes(
    mode_store, auto_confirm, derivatives, live_setting,
):
    stored, store, set_config, _ = mode_store
    legacy = {
        **live_setting,
        "exec_auto_confirm": auto_confirm,
        "exec_derivatives": derivatives,
    }
    stored.update(legacy)
    disabled = {"auto_spot_enabled": False, "auto_futures_enabled": False}

    assert auto_modes.legacy_mode_values(legacy) == disabled
    assert auto_modes.ensure_modes_migrated() == disabled
    assert stored == {**legacy, **disabled}
    store.insert_operator_settings_if_absent.assert_called_once_with(
        disabled, updated_by="auto-mode-migration",
    )
    set_config.assert_called_once_with(disabled)


@pytest.mark.parametrize("auto_confirm,derivatives,expected", LEGACY_CASES)
def test_migration_persists_legacy_mapping_as_one_batch(
    mode_store, auto_confirm, derivatives, expected,
):
    stored, store, set_config, record = mode_store
    legacy = {
        "exec_live": True,
        "exec_auto_confirm": auto_confirm,
        "exec_derivatives": derivatives,
    }
    stored.update(legacy)

    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == {**legacy, **expected}
    store.insert_operator_settings_if_absent.assert_called_once_with(
        expected, updated_by="auto-mode-migration",
    )
    set_config.assert_called_once_with(expected)
    record.assert_called_once_with(
        "auto_modes_migrated",
        legacy_present=["exec_auto_confirm", "exec_derivatives"],
        modes=expected,
    )


@pytest.mark.parametrize("legacy,expected", MISSING_LEGACY_CASES)
def test_migration_missing_legacy_rows_fails_closed(mode_store, legacy, expected):
    stored, store, set_config, _ = mode_store
    stored.update(legacy)

    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == {**legacy, **expected}
    store.insert_operator_settings_if_absent.assert_called_once_with(
        expected, updated_by="auto-mode-migration",
    )
    set_config.assert_called_once_with(expected)


@pytest.mark.parametrize("spot,futures", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("legacy_auto_confirm", [True, False])
@pytest.mark.parametrize("live_setting", [{}, {"exec_live": False}], ids=["missing-live", "dry"])
def test_migration_preserves_existing_mode_rows(
    mode_store, spot, futures, legacy_auto_confirm, live_setting,
):
    stored, store, set_config, record = mode_store
    expected = {"auto_spot_enabled": spot, "auto_futures_enabled": futures}
    initial = {
        **live_setting,
        "exec_auto_confirm": legacy_auto_confirm,
        "exec_derivatives": True,
        **expected,
    }
    stored.update(initial)

    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == initial
    store.insert_operator_settings_if_absent.assert_not_called()
    set_config.assert_called_once_with(expected)
    record.assert_not_called()


@pytest.mark.parametrize(
    "existing_key,existing_value,missing_key",
    [
        ("auto_spot_enabled", True, "auto_futures_enabled"),
        ("auto_spot_enabled", False, "auto_futures_enabled"),
        ("auto_futures_enabled", True, "auto_spot_enabled"),
        ("auto_futures_enabled", False, "auto_spot_enabled"),
    ],
)
def test_migration_fills_only_missing_new_row_with_false(
    mode_store, existing_key, existing_value, missing_key,
):
    stored, store, set_config, _ = mode_store
    legacy = {"exec_auto_confirm": True, "exec_derivatives": True}
    stored.update({**legacy, existing_key: existing_value})
    expected = {existing_key: existing_value, missing_key: False}

    assert auto_modes.ensure_modes_migrated(updated_by="operator") == expected

    assert stored == {**legacy, **expected}
    store.insert_operator_settings_if_absent.assert_called_once_with(
        {missing_key: False}, updated_by="operator",
    )
    set_config.assert_called_once_with(expected)


def test_migration_does_not_enable_invalid_persisted_values(mode_store):
    stored, store, set_config, record = mode_store
    stored.update({"auto_spot_enabled": 1, "auto_futures_enabled": True})
    expected = {"auto_spot_enabled": False, "auto_futures_enabled": True}

    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == {"auto_spot_enabled": 1, "auto_futures_enabled": True}
    store.insert_operator_settings_if_absent.assert_not_called()
    set_config.assert_called_once_with(expected)
    record.assert_not_called()


def test_migration_is_idempotent_even_when_legacy_rows_change(mode_store):
    stored, store, set_config, record = mode_store
    stored.update({"exec_live": True, "exec_auto_confirm": True, "exec_derivatives": True})
    expected = {"auto_spot_enabled": False, "auto_futures_enabled": True}

    assert auto_modes.ensure_modes_migrated() == expected
    stored.update({"exec_auto_confirm": False, "exec_derivatives": False})
    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == {
        "exec_live": True, "exec_auto_confirm": False, "exec_derivatives": False, **expected,
    }
    store.insert_operator_settings_if_absent.assert_called_once_with(
        expected, updated_by="auto-mode-migration",
    )
    assert set_config.call_args_list == [call(expected), call(expected)]
    assert record.call_count == 1


def test_dry_migration_remains_disabled_after_legacy_live_gate_opens(mode_store):
    stored, store, set_config, _ = mode_store
    stored.update({"exec_live": False, "exec_auto_confirm": True, "exec_derivatives": True})
    disabled = {"auto_spot_enabled": False, "auto_futures_enabled": False}

    assert auto_modes.ensure_modes_migrated() == disabled
    stored["exec_live"] = True
    assert auto_modes.ensure_modes_migrated() == disabled

    assert stored == {
        "exec_live": True, "exec_auto_confirm": True, "exec_derivatives": True, **disabled,
    }
    store.insert_operator_settings_if_absent.assert_called_once_with(
        disabled, updated_by="auto-mode-migration",
    )
    assert set_config.call_args_list == [call(disabled), call(disabled)]


def test_migration_does_not_enable_spot_until_explicitly_requested(mode_store):
    stored, store, set_config, _ = mode_store
    legacy = {"exec_live": True, "exec_auto_confirm": True, "exec_derivatives": True}
    stored.update(legacy)
    futures_only = {"auto_spot_enabled": False, "auto_futures_enabled": True}

    assert auto_modes.ensure_modes_migrated() == futures_only
    assert stored == {**legacy, **futures_only}

    auto_modes.set_modes({"auto_spot_enabled": True}, updated_by="operator")
    both_modes = {"auto_spot_enabled": True, "auto_futures_enabled": True}
    assert stored == {**legacy, **both_modes}
    assert auto_modes.ensure_modes_migrated() == both_modes
    store.set_operator_settings.assert_called_once_with(
        {"auto_spot_enabled": True}, updated_by="operator",
    )
    assert set_config.call_args_list == [
        call(futures_only), call({"auto_spot_enabled": True}), call(both_modes),
    ]


def test_migration_reloads_rows_after_a_concurrent_insert(mode_store):
    stored, store, set_config, record = mode_store
    stored.update({"exec_live": True, "exec_auto_confirm": True, "exec_derivatives": True})
    insert_if_absent = store.insert_operator_settings_if_absent.side_effect

    def concurrent_insert(updates, *, updated_by=None):
        stored["auto_spot_enabled"] = True
        return insert_if_absent(updates, updated_by=updated_by)

    store.insert_operator_settings_if_absent.side_effect = concurrent_insert
    expected = {"auto_spot_enabled": True, "auto_futures_enabled": True}

    assert auto_modes.ensure_modes_migrated() == expected

    assert stored == {
        "exec_live": True, "exec_auto_confirm": True, "exec_derivatives": True, **expected,
    }
    set_config.assert_called_once_with(expected)
    record.assert_called_once_with(
        "auto_modes_migrated",
        legacy_present=["exec_auto_confirm", "exec_derivatives"],
        modes=expected,
    )


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"unknown": True},
        {"auto_spot_enabled": True, "unknown": False},
        {"auto_spot_enabled": 1},
        {"auto_futures_enabled": 0},
        {"auto_spot_enabled": "true"},
        {"auto_spot_enabled": None},
        {"auto_spot_enabled": True, "auto_futures_enabled": "false"},
    ],
)
def test_set_modes_rejects_invalid_updates_before_writing(mode_store, updates):
    stored, store, set_config, _ = mode_store

    with pytest.raises(ValueError, match="auto mode values must be booleans"):
        auto_modes.set_modes(updates, updated_by="operator")

    assert stored == {}
    store.set_operator_settings.assert_not_called()
    set_config.assert_not_called()


@pytest.mark.parametrize(
    "updates",
    [
        {"auto_spot_enabled": True},
        {"auto_futures_enabled": False},
        {"auto_spot_enabled": True, "auto_futures_enabled": False},
    ],
)
def test_set_modes_persists_and_applies_updates_as_one_batch(mode_store, updates):
    stored, store, set_config, _ = mode_store

    auto_modes.set_modes(updates, updated_by="operator")

    assert stored == updates
    store.set_operator_settings.assert_called_once_with(updates, updated_by="operator")
    set_config.assert_called_once_with(updates)


def test_set_modes_does_not_apply_config_when_persistence_fails(mode_store):
    stored, store, set_config, _ = mode_store
    store.set_operator_settings.side_effect = RuntimeError("database unavailable")
    updates = {"auto_spot_enabled": True, "auto_futures_enabled": True}

    with pytest.raises(RuntimeError, match="database unavailable"):
        auto_modes.set_modes(updates)

    assert stored == {}
    store.set_operator_settings.assert_called_once_with(updates, updated_by=None)
    set_config.assert_not_called()
