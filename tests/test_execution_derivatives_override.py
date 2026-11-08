"""Executor-level soft-cap override tests: plan and execute stages.

Split from ``test_execution_derivatives.py`` because that module's autouse
``_recovery_db`` fixture PostgreSQL-gates every test (``postgres_schema``
skips without ``TRADINGAGENTS_TEST_DATABASE_URL``, so in a default local or
CI run these tests never executed there). The override path never touches
``job_store``: plan_order and the dry-run execute path run entirely on the
fake exchange and the risk guard. The exchange is a fake injected through
``exchange_factory``; no real client is built and no socket is opened.
"""

import pytest

from tests.execution_derivatives_support import gates_config, make_executor, read_audit
from tradingagents.portfolio import PortfolioContext, Position

pytestmark = pytest.mark.unit


def _override_book() -> PortfolioContext:
    """Book with a 7.4% derivatives position on another asset (equity 1000)."""
    return PortfolioContext(
        cash=1_000.0, currency="USDT",
        positions=[Position(
            ticker="SOL-USD", quantity=None, notional=74.0,
            notional_currency="USDT", market="derivatives", market_symbol="SOL-USD",
        )],
    )


def _override_executor(tmp_path, monkeypatch, *, budget=1):
    return make_executor(
        tmp_path, monkeypatch,
        config=gates_config(
            risk_max_derivatives_exposure_pct=25.0,
            risk_max_derivatives_exposure_hard_pct=35.0,
            risk_derivatives_override_max_per_day=budget,
        ),
        env={"TRADINGAGENTS_EXEC_DERIVATIVES": "true"},
    )


def _plan_override_order(executor, book):
    """Above-soft-cap entry with every override condition met: R 3.16,
    risk-at-stop 0.95%, funding favorable, budget 1."""
    return executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0,
        stop_loss=48_100.0, take_profit=56_000.0,
        futures_execution_intent="enter",
        confirmation_ok=True, funding_rate=-0.0001,
    )


def test_plan_order_carries_override_evidence_to_the_guard(tmp_path, monkeypatch):
    """confirmation_ok/funding_rate ride the plan into the risk guard: the plan
    passes ABOVE the soft cap and the recorded decision carries override_used."""
    executor = _override_executor(tmp_path, monkeypatch)
    order = _plan_override_order(executor, _override_book())
    assert order.confirmation_ok is True
    assert order.funding_rate == pytest.approx(-0.0001)
    assert order.override_reservation_id
    assert order.side == "buy"
    (decision,) = executor.risk_guard_decisions
    assert decision["stage"] == "plan"
    assert decision["allowed"] is True
    assert decision["override_used"] is True


def test_plan_order_without_confirmation_evidence_refuses_the_override(tmp_path, monkeypatch):
    """The same above-soft-cap plan without confirmation_ok=True is refused,
    not resized -- the override never consumes a budget unit."""
    executor = _override_executor(tmp_path, monkeypatch)
    book = _override_book()
    order = executor.plan_order(
        "BTC-USD", "Buy", book, price=50_000.0,
        stop_loss=48_100.0, take_profit=56_000.0,
        futures_execution_intent="enter",
        confirmation_ok=False, funding_rate=-0.0001,
    )
    assert order.confirmation_ok is False
    assert order.side is None
    (decision,) = executor.risk_guard_decisions
    assert decision["stage"] == "plan"
    assert decision["allowed"] is False
    assert decision["override_used"] is False
    assert "confirmation_ok is not True" in decision["reason"]


def test_execute_recheck_does_not_self_count_the_plan_time_override_event(
    tmp_path, monkeypatch,
):
    """budget=1 admits exactly one override order per UTC day.

    The plan approves and charges the budget once (one audit event). The
    execute-time re-check (fail_closed=True, same audit log) re-verifies
    eligibility WITHOUT re-counting this order's own plan-time event --
    before the fix it refused with "daily override budget exhausted (1/1)"
    and no override order was ever sent."""
    book = _override_book()
    executor = _override_executor(tmp_path, monkeypatch, budget=1)
    order = _plan_override_order(executor, book)
    assert order.side == "buy"

    # dry-run send: the execute re-check approves on the same audit log
    sent = executor.execute_order(order, confirm=True, portfolio=book)
    assert sent is None
    stages = [
        (decision["stage"], decision["allowed"], decision["override_used"])
        for decision in executor.risk_guard_decisions
    ]
    assert stages == [("plan", True, True), ("execute", True, True)]
    used = [
        event for event in read_audit(tmp_path)
        if event.get("event") == "derivatives_override_used"
    ]
    assert len(used) == 1
    assert used[0]["reservation_id"] == order.override_reservation_id

    # the single budget unit is spent by that order: a second plan refuses
    second = _plan_override_order(executor, book)
    assert second.side is None
    assert "daily override budget exhausted (1/1)" in second.reason


def test_execute_rejects_override_order_without_budget_reservation(tmp_path, monkeypatch):
    """The execute-time re-check cannot authorize a crafted order by
    bypassing the plan-time budget reservation."""
    from tradingagents.execution.sizing import PlannedOrder

    executor = _override_executor(tmp_path, monkeypatch, budget=1)
    order = PlannedOrder(
        ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy",
        side="buy", quantity=0.005, estimated_price=50_000.0, cost=250.0,
        market="derivatives", leverage=1.0, stop_loss=48_100.0,
        take_profit=56_000.0, futures_execution_intent="enter",
        confirmation_ok=True, funding_rate=-0.0001,
    )
    with pytest.raises(PermissionError, match="plan-time budget reservation"):
        executor.execute_order(
            order, confirm=True, portfolio=_override_book(),
        )
