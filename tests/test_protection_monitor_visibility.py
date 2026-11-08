"""Blocked protection polls surface operational risk without exposing identifiers."""

import logging
from types import SimpleNamespace

import pytest

from server import bracket_monitor, position_monitor
from tradingagents.execution import derivatives
from tradingagents.execution.futures_operation_lock import OperationLockBusyError

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
    "bracket_ambiguous",
    "entry_order_unavailable",
    "entry_order_unverified",
    "position_scope_unknown",
    "open_position_provenance_ambiguous",
    "bracket_evidence_incomplete",
    "account_identity_unknown",
    "lock_or_database_lost",
    "some_new_unresolved_status",
])
def test_entry_poll_reports_every_unresolved_status_with_symbol(monkeypatch, caplog, status):
    symbol = "BTC/USDT:USDT"
    task_id = f"entry-intent:{symbol}:sensitive-order-id"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "submission_uncertain", "symbol": symbol},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=lambda _task_id: status,
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        outcomes = bracket_monitor.poll_active_brackets()

    assert outcomes == [{"task_id": task_id, "status": status}]
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert status in record.message
    assert symbol in record.message
    assert "reconciliation" in record.message
    assert "verify" in record.message.lower()
    assert task_id not in caplog.text
    assert "sensitive-order-id" not in caplog.text


@pytest.mark.parametrize("status", ["completed", "already_reconciled"])
def test_successful_entry_poll_does_not_report_deferred_reconciliation(monkeypatch, caplog, status):
    task_id = "entry-intent:BTC/USDT:USDT:sensitive-order-id"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "submission_uncertain", "symbol": "BTC/USDT:USDT"},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=lambda _task_id: status,
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        assert bracket_monitor.poll_active_brackets() == [{"task_id": task_id, "status": status}]

    assert not caplog.records


def test_entry_poll_reports_each_symbol_without_order_identifiers(monkeypatch, caplog):
    tasks = [
        {"task_id": "entry-intent:BTC/USDT:USDT:secret1", "symbol": "BTC/USDT:USDT",
         "status": "submission_uncertain"},
        {"task_id": "entry-intent:ETH/USDT:USDT:secret2", "symbol": "ETH/USDT:USDT",
         "status": "submission_uncertain"},
    ]
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: tasks)
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=lambda _task_id: "bracket_ambiguous",
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        bracket_monitor.poll_active_brackets()

    assert len(caplog.records) == 2
    assert all(record.levelno == logging.ERROR for record in caplog.records)
    assert all("bracket_ambiguous" in record.message for record in caplog.records)
    assert "BTC/USDT:USDT" in caplog.text
    assert "ETH/USDT:USDT" in caplog.text
    assert "secret1" not in caplog.text
    assert "secret2" not in caplog.text


def test_entry_poll_sanitizes_exception_but_preserves_returned_outcome(monkeypatch, caplog):
    symbol = "BTC/USDT:USDT"
    task_id = f"entry-intent:{symbol}:sensitive-order-id"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "submission_uncertain", "symbol": symbol},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})

    def fail(_task_id):
        raise RuntimeError("api-key=private-credential")

    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=fail,
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        outcomes = bracket_monitor.poll_active_brackets()

    assert outcomes == [{"task_id": task_id, "status": "error:RuntimeError:api-key=private-credential"}]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert "RuntimeError" in caplog.text
    assert symbol in caplog.text
    assert "private-credential" not in caplog.text
    assert "sensitive-order-id" not in caplog.text


def test_entry_poll_sanitizes_error_status_details(monkeypatch, caplog):
    symbol = "BTC/USDT:USDT"
    task_id = f"entry-intent:{symbol}:sensitive-order-id"
    status = "error:RuntimeError:api-key=private-credential"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "submission_uncertain", "symbol": symbol},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})
    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=lambda _task_id: status,
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        assert bracket_monitor.poll_active_brackets() == [{"task_id": task_id, "status": status}]

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    assert "RuntimeError" in caplog.text
    assert symbol in caplog.text
    assert "private-credential" not in caplog.text
    assert "sensitive-order-id" not in caplog.text


@pytest.mark.parametrize("busy", ["lock_busy", OperationLockBusyError("private-credential")])
def test_entry_poll_reports_lock_contention_as_benign_warning(monkeypatch, caplog, busy):
    symbol = "BTC/USDT:USDT"
    task_id = f"entry-intent:{symbol}:sensitive-order-id"
    monkeypatch.setattr(bracket_monitor.job_store, "active_bracket_recoveries", lambda: [
        {"task_id": task_id, "status": "submission_uncertain", "symbol": symbol},
    ])
    monkeypatch.setattr(bracket_monitor, "get_config", lambda: {})

    def monitor(_task_id):
        if isinstance(busy, Exception):
            raise busy
        return busy

    monkeypatch.setattr(derivatives, "DerivativesExecutor", lambda **_kwargs: SimpleNamespace(
        effective_mode=lambda: "dry", monitor_bracket=monitor,
    ))

    with caplog.at_level(logging.WARNING, logger=bracket_monitor.__name__):
        bracket_monitor.poll_active_brackets()

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "lock_busy" in caplog.text
    assert symbol in caplog.text
    assert "private-credential" not in caplog.text
    assert "sensitive-order-id" not in caplog.text


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
