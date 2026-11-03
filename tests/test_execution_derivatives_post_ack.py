"""Post-ACK derivatives entry safety with a fake exchange and isolated PostgreSQL."""

import ccxt
import psycopg
import pytest

from server import job_store, postgres
from tests.execution_derivatives_support import (
    VERIFIED_IDENTITY,
    FakeDerivExchange,
    _live_bracket_order,
    _recovery_db as _recovery_db,
    bind_identity,
    live_config,
    live_env,
    make_executor,
    read_audit,
)
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


@pytest.fixture
def entry_setup(tmp_path, monkeypatch):
    exchange = FakeDerivExchange()
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    book = PortfolioContext(cash=1_000.0)
    order = _live_bracket_order(executor, book)
    return exchange, executor, order, book


def _entry_and_exits(exchange):
    entries = []
    exits = []
    for call in exchange.calls:
        if call[0] != "create_order":
            continue
        if call[5] and call[5].get("reduceOnly"):
            exits.append(call)
        else:
            entries.append(call)
    return entries, exits


def _active_entry_intent():
    intents = [
        task for task in job_store.active_bracket_recoveries()
        if task["task_id"].startswith("entry-intent:")
    ]
    assert len(intents) == 1
    return intents[0]


@pytest.mark.parametrize("filled,status", [(0.005, "closed"), (0.002, "open")])
@pytest.mark.parametrize("failure", [
    "intent update", "entry audit", "all audits", "position state",
    "position state false", "position failure audit",
])
def test_post_ack_failure_attempts_confirmed_fill_once(
    entry_setup, monkeypatch, filled, status, failure,
):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "entry-ack", "status": status, "filled": filled, "average": 50_100.0,
    }
    create_order = exchange.create_order
    observed_intents = []

    def create_with_intent_check(symbol, order_type, side, amount, price=None, params=None):
        if params and params.get("clientOrderId", "").startswith("entry-"):
            intent = _active_entry_intent()
            observed_intents.append(intent)
            assert intent["legs"][0]["client_order_id"] == params["clientOrderId"]
        return create_order(symbol, order_type, side, amount, price, params)

    monkeypatch.setattr(exchange, "create_order", create_with_intent_check)
    update = job_store.update_bracket_recovery

    def update_with_failure(task_id, **kwargs):
        if (
            failure == "intent update" and task_id.startswith("entry-intent:")
            and kwargs["status"] == "submission_uncertain"
        ):
            raise psycopg.OperationalError("intent update unavailable")
        return update(task_id, **kwargs)

    monkeypatch.setattr(job_store, "update_bracket_recovery", update_with_failure)
    audit = executor._bridge.audit_plan

    def audit_with_failure(*args, **kwargs):
        if kwargs.get("phase") == "execute" and (
            failure == "all audits"
            or failure == "entry audit" and kwargs.get("action") == "buy"
            or failure == "position failure audit" and kwargs.get("action") == "position_state_failed"
        ):
            raise OSError("audit unavailable")
        return audit(*args, **kwargs)

    monkeypatch.setattr(executor._bridge, "audit_plan", audit_with_failure)
    persist = job_store.persist_futures_entry_state

    def persist_with_failure(*args, **kwargs):
        if failure == "position state":
            raise psycopg.OperationalError("position state unavailable")
        if failure in {"position state false", "position failure audit"}:
            return False
        return persist(*args, **kwargs)

    monkeypatch.setattr(job_store, "persist_futures_entry_state", persist_with_failure)

    with pytest.raises(RuntimeError) as excinfo:
        executor._execute_live(order, True)

    message = str(excinfo.value).lower()
    assert "post-ack" in message
    assert "bracket" in message
    assert "entry-ack" in message
    if status == "open":
        assert "residual exposure unresolved" in message
        assert "not cancelled" in message
    entries, exits = _entry_and_exits(exchange)
    assert len(observed_intents) == len(entries) == 1
    assert [call[4] for call in exits] == [filled, filled]
    assert all(call[3] == "sell" and call[5]["reduceOnly"] is True for call in exits)
    task = job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:entry-ack")
    assert task["quantity"] == pytest.approx(filled)
    assert task["status"] == "submission_uncertain"
    intent = _active_entry_intent()
    assert intent["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]
    assert intent["status"] in {"pending", "submission_uncertain"}


@pytest.mark.parametrize("filled", [None, 0, "nan"])
def test_unknown_fill_and_failed_intent_update_never_use_planned_quantity(
    entry_setup, monkeypatch, filled,
):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {"id": "unknown-ack", "filled": filled, "average": 50_100.0}
    update = job_store.update_bracket_recovery

    def fail_ack_update(task_id, **kwargs):
        if task_id.startswith("entry-intent:") and kwargs["status"] == "submission_uncertain":
            raise psycopg.OperationalError("intent update unavailable")
        return update(task_id, **kwargs)

    monkeypatch.setattr(job_store, "update_bracket_recovery", fail_ack_update)
    with pytest.raises(RuntimeError, match="(?i)fill.*unknown|unknown.*fill"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert exits == []
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]


def test_known_fill_unknown_price_still_attempts_bracket_after_intent_error(entry_setup, monkeypatch):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {"id": "unknown-price", "status": "open", "filled": 0.002}
    update = job_store.update_bracket_recovery

    def fail_ack_update(task_id, **kwargs):
        if task_id.startswith("entry-intent:") and kwargs["status"] == "submission_uncertain":
            raise psycopg.OperationalError("intent update unavailable")
        return update(task_id, **kwargs)

    monkeypatch.setattr(job_store, "update_bracket_recovery", fail_ack_update)
    with pytest.raises(RuntimeError, match="(?i)fill.*unknown|unknown.*fill"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert [call[4] for call in exits] == [0.002, 0.002]
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]


def test_post_ack_database_outage_reports_failed_bracket_attempt(entry_setup, monkeypatch):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "outage-entry", "status": "closed", "filled": 0.005, "average": 50_100.0,
    }
    create_order = exchange.create_order
    place_bracket = executor._place_bracket
    attempts = []

    def fail_connection():
        raise psycopg.OperationalError("database offline")

    def create_then_drop_database(symbol, order_type, side, amount, price=None, params=None):
        response = create_order(symbol, order_type, side, amount, price, params)
        if params and params.get("clientOrderId", "").startswith("entry-"):
            monkeypatch.setattr(postgres, "connect", fail_connection)
        return response

    def record_bracket_attempt(actual_order, confirm, order_id):
        attempts.append(actual_order.quantity)
        return place_bracket(actual_order, confirm, order_id)

    monkeypatch.setattr(exchange, "create_order", create_then_drop_database)
    monkeypatch.setattr(executor, "_place_bracket", record_bracket_attempt)
    original_connect = postgres.connect
    try:
        with pytest.raises(RuntimeError) as excinfo:
            executor._execute_live(order, True)
    finally:
        monkeypatch.setattr(postgres, "connect", original_connect)

    assert "bracket" in str(excinfo.value).lower()
    assert "fail" in str(excinfo.value).lower()
    assert attempts == [0.005]
    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert exits == []
    assert job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:outage-entry") is None
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]
    with pytest.raises(PermissionError, match="existing active task"):
        executor._execute_live(order, True)
    assert len(_entry_and_exits(exchange)[0]) == 1


def test_scale_out_audit_failure_still_attempts_single_bracket(tmp_path, monkeypatch):
    exchange = FakeDerivExchange(entry_response={
        "id": "scale-out-ack", "status": "closed", "filled": 0.005, "average": 50_100.0,
    })
    bind_identity(monkeypatch, VERIFIED_IDENTITY)
    executor = make_executor(
        tmp_path, monkeypatch,
        config=live_config(
            risk_max_derivatives_exposure_pct=100.0,
            risk_derivatives_scale_out_enabled=True,
            risk_derivatives_scale_out_first_fraction=0.5,
            risk_derivatives_scale_out_second_fraction=0.5,
        ),
        factory=lambda: exchange, env=live_env(),
    )
    order = _live_bracket_order(executor, PortfolioContext(cash=1_000.0))
    audit = executor._bridge.audit_plan

    def fail_scale_out_audit(*args, **kwargs):
        if kwargs.get("action") == "scale_out_refused":
            raise OSError("audit unavailable")
        return audit(*args, **kwargs)

    monkeypatch.setattr(executor._bridge, "audit_plan", fail_scale_out_audit)
    with pytest.raises(RuntimeError, match="post-ACK"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert [call[4] for call in exits] == [0.005, 0.005]
    assert job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:scale-out-ack") is not None
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]


def test_full_entry_never_marks_intent_completed_before_bracket_verification(entry_setup, monkeypatch):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "completed-ack", "status": "closed", "filled": 0.005, "average": 50_100.0,
    }
    update = job_store.update_bracket_recovery
    intent_statuses = []

    def record_intent_update(task_id, **kwargs):
        if task_id.startswith("entry-intent:"):
            intent_statuses.append(kwargs["status"])
        return update(task_id, **kwargs)

    monkeypatch.setattr(job_store, "update_bracket_recovery", record_intent_update)
    with pytest.raises(RuntimeError, match="(?i)bracket.*unverified"):
        executor._execute_live(order, True)
    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert [call[4] for call in exits] == [0.005, 0.005]
    assert intent_statuses == ["submission_uncertain"]
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]


def test_post_ack_error_fences_retry_without_duplicate_orders(entry_setup, monkeypatch):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "retry-ack", "status": "closed", "filled": 0.005, "average": 50_100.0,
    }
    audit = executor._bridge.audit_plan

    def fail_entry_audit(*args, **kwargs):
        if kwargs.get("phase") == "execute" and kwargs.get("action") == "buy":
            raise OSError("audit unavailable")
        return audit(*args, **kwargs)

    monkeypatch.setattr(executor._bridge, "audit_plan", fail_entry_audit)
    with pytest.raises(RuntimeError, match="post-ACK"):
        executor._execute_live(order, True)
    calls_before_retry = _entry_and_exits(exchange)
    with pytest.raises(PermissionError, match="existing active task"):
        executor._execute_live(order, True)
    assert _entry_and_exits(exchange) == calls_before_retry
    assert len(calls_before_retry[0]) == 1
    assert len(calls_before_retry[1]) == 2


@pytest.mark.parametrize(("filled", "status", "average"), [
    (0.002, "open", 50_100.0),
    (0.002, "open", None),
    (None, "open", 50_100.0),
    (0.002, None, 50_100.0),
])
def test_unverified_account_keeps_residual_active_without_cancel_or_retry(
    entry_setup, monkeypatch, tmp_path, filled, status, average,
):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "partial-entry", "status": status, "filled": filled, "average": average,
    }
    with pytest.raises(RuntimeError, match="(?i)residual.*unresolved"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert not [call for call in exchange.calls if call[0] in {"cancel_order", "fetch_order"}]
    if filled is None:
        assert exits == []
    else:
        assert [call[4] for call in exits] == [filled, filled]
    intent = _active_entry_intent()
    assert intent["status"] in {"pending", "submission_uncertain"}
    assert intent["legs"][0]["order_id"] == "partial-entry"
    assert intent["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]
    assert "residual" in intent["last_error"].lower()
    assert "unresolved" in intent["last_error"].lower()
    warnings = [event for event in read_audit(tmp_path) if event["action"] == "entry_partial_or_unknown"]
    assert warnings and "residual" in warnings[-1]["reason"].lower()
    assert "unresolved" in warnings[-1]["reason"].lower()
    assert "submitted" not in warnings[-1]["reason"].lower()

    reloaded = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    with pytest.raises(PermissionError, match="existing active task"):
        reloaded._execute_live(order, True)
    assert _entry_and_exits(exchange) == (entries, exits)
    assert not [call for call in exchange.calls if call[0] in {"cancel_order", "fetch_order"}]


def test_partial_database_outage_reports_unresolved_without_cancel(entry_setup, monkeypatch):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "outage-partial", "status": "open", "filled": 0.002, "average": 50_100.0,
    }
    create_order = exchange.create_order
    original_connect = postgres.connect

    def fail_connection():
        raise psycopg.OperationalError("database offline")

    def create_then_drop_database(symbol, order_type, side, amount, price=None, params=None):
        response = create_order(symbol, order_type, side, amount, price, params)
        if params and params.get("clientOrderId", "").startswith("entry-"):
            monkeypatch.setattr(postgres, "connect", fail_connection)
        return response

    monkeypatch.setattr(exchange, "create_order", create_then_drop_database)
    try:
        with pytest.raises(RuntimeError, match="(?i)residual.*unresolved") as excinfo:
            executor._execute_live(order, True)
    finally:
        monkeypatch.setattr(postgres, "connect", original_connect)

    assert "bracket attempt failed or unverified" in str(excinfo.value)
    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert exits == []
    assert not [call for call in exchange.calls if call[0] in {"cancel_order", "fetch_order"}]
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]


@pytest.mark.parametrize(("bracket_error", "expected_status", "expected_exits"), [
    (None, "submission_uncertain", 2),
    (ccxt.ExchangeError("leg rejected"), "pending", 1),
    (ccxt.NetworkError("leg timeout"), "submission_uncertain", 1),
])
def test_full_entry_with_unverified_bracket_keeps_intent_active_and_fences_retry(
    entry_setup, monkeypatch, tmp_path, bracket_error, expected_status, expected_exits,
):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "full-unverified", "status": "closed", "filled": 0.005, "average": 50_100.0,
    }
    exchange.bracket_error = bracket_error

    with pytest.raises(RuntimeError, match="(?i)bracket.*unverified"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert len(exits) == expected_exits
    task = job_store.load_bracket_recovery(f"bracket:{order.ccxt_symbol}:full-unverified")
    assert task["status"] == expected_status
    assert task["quantity"] == pytest.approx(0.005)
    intent = _active_entry_intent()
    assert intent["status"] in {"pending", "submission_uncertain"}
    assert intent["legs"][0]["order_id"] == "full-unverified"
    assert intent["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]
    assert "unverified" in intent["last_error"].lower()

    reloaded = make_executor(
        tmp_path, monkeypatch, config=live_config(risk_max_derivatives_exposure_pct=100.0),
        factory=lambda: exchange, env=live_env(),
    )
    with pytest.raises(PermissionError, match="existing active task"):
        reloaded._execute_live(order, True)
    assert _entry_and_exits(exchange) == (entries, exits)


def test_full_entry_with_existing_bracket_task_stays_unresolved(entry_setup):
    exchange, executor, order, _ = entry_setup
    exchange.entry_response = {
        "id": "existing-bracket", "status": "closed", "filled": 0.005, "average": 50_100.0,
    }
    task_id = f"bracket:{order.ccxt_symbol}:existing-bracket"
    assert job_store.create_bracket_recovery(
        task_id, symbol=order.ccxt_symbol, entry_order_id="existing-bracket",
        close_side="sell", quantity=0.005, legs=[], now="2026-09-28T12:00:00+00:00",
    )
    job_store.update_bracket_recovery(
        task_id, status="cancelled", placed_ids=[], attempts=0,
        last_error=None, now="2026-09-28T12:00:00+00:00",
    )

    with pytest.raises(RuntimeError, match="(?i)bracket.*unverified"):
        executor._execute_live(order, True)

    entries, exits = _entry_and_exits(exchange)
    assert len(entries) == 1
    assert exits == []
    assert _active_entry_intent()["legs"][0]["client_order_id"] == entries[0][5]["clientOrderId"]
    assert job_store.load_bracket_recovery(task_id)["status"] == "cancelled"


def test_public_entry_remains_fail_closed(entry_setup, monkeypatch):
    exchange, executor, order, book = entry_setup
    # Without verified account identity the public entry refuses closed.
    bind_identity(monkeypatch, None)
    with pytest.raises(PermissionError):
        executor.execute_order(order, confirm=True, portfolio=book)
    assert _entry_and_exits(exchange) == ([], [])
