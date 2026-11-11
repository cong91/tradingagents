"""test autoloop proposals."""

import pytest

from server import (
    autoloop as autoloop_mod,
    autoloop_candidates,
    autoloop_cycle_records,
    autoloop_futures_execution,
    job_store,
)
from tests.autoloop_runtime_fixtures import (
    _agent_proposal,
    _market_context,
    _no_network as _no_network,
    _NoopDerivatives,
    _server_state as _server_state,
    client as client,
)
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


def test_futures_intent_abstain_is_recorded_without_planning(monkeypatch):
    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    context = _market_context()

    class Derivatives(_NoopDerivatives):
        def plan_order(self, *args, **kwargs):
            pytest.fail("abstain intent must not plan an order")

    monkeypatch.setattr(
        autoloop_cycle_records,
        '_record_futures_cycle_rejection',
        lambda *args, **kwargs: None,
    )
    autoloop_futures_execution._process_futures_candidate(
        Derivatives(), "BTC-USD", "Sell", {}, result,
        context=context, proposal={}, intent="abstain",
    )
    assert result.plan is None
    assert result.reason == "agent_abstained"


@pytest.mark.parametrize(
    ("signal", "intent", "reason"),
    [
        ("Sell", None, "structured_terms_unavailable"),
        ("Underweight", None, "structured_terms_unavailable"),
        ("Sell", "manage_existing", "position_management_only"),
        ("Underweight", "manage_existing", "position_management_only"),
        ("Sell", "unexpected", "invalid_futures_intent: unexpected"),
    ],
)
def test_bearish_rating_never_bypasses_missing_or_non_entry_intent(
    monkeypatch, signal, intent, reason,
):
    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    context = _market_context()

    class Derivatives(_NoopDerivatives):
        def plan_order(self, *args, **kwargs):
            pytest.fail("non-entry intent must never reach futures planning")

    monkeypatch.setattr(
        autoloop_cycle_records,
        '_record_futures_cycle_rejection',
        lambda *args, **kwargs: None,
    )
    autoloop_futures_execution._process_futures_candidate(
        Derivatives(), "BTC-USD", signal, {}, result,
        context=context, proposal=_agent_proposal(), intent=intent,
    )

    assert result.plan is None
    assert result.executed is False
    assert result.reason == reason


def test_futures_entry_without_agent_proposal_is_refused():
    """The decision agents own the futures terms: without a proposal the cycle
    records the refusal instead of falling back to code-chosen leverage."""
    from server import job_store

    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_futures_execution._process_futures_candidate(
        _NoopDerivatives(), "BTC-USD", "Buy", {}, result, context=_market_context(),
        intent="enter",
    )

    assert result.plan is None and result.executed is False
    assert result.reason.startswith("agent_proposal_missing")
    (cycle,) = job_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected" and cycle["phase"] == "proposal"


def test_agent_proposal_is_the_source_of_leverage_and_budgets(monkeypatch):
    """A complete proposal is passed through verbatim: leverage, risk budget,
    margin budget and margin mode all come from the agents, not the config."""
    from tradingagents.execution.sizing import PlannedOrder

    captured = {}

    class Derivatives(_NoopDerivatives):
        def plan_order(self, *args, **kwargs):
            captured.update(kwargs)
            return PlannedOrder(
                ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy",
                market="derivatives", futures_execution_intent="enter", side="buy", quantity=1, cost=100,
                estimated_price=100, stop_loss=90, take_profit=110,
            )

        def audit_plan(self, *args, **kwargs):
            return None

    monkeypatch.setattr(job_store, "claim_futures_cycle", lambda *a, **k: True)
    monkeypatch.setattr(job_store, "update_futures_cycle", lambda *a, **k: None)
    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_futures_execution._process_futures_candidate(
        Derivatives(), "BTC-USD", "Buy", {}, result, context=_market_context(),
        proposal=_agent_proposal(leverage=2, risk_fraction=0.004, margin_fraction=0.15,
                                 margin_mode="cross", stop_loss=90.0, take_profit=110.0),
        intent="enter",
        portfolio=PortfolioContext(cash=1_000.0, currency="USDT"),
    )

    assert captured["leverage"] == 2
    assert captured["risk_fraction"] == 0.004
    assert captured["margin_fraction"] == 0.15
    assert captured["margin_mode"] == "cross"


def test_futures_screening_asks_the_universe_seam_for_contracts_only():
    """The autoloop's futures mode must opt into the contract filter, while a
    seam written before the flag keeps working with two arguments."""
    seen: list[dict] = []

    def with_flag(config, limit, *, futures_only=False):
        seen.append({"limit": limit, "futures_only": futures_only})
        return ["BTC-USD"]

    def without_flag(config, limit):
        seen.append({"limit": limit, "futures_only": None})
        return ["BTC-USD"]

    autoloop_candidates._call_crypto_universe(with_flag, {}, 60, True)
    autoloop_candidates._call_crypto_universe(without_flag, {}, 60, True)
    autoloop_candidates._call_crypto_universe(with_flag, {}, 30, False)

    assert seen == [
        {"limit": 60, "futures_only": True},
        {"limit": 60, "futures_only": None},
        {"limit": 30, "futures_only": False},
    ]


def test_proposal_entry_matching_the_snapshot_mark_survives_a_moved_live_price():
    """The agents are shown one mark and told to make entry consistent with it.

    Validating that entry against a freshly fetched mark instead refused
    proposals the agent made exactly as instructed: on a moving pair the price
    drifts more than the 0.1% tolerance within the seconds the analysis takes.
    """
    proposal = _agent_proposal(entry_price=100.0)

    terms, reason = autoloop_futures_execution._proposal_terms(
        proposal, mark=101.0, reference_mark=100.0, direction="long", config={},
    )

    assert reason is None
    assert terms is not None


def test_proposal_entry_matching_neither_mark_is_still_refused():
    terms, reason = autoloop_futures_execution._proposal_terms(
        _agent_proposal(entry_price=105.0), mark=101.0, reference_mark=100.0,
        direction="long", config={},
    )

    assert terms is None
    assert "non-market entry_price" in (reason or "")


def test_proposal_above_the_hard_caps_is_refused_not_clamped():
    """An over-cap proposal is refused: silently resizing it would execute a
    decision no agent made."""
    for overrides, fragment in (
        ({"leverage": autoloop_futures_execution.MAX_AUTO_LEVERAGE + 1}, "leverage"),
        ({"risk_fraction": 0.5}, "risk_fraction"),
        ({"margin_fraction": 0.9}, "margin_fraction"),
        ({"margin_mode": "portfolio"}, "margin_mode"),
        ({"stop_loss": 120, "take_profit": 80}, "bracket"),
    ):
        terms, reason = autoloop_futures_execution._proposal_terms(
            _agent_proposal(**overrides), mark=100.0, direction="long", config={},
        )
        assert terms is None, overrides
        assert fragment in (reason or ""), (overrides, reason)
