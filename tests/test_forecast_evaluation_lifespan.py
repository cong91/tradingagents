"""Forecast evaluation worker lifecycle at application shutdown."""

import pytest

from server import main

pytestmark = pytest.mark.unit


def test_evaluation_shutdown_timeout_does_not_skip_outcome_worker_stop(monkeypatch, caplog):
    calls = []
    for name in ("_open_state_db", "_apply_engine_settings", "_apply_operator_config"):
        monkeypatch.setattr(main, name, lambda: None)
    monkeypatch.setattr(main.auto_modes, "ensure_modes_migrated", lambda: None)
    monkeypatch.setattr(main.watchlist, "ensure_seeded", lambda: None)
    monkeypatch.setattr(main.data_source_credentials, "push_to_engine", lambda: [])
    monkeypatch.setattr(main.llm_credentials, "push_to_engine", lambda: [])
    monkeypatch.setattr(main.autoloop, "ensure_started", lambda: None)
    monkeypatch.setattr(main.autoloop, "stop", lambda: None)
    for monitor in (main.bracket_monitor, main.position_monitor):
        monkeypatch.setattr(monitor, "start", lambda: None)
        monkeypatch.setattr(monitor, "stop", lambda: None)
    monkeypatch.setattr(main.outcome_ledger, "start", lambda: None)
    monkeypatch.setattr(main.outcome_ledger, "stop", lambda: calls.append("outcome-stopped"))
    monkeypatch.setattr(main.forecast_evaluation, "start_at_boot", lambda: None)

    def timeout():
        raise TimeoutError("poll still running")

    monkeypatch.setattr(main.forecast_evaluation, "stop", timeout)

    manager = main.lifespan(None)
    with pytest.raises(StopIteration):
        manager.__aenter__().send(None)
    with pytest.raises(StopIteration):
        manager.__aexit__(None, None, None).send(None)
    assert calls == ["outcome-stopped"]
    assert "forecast evaluation did not stop" in caplog.text
