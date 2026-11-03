"""Blocked protection polls surface operational risk without exposing identifiers."""

import logging
from types import SimpleNamespace

import pytest

from server import bracket_monitor, position_monitor
from tradingagents.execution import derivatives

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", [
    "account_identity_unknown", "account_identity_mismatch",
    "entry_reconciliation_required", "venue_mismatch",
])
def test_bracket_poll_reports_deferred_reconciliation(monkeypatch, caplog, status):
    task_id = "sensitive-task-id"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "monitoring"},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=lambda _task_id: status,
    ))

    with caplog.at_level(logging.ERROR, logger=bracket_monitor.__name__):
        outcomes = bracket_monitor.poll_active_brackets()

    assert outcomes == [{"task_id": task_id, "status": status}]
    assert status in caplog.text
    assert "reconciliation" in caplog.text
    assert task_id not in caplog.text


@pytest.mark.parametrize("status", [
    "account_identity_unknown", "account_identity_mismatch",
    "missing_bracket_link", "missing_bracket_task",
    "venue_mismatch", "bracket_identity_mismatch", "position_unknown",
])
def test_position_poll_reports_deferred_protection(monkeypatch, caplog, status):
    position_key = "sensitive-position-key"
    monkeypatch.setattr(position_monitor.job_store, "active_position_states", lambda: [
        {"position_key": position_key},
    ])
    monkeypatch.setattr(position_monitor, "get_config", lambda: {
        "exec_derivatives": True, "exec_live": True,
        "risk_derivatives_profit_protection_enabled": True,
    })
    monkeypatch.setattr(position_monitor, "_env_flag", lambda _name: True)
    monkeypatch.setattr(position_monitor, "_build_executor", lambda _config: SimpleNamespace(
        _bridge=object(),
    ))
    monkeypatch.setattr(position_monitor, "manage_active_positions", lambda *_args, **_kwargs: [
        {"position_key": position_key, "status": status},
    ])

    with caplog.at_level(logging.ERROR, logger=position_monitor.__name__):
        outcomes = position_monitor.poll_active_positions()

    assert outcomes == [{"position_key": position_key, "status": status}]
    assert status in caplog.text
    assert "protection" in caplog.text
    assert position_key not in caplog.text


def test_successful_position_poll_does_not_report_deferred_protection(monkeypatch, caplog):
    monkeypatch.setattr(position_monitor.job_store, "active_position_states", lambda: [
        {"position_key": "position"},
    ])
    monkeypatch.setattr(position_monitor, "get_config", lambda: {
        "exec_derivatives": True, "exec_live": True,
        "risk_derivatives_profit_protection_enabled": True,
    })
    monkeypatch.setattr(position_monitor, "_env_flag", lambda _name: True)
    monkeypatch.setattr(position_monitor, "_build_executor", lambda _config: SimpleNamespace(
        _bridge=object(),
    ))
    monkeypatch.setattr(position_monitor, "manage_active_positions", lambda *_args, **_kwargs: [
        {"position_key": "position", "status": "unchanged"},
    ])

    with caplog.at_level(logging.ERROR, logger=position_monitor.__name__):
        assert position_monitor.poll_active_positions() == [
            {"position_key": "position", "status": "unchanged"},
        ]

    assert not caplog.records
