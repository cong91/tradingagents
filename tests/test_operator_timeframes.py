from types import SimpleNamespace

import pytest

from server import main, operator_config
from tradingagents.dataflows.config import get_config, set_config

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["1m", "15m", "1h", "4h"])
def test_operator_timeframe_accepts_only_declared_choices(value):
    assert operator_config._coerce("auto_spot_timeframe", value) == value
    assert operator_config._coerce("auto_futures_timeframe", value) == value


@pytest.mark.parametrize("value", [None, "5m", "1M", True, 1])
def test_operator_timeframe_rejects_invalid_value(value):
    with pytest.raises(ValueError):
        operator_config._coerce("auto_futures_timeframe", value)


def test_operator_timeframes_reload_from_persisted_values(monkeypatch):
    stored = {"auto_spot_timeframe": "1m", "auto_futures_timeframe": "15m"}
    monkeypatch.setattr(main.control_store, "load_operator_settings", lambda: dict(stored))
    set_config({"auto_spot_timeframe": "4h", "auto_futures_timeframe": "4h"})
    main._apply_operator_config()
    assert {key: get_config()[key] for key in stored} == stored
    assert get_config().get("auto_spot_enabled") is not True
    assert get_config().get("auto_futures_enabled") is not True


def test_operator_timeframes_save_as_one_batch_without_arming_modes(monkeypatch):
    stored = {}
    monkeypatch.setattr(operator_config, "json_body", lambda _request: _body())
    monkeypatch.setattr(operator_config.control_store, "set_operator_settings", lambda updates, **_: stored.update(updates))
    monkeypatch.setattr(operator_config.settings_events, "publish_change", lambda *_: None)
    monkeypatch.setattr(operator_config.audit_log, "record", lambda *_a, **_k: None)
    monkeypatch.setattr(operator_config, "_payload", lambda: {})
    coroutine = operator_config.put_operator_config(SimpleNamespace())
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert stored == {"auto_spot_timeframe": "1m", "auto_futures_timeframe": "15m"}
    assert all(get_config()[key] == value for key, value in stored.items())
    assert get_config().get("auto_spot_enabled") is not True
    assert get_config().get("auto_futures_enabled") is not True


async def _body():
    return {"settings": {"auto_spot_timeframe": "1m", "auto_futures_timeframe": "15m"}}
