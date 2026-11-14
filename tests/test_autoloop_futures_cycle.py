"""test autoloop futures cycle."""

import json
import threading
from datetime import UTC, datetime

import pytest

import tradingagents.dataflows.screening as screening_mod
from server import (
    autoloop as autoloop_mod,
    autoloop_candles,
    autoloop_futures_context,
    autoloop_futures_execution,
)
from tests.autoloop_runtime_fixtures import (
    _STUB_CASH,
    _STUB_PRICE,
    _agent_proposal,
    _derivatives_cycle_env,
    _FakeGraph,
    _no_network as _no_network,
    _server_state as _server_state,
    _StubDerivVenue,
    _StubOffWhitelistDerivVenue,
    _StubVenue,
    client as client,
)
from tradingagents.dataflows.config import config_key_revision, get_config, set_config
from tradingagents.execution import daily as execution_daily
from tradingagents.execution.auto_authorization import capture_authorization

pytestmark = pytest.mark.unit

_PM_DECISION = (
    "**Rating**: Sell\n\n"
    "**Executive Summary**: Momentum is weak but funding is neutral.\n\n"
    "**Investment Thesis**: Wait for a cleaner breakdown.\n\n"
    "**Futures Execution Intent**: abstain"
)


@pytest.mark.parametrize(
    "spot,futures,expected_markets",
    [
        (True, False, ["spot"]),
        (False, True, ["derivatives"]),
        (True, True, ["spot", "derivatives"]),
        (False, False, []),
    ],
    ids=["spot-only", "futures-only", "both", "both-off"],
)
def test_cycle_dispatches_only_enabled_markets(tmp_path, monkeypatch, spot, futures, expected_markets):
    from tradingagents.execution.bridge import ExchangeBridge

    _derivatives_cycle_env(tmp_path, monkeypatch)
    set_config({"auto_spot_enabled": spot, "auto_futures_enabled": futures})
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )
    graph_modes = []

    def graph_builder(config, analysts):
        graph_modes.append((config["auto_spot_enabled"], config["auto_futures_enabled"]))
        return _FakeGraph("Buy")

    results = autoloop_mod._run_cycle(graph_builder)

    assert all(result.plan is not None for result in results), results
    assert [result.plan.market for result in results] == expected_markets
    assert all(result.executed for result in results)
    assert graph_modes == [
        (market == "spot", market == "derivatives") for market in expected_markets
    ]
    modes = autoloop_mod._last_cycle["modes"]
    assert modes["auto_spot"]["status"] == ("completed" if spot else "disabled")
    assert modes["auto_futures"]["status"] == ("completed" if futures else "disabled")
    assert autoloop_mod._last_cycle["status"] == "completed"


@pytest.mark.parametrize("closed_mode", ["auto_spot_enabled", "auto_futures_enabled"])
def test_cycle_closing_one_mode_during_spot_analysis_preserves_the_other(
    tmp_path, monkeypatch, closed_mode,
):
    from tradingagents.execution.bridge import ExchangeBridge

    _derivatives_cycle_env(tmp_path, monkeypatch)
    set_config({"auto_spot_enabled": True, "auto_futures_enabled": True})
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )
    seen_modes = []

    class SpotGraph(_FakeGraph):
        def propagate(self, *args, **kwargs):
            decision = super().propagate(*args, **kwargs)
            set_config({closed_mode: False})
            return decision

    def graph_builder(config, analysts):
        is_spot = config["auto_spot_enabled"]
        seen_modes.append("spot" if is_spot else "futures")
        return SpotGraph("Buy") if is_spot else _FakeGraph("Buy")

    results = autoloop_mod._run_cycle(graph_builder)

    remaining_market = "derivatives" if closed_mode == "auto_spot_enabled" else "spot"
    assert [result.plan.market for result in results] == [remaining_market]
    assert results[0].executed is True
    assert seen_modes == (["spot", "futures"] if remaining_market == "derivatives" else ["spot"])
    modes = autoloop_mod._last_cycle["modes"]
    assert modes["auto_spot"]["status"] == ("disabled" if closed_mode == "auto_spot_enabled" else "completed")
    assert modes["auto_futures"]["status"] == ("disabled" if closed_mode == "auto_futures_enabled" else "completed")


@pytest.mark.parametrize("market", ["spot", "futures"])
def test_cycle_closing_mode_during_planning_prevents_execution(tmp_path, monkeypatch, market):
    from tradingagents.execution.bridge import ExchangeBridge
    from tradingagents.execution.derivatives import DerivativesExecutor

    _derivatives_cycle_env(tmp_path, monkeypatch)
    set_config({"auto_spot_enabled": market == "spot", "auto_futures_enabled": market == "futures"})
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config, *, authorization: ExchangeBridge(
            config=config, exchange_factory=_StubVenue, authorization=authorization,
        ),
    )
    executor_type = ExchangeBridge if market == "spot" else DerivativesExecutor
    original_plan = executor_type.plan_order
    planned = []
    submitted = []

    def close_after_planning(self, *args, **kwargs):
        plan = original_plan(self, *args, **kwargs)
        planned.append(plan)
        set_config({f"auto_{market}_enabled": False})
        return plan

    def execute_order(*args, **kwargs):
        submitted.append((args, kwargs))
        pytest.fail("disabled mode must not execute an order")

    monkeypatch.setattr(executor_type, "plan_order", close_after_planning)
    monkeypatch.setattr(executor_type, "execute_order", execute_order)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    assert len(planned) == 1
    assert planned[0].side == "buy"
    assert results == []
    assert submitted == []
    assert autoloop_mod._last_cycle["modes"][f"auto_{market}"]["status"] == "disabled"


def test_run_cycle_derivatives_dry_buy_long_bracket(tmp_path, monkeypatch):
    """Auto Futures plans through the
    DerivativesExecutor using the AGENT's leverage (not a code default) and the
    Buy's dry fill carries the LONG bracket (stop below entry, target above).
    The executed row keeps the PM's verbatim decision — HEAD wrote it there and
    the scanner UI renders it on completed entries too."""
    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch)

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph(
            "Buy", proposal=_agent_proposal(leverage=2),
            note="**Rating**: Buy\n\n**Futures Execution Intent**: enter",
        ),
    )

    (btc,) = results
    assert btc.executed is True
    assert btc.plan.market == "derivatives"
    assert btc.plan.leverage == 2  # the proposal's number, below the 3x ceiling
    assert btc.plan.margin_mode == "isolated"
    assert btc.plan.side == "buy"
    assert btc.plan.stop_loss < btc.plan.estimated_price < btc.plan.take_profit

    from server import futures_cycle_store

    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    payload = cycle["payload"]
    assert cycle["status"] == "completed" and cycle["phase"] == "entry"
    assert payload["execution_status"] == "executed"
    assert payload["agent_decision_note"] == (
        "**Rating**: Buy\n\n**Futures Execution Intent**: enter"
    )
    assert payload["pm_decision"]["rating"] == "Buy"
    assert payload["trader_decision"]["futures_execution_intent"] == "enter"

    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    (plan_buy,) = [event for event in events if event.get("phase") == "plan" and event.get("action") == "buy"]
    (dry_buy,) = [event for event in events if event.get("phase") == "execute" and event.get("action") == "buy"]
    assert plan_buy["market"] == "derivatives" and plan_buy["leverage"] == 2
    assert plan_buy["margin_mode"] == "isolated"
    assert dry_buy["mode"] == "dry" and dry_buy["confirmed"] is True
    assert all(event.get("mode") != "live" for event in events)


def test_run_cycle_passes_the_futures_decision_context_to_the_agents(tmp_path, monkeypatch):
    """The agents decide margin mode, leverage and budgets, so the cycle must
    hand them the futures wallet, the closed candles, and the hard limits."""
    _derivatives_cycle_env(tmp_path, monkeypatch)
    seen = {}

    class RecordingGraph(_FakeGraph):
        def propagate(self, ticker, trade_date, asset_type=None, execution_context=""):
            seen["execution_context"] = execution_context
            return super().propagate(ticker, trade_date, asset_type)

    results = autoloop_mod._run_cycle(lambda config, analysts: RecordingGraph("Buy"))

    (btc,) = results
    assert btc.executed is True
    context = seen["execution_context"]
    assert f"Futures equity: {_STUB_CASH:,.4f} USDT" in context
    assert "Closed 1h candle" in context and "Closed 4h candle" in context
    assert "Maximum leverage: 3x" in context
    assert "A FuturesExecutionProposal is required for any entry" in context


def test_run_cycle_derivatives_dry_sell_short_bracket(tmp_path, monkeypatch):
    """A Sell signal opens a SHORT on the derivatives path: the bracket is
    mirrored (stop ABOVE entry, target BELOW, i.e. stop > entry > take) —
    the risk direction the long-only spot path never produces."""
    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.signal == "Sell"
    assert btc.executed is True
    assert btc.plan.market == "derivatives"
    assert btc.plan.side == "sell"  # short opener, no held position required
    # short: stop above entry, take below — the mirror of the long bracket
    assert btc.plan.stop_loss > btc.plan.estimated_price > btc.plan.take_profit
    assert btc.plan.stop_loss > btc.plan.estimated_price > btc.plan.take_profit

    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    (plan_sell,) = [event for event in events if event.get("phase") == "plan" and event.get("action") == "sell"]
    (dry_sell,) = [event for event in events if event.get("phase") == "execute" and event.get("action") == "sell"]
    assert plan_sell["market"] == "derivatives"
    assert dry_sell["mode"] == "dry" and dry_sell["confirmed"] is True


def test_run_cycle_derivatives_doji_candle_is_confirmation_missing(tmp_path, monkeypatch):
    """A doji closed candle can never confirm a tradeable signal in either
    direction: the candidate is skipped with confirmation_missing and the
    rejection is durably recorded with the doji reason for the scanner UI."""
    from server import futures_cycle_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="doji")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.signal == "Buy"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("action") != "buy" for event in events)  # nothing planned on a doji
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["confirmation_reason"] == "doji_candle"
    assert cycle["payload"]["unified_symbol"] == "BTC/USDT:USDT"


def test_run_cycle_abstain_records_pm_decision_note(tmp_path, monkeypatch):
    """The Portfolio Manager's verbatim decision must survive on the rejected
    cycle: it is the only human-legible record of why an analysed candidate
    was refused entry."""
    from server import futures_cycle_store

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph("Sell", intent="abstain", note=_PM_DECISION)
    )

    (btc,) = results
    assert btc.reason == "agent_abstained"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected" and cycle["phase"] == "proposal"
    payload = cycle["payload"]
    assert payload["agent_decision_note"] == _PM_DECISION
    assert payload["confirmation_result"] is True
    # The fixture mirrors the real nodes: with intent=abstain the Trader telemetry
    # carries no enter authorization and no proposal is surfaced.
    assert payload["trader_decision"]["action"] == "Buy"
    assert payload["trader_decision"]["futures_execution_intent"] == "abstain"
    assert payload["trader_decision"]["futures_execution_proposal_present"] is False
    assert payload["pm_decision"]["rating"] == "Sell"
    assert payload["pm_decision"]["futures_execution_intent"] == "abstain"
    assert payload["pm_decision"]["futures_execution_proposal_present"] is False
    assert payload["proposal_validation"]["valid"] is False
    assert payload["execution_status"] == "not_started"


def test_run_cycle_abstain_without_pm_note_omits_the_key(tmp_path, monkeypatch):
    """Older engines (and the stock path) carry no PM prose: the payload must
    simply omit the key rather than store null noise."""
    from server import futures_cycle_store

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph("Sell", intent="abstain")
    )

    (btc,) = results
    assert btc.reason == "agent_abstained"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert "agent_decision_note" not in cycle["payload"]


def test_run_cycle_derivatives_direction_mismatch_is_confirmation_missing(tmp_path, monkeypatch):
    """Short against bullish closed candles is the wrong-direction mirror of
    the Short happy path: confirmation_missing with direction_mismatch, and
    no plan ever reaches the executor. The PM's verbatim decision survives this
    rejection too — the agents HAD decided, the candles refused, and the audit
    row must let an operator tell those two apart."""
    from server import futures_cycle_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bullish")

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph("Sell", intent="enter", note=_PM_DECISION)
    )

    (btc,) = results
    assert btc.signal == "Sell"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("phase") != "plan" for event in events)
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    payload = cycle["payload"]
    assert payload["confirmation_reason"] == "direction_mismatch"
    # The analysis phase recorded tradeable_signal; the confirmation rejection
    # write leaves signal_reason unset and must not null it out.
    assert payload["signal"] == "Sell"
    assert payload["signal_reason"] == "tradeable_signal"
    # The decision layer is not lost behind the confirmation layer.
    assert payload["agent_decision_note"] == _PM_DECISION
    assert payload["trader_decision"]["futures_execution_intent"] == "enter"
    assert payload["pm_decision"]["rating"] == "Sell"
    assert payload["pm_decision"]["futures_execution_intent"] == "enter"
    assert payload["execution_status"] == "not_started"


def test_run_cycle_derivatives_long_against_bearish_is_confirmation_missing(tmp_path, monkeypatch):
    """Long against bearish closed candles is the mirror of the Long happy
    path: confirmation_missing with direction_mismatch, no plan, and the
    durable rejection row carries the reason for the scanner UI."""
    from server import futures_cycle_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.signal == "Buy"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("phase") != "plan" for event in events)
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["confirmation_reason"] == "direction_mismatch"
    assert cycle["payload"]["unified_symbol"] == "BTC/USDT:USDT"


def test_run_cycle_derivatives_off_whitelist_near_plans_and_prices_through_unified_symbol(tmp_path, monkeypatch):
    """NEAR-USD sits outside the spot closed set: the cycle must still plan
    when the venue metadata lists an active USDT-settled NEAR swap, fetch the
    mark price through the RESOLVED unified symbol (never the spot resolver),
    and dry-execute with the protective bracket intact."""
    from server import futures_cycle_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bullish")
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [{"ticker": "NEAR-USD", "asset_type": "crypto"}])

    def build(config):
        from tradingagents.execution.derivatives import DerivativesExecutor

        return DerivativesExecutor(config=config, exchange_factory=_StubOffWhitelistDerivVenue)

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)
    # NEAR marks at 5.0 through the unified symbol: the ATR bracket must sit
    # around THAT price, not the BTC stub price, or the protective check fails.
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices',
                        lambda ticker, direction="long": (4.0, 6.0))
    _StubOffWhitelistDerivVenue.tickers_fetched = []

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (near,) = results
    assert near.signal == "Buy"
    assert near.executed is True
    assert near.plan.market == "derivatives"
    assert near.plan.ccxt_symbol == "NEAR/USDT:USDT"
    assert near.plan.side == "buy"
    assert near.plan.stop_loss < near.plan.estimated_price < near.plan.take_profit
    # The mark price came through the resolved futures symbol, not a spot symbol.
    assert _StubOffWhitelistDerivVenue.tickers_fetched == ["NEAR/USDT:USDT"]

    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    (dry_buy,) = [event for event in events if event.get("phase") == "execute" and event.get("action") == "buy"]
    assert dry_buy["mode"] == "dry" and dry_buy["confirmed"] is True

    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "completed"
    assert cycle["symbol"] == "NEAR/USDT:USDT"
    payload = cycle["payload"]
    assert payload["requested_ticker"] == "NEAR-USD"
    assert payload["unified_symbol"] == "NEAR/USDT:USDT"
    assert payload["snapshot"]["one_hour_direction"] == "bullish"
    assert payload["snapshot"]["four_hour_direction"] == "bullish"
    assert payload["confirmation_result"] is True
    assert payload["confirmation_reason"] == "aligned_direction"
    assert payload["confirmation"] == {"result": True, "reason": "aligned_direction"}
    assert payload["trader_decision"] == {
        "action": "Buy", "futures_execution_intent": "enter",
        "futures_execution_proposal_present": True, "structured_output": True,
    }
    assert payload["pm_decision"]["rating"] == "Buy"
    assert payload["pm_decision"]["futures_execution_intent"] == "enter"
    assert payload["pm_decision"]["futures_execution_status"] == "structured"
    assert payload["pm_decision"]["futures_execution_proposal_present"] is True
    assert payload["effective_caps"]["effective_leverage"] == 3.0
    assert payload["proposal_validation"] == {"valid": True, "reason": None}
    assert payload["risk_guard"]["allowed"] is True
    assert payload["execution_status"] == "executed"
    assert "execution_finished_at" in payload["phase_timestamps"]
    assert cycle["status"] == "completed"
    assert cycle["symbol"] == "NEAR/USDT:USDT"


def test_abandoned_cycle_worker_cannot_persist_a_stale_futures_row(tmp_path, monkeypatch):
    """A timed-out candidate keeps running on its own thread until its blocking
    call returns. Everything it does afterwards belongs to a cycle that already
    closed: persisting a row there is how a stale funnel entry (and, in tests, a
    write into a schema that has since been dropped) appeared. The write gate
    must refuse while order submission is refused.
    """
    from server import futures_cycle_store

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    from tradingagents.execution.derivatives import DerivativesExecutor

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")
    derivatives = DerivativesExecutor(config=get_config(), exchange_factory=_StubDerivVenue)

    cancelled = threading.Event()
    cancelled.set()
    result = autoloop_mod.DailyResult(ticker="BTC-USD")

    # A cycle that already claimed its signal (cycle_id + payload present) goes
    # straight to the terminal rejection write.
    autoloop_futures_execution._process_futures_candidate(
        derivatives, "BTC-USD", "Sell", get_config(), result,
        intent="abstain", cancelled=cancelled,
        cycle_id="futures:BTC/USDT:USDT:stale:1",
        cycle_payload={"requested_ticker": "BTC-USD", "execution_status": "in_progress"},
    )

    assert futures_cycle_store.list_futures_cycles(limit=5) == []


def test_run_cycle_derivatives_planning_failure_preserves_decision_telemetry(
    tmp_path, monkeypatch,
):
    """A venue failure during futures planning must not erase the decision
    audit trail: the failed row keeps the trader/PM typed decisions, the
    confirmed signal, the validated proposal and the failure reason, so the
    funnel can tell 'execution failed' apart from 'agents never decided'."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bullish")

    def broken_plan(self, *args, **kwargs):
        raise RuntimeError("venue down")

    monkeypatch.setattr(DerivativesExecutor, "plan_order", broken_plan)

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph("Buy", note=_PM_DECISION),
    )

    (btc,) = results
    assert btc.executed is False
    assert btc.reason == "RuntimeError: venue down"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "failed"
    assert cycle["phase"] == "execution"
    payload = cycle["payload"]
    assert payload["signal"] == "Buy"
    assert payload["signal_reason"] == "tradeable_signal"
    assert payload["confirmation_result"] is True
    assert payload["trader_decision"] == {
        "action": "Buy", "futures_execution_intent": "enter",
        "futures_execution_proposal_present": True, "structured_output": True,
    }
    assert payload["pm_decision"]["futures_execution_intent"] == "enter"
    assert payload["pm_decision"]["futures_execution_status"] == "structured"
    assert payload["proposal_validation"]["valid"] is True
    assert payload["agent_proposal"]["leverage"] == 3
    assert "rationale" not in payload["agent_proposal"]  # free text never lands
    # the PM's verbatim decision survives a planning failure too (HEAD wrote it
    # on terminal rows; the funnel's planning-failure bucket renders it)
    assert payload["agent_decision_note"] == _PM_DECISION
    assert payload["execution_status"] == "failed"
    assert payload["order_id"] is None
    assert payload["failure_reason"] == "RuntimeError: venue down"
    assert payload["risk_guard"] == {"allowed": None, "halted": None, "reason": "not_called"}
    assert "execution_failed_at" in payload["phase_timestamps"]


def test_risk_guard_decisions_belong_to_their_own_candidate(tmp_path, monkeypatch):
    """The executor's guard-decision list is shared by every candidate in the
    cycle, so a row may only carry the decisions appended FOR its own
    candidate: a candidate whose analysis never reached planning must not
    inherit its predecessor's allowed verdict, and a planning failure must not
    drag earlier decisions into its own row."""
    from server import futures_cycle_store

    _derivatives_cycle_env(tmp_path, monkeypatch)

    def _two_contracts(self):
        contract = {"base": "X", "quote": "USDT", "settle": "USDT", "linear": True,
                    "contract": True, "swap": True, "type": "swap", "active": True}
        markets = {}
        for base, symbol in (("BTC", "BTC/USDT:USDT"), ("ETH", "ETH/USDT:USDT")):
            markets[symbol] = {**contract, "base": base}
        return markets

    class _TwoContractRanking:
        # Futures discovery ranks the venue's contracts by production volume;
        # BTC outranks ETH so the cycle runs them in that order.
        def fetch_tickers(self):
            return {
                "BTC/USDT:USDT": {"quoteVolume": 2_000_000_000_000.0, "last": _STUB_PRICE},
                "ETH/USDT:USDT": {"quoteVolume": 1_000_000_000_000.0, "last": _STUB_PRICE},
            }

    monkeypatch.setattr(_StubDerivVenue, "load_markets", _two_contracts)
    monkeypatch.setattr(screening_mod, "_public_futures_ranking_client",
                        lambda config: _TwoContractRanking())
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: [])
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [
                            {"ticker": ticker, "asset_type": "crypto"} for ticker in crypto
                        ])

    class Graph:
        def propagate(self, ticker, trade_date, asset_type=None):
            if ticker == "ETH-USD":
                # ETH's analysis blows up BEFORE any guard call for ETH: with
                # the shared list the old code copied BTC's verdicts onto
                # ETH's failure row.
                raise RuntimeError("analysis blew up before planning")
            return {
                "trader_decision_telemetry": {
                    "action": "Buy", "futures_execution_intent": "enter",
                    "futures_execution_proposal_present": True,
                    "structured_output": True,
                },
                "pm_decision_telemetry": {
                    "rating": "Buy", "futures_execution_intent": "enter",
                    "futures_execution_status": "structured",
                    "futures_execution_proposal_present": True,
                    "structured_output": True,
                },
                "futures_execution_intent": "enter",
                "futures_execution_status": "structured",
                "futures_execution_proposal": _agent_proposal(),
                "final_trade_decision": "**Rating**: Buy",
            }, "Buy"

    results = autoloop_mod._run_cycle(lambda config, analysts: Graph())

    assert [entry.ticker for entry in results] == ["BTC-USD", "ETH-USD"]
    rows = {row["symbol"]: row for row in futures_cycle_store.list_futures_cycles(limit=10)}
    btc_row = rows["BTC/USDT:USDT"]["payload"]
    eth_row = rows["ETH/USDT:USDT"]["payload"]
    # BTC's own guard verdicts ride its row (plan and/or execute stage)...
    assert btc_row["risk_guard"]["allowed"] is True
    assert len(btc_row["risk_guard_decisions"]) >= 1
    # ...and ETH's row carries none of them: the guard never evaluated ETH,
    # so its failure row must read not_called with an empty decision list.
    assert eth_row["risk_guard_decisions"] == []
    assert eth_row["risk_guard"] == {
        "allowed": None, "halted": None, "reason": "not_called",
    }
    assert eth_row["failure_reason"] == "RuntimeError: analysis blew up before planning"


def test_executed_row_records_the_venue_order_id(tmp_path, monkeypatch):
    """Dry fills legitimately have no venue id (order_id stays None), but the
    live path's order id must land on the row: the funnel's entered bucket and
    the audit trail both key off it."""
    from server import futures_cycle_store

    _derivatives_cycle_env(tmp_path, monkeypatch)

    def live_fill(_executor, _plan, _book, result, execute_guard, **_kwargs):
        result.executed = True
        result.order_id = "venue-123"

    monkeypatch.setattr(execution_daily, '_handle_plan', live_fill)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.executed is True and btc.order_id == "venue-123"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    payload = cycle["payload"]
    assert cycle["status"] == "completed" and cycle["phase"] == "entry"
    assert payload["execution_status"] == "executed"
    assert payload["executed"] is True
    assert payload["order_id"] == "venue-123"


def test_run_cycle_derivatives_awaiting_approval_is_never_entered(tmp_path, monkeypatch):
    """A closed executor authorization leaves a planned order awaiting approval."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor

    _derivatives_cycle_env(tmp_path, monkeypatch)
    original_plan = DerivativesExecutor.plan_order

    def plan_then_require_approval(self, *args, **kwargs):
        plan = original_plan(self, *args, **kwargs)
        # The cycle token remains valid; model a separate executor authorization
        # closing between planning and the approval policy.
        self._bridge._auto_authorization = capture_authorization(
            {"auto_futures_enabled": False}
        )
        return plan

    monkeypatch.setattr(DerivativesExecutor, "plan_order", plan_then_require_approval)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.executed is False
    assert btc.reason and "approval" in btc.reason
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["phase"] == "execution"
    payload = cycle["payload"]
    assert payload["execution_status"] == "awaiting_approval"
    assert payload["executed"] is False
    assert payload["order_id"] is None
    assert "approval" in (payload.get("execution_reason") or "")
    assert payload["risk_guard"]["allowed"] is True
    assert "execution_started_at" in payload["phase_timestamps"]
    summary = futures_cycle_store.futures_cycle_summary()
    assert summary.get("entered", 0) == 0
    assert summary.get("awaiting_approval") == 1


def test_run_cycle_derivatives_risk_refusal_records_risk_rejected(tmp_path, monkeypatch):
    """A RiskGuard refusal of the plan (decision not allowed, side None) is a
    deliberate no: the row records risk_rejected with the guard's reason and
    lands in its own bucket — never entered, never execution_failed."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.execution.risk import RiskDecision, RiskLimits

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    class RefusingGuard:
        limits = RiskLimits(
            max_derivatives_leverage=3.0, max_derivatives_exposure_pct=100.0,
            max_position_pct_per_asset=100.0, max_total_exposure_pct=100.0,
        )

        def check(self, order, portfolio, log_path):
            return RiskDecision(
                allowed=False, halted=False,
                reason="short exposure cap exceeded",
            )

    def build(config):
        return DerivativesExecutor(
            config=config, exchange_factory=_StubDerivVenue, risk_guard=RefusingGuard(),
        )

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.executed is False
    assert btc.reason == "short exposure cap exceeded"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    payload = cycle["payload"]
    assert payload["execution_status"] == "risk_rejected"
    assert payload["executed"] is False
    assert payload["order_id"] is None
    assert payload["risk_guard"]["allowed"] is False
    assert payload["risk_guard"]["reason"] == "short exposure cap exceeded"
    assert payload["execution_reason"] == "short exposure cap exceeded"
    summary = futures_cycle_store.futures_cycle_summary()
    assert summary.get("entered", 0) == 0
    assert summary.get("risk_rejected") == 1
    assert "execution_failed" not in summary


def test_run_cycle_derivatives_risk_guard_halt_is_rejected_not_failed(tmp_path, monkeypatch):
    """A guard HALT raises from plan_order: the terminal row must still read
    rejected/risk_rejected (a deliberate no), not failed — the funnel must be
    able to tell refusals apart from malfunctions."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.execution.risk import RiskDecision, RiskLimits

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    class HaltingGuard:
        limits = RiskLimits(
            max_derivatives_leverage=3.0, max_derivatives_exposure_pct=100.0,
            max_position_pct_per_asset=100.0, max_total_exposure_pct=100.0,
        )

        def check(self, order, portfolio, log_path):
            return RiskDecision(allowed=False, halted=True, reason="daily loss limit hit")

    def build(config):
        return DerivativesExecutor(
            config=config, exchange_factory=_StubDerivVenue, risk_guard=HaltingGuard(),
        )

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.executed is False
    assert "daily loss limit hit" in (btc.reason or "")
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    payload = cycle["payload"]
    assert payload["execution_status"] == "risk_rejected"
    assert payload["executed"] is False
    assert payload["risk_guard"]["halted"] is True
    assert payload["execution_reason"] == "risk guard halted trading: daily loss limit hit"
    summary = futures_cycle_store.futures_cycle_summary()
    assert summary.get("entered", 0) == 0
    assert summary.get("risk_rejected") == 1
    assert summary.get("execution_failed", 0) == 0


def test_run_cycle_derivatives_no_order_plan_records_not_executed(tmp_path, monkeypatch):
    """A plan that never produces an order (sizing refusal, no risk decision)
    records not_executed with the plan's reason — a distinct no-order outcome."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor
    from tradingagents.execution.sizing import PlannedOrder

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    def refuse_plan(self, *args, **kwargs):
        return PlannedOrder(
            ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Sell",
            market="derivatives", side=None, quantity=None, cost=None,
            reason="sizing refused: risk budget below minimum",
        )

    monkeypatch.setattr(DerivativesExecutor, "plan_order", refuse_plan)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.executed is False
    assert btc.reason == "sizing refused: risk budget below minimum"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    payload = cycle["payload"]
    assert payload["execution_status"] == "not_executed"
    assert payload["executed"] is False
    assert payload["order_id"] is None
    assert payload["plan_reason"] == "sizing refused: risk budget below minimum"
    summary = futures_cycle_store.futures_cycle_summary()
    assert summary.get("entered", 0) == 0
    assert summary.get("not_executed") == 1


def test_run_cycle_derivatives_ambiguous_execution_is_uncertain(tmp_path, monkeypatch):
    """An exception at the execution boundary does not prove no order was sent."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor

    _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    def broken_execute(self, *args, **kwargs):
        raise RuntimeError("venue refused connection")

    monkeypatch.setattr(DerivativesExecutor, "execute_order", broken_execute)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.executed is False
    assert btc.reason == "RuntimeError: venue refused connection"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "failed"
    assert cycle["phase"] == "execution"
    payload = cycle["payload"]
    assert payload["execution_status"] == "uncertain"
    assert payload.get("executed") is not False
    assert payload["order_id"] is None
    assert payload["failure_reason"] == "RuntimeError: venue refused connection"
    assert "execution_started_at" in payload["phase_timestamps"]
    assert "execution_failed_at" in payload["phase_timestamps"]
    summary = futures_cycle_store.futures_cycle_summary()
    assert summary.get("entered", 0) == 0
    assert summary.get("interrupted") == 1


def test_run_cycle_derivatives_mode_disabled_before_planning(tmp_path, monkeypatch):
    """Closing Auto Futures during analysis must prevent a new plan or order."""
    from server import futures_cycle_store
    from tradingagents.execution.derivatives import DerivativesExecutor

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch)

    def plan_order(*_args, **_kwargs):
        pytest.fail("disabled futures mode must not plan an order")

    class ClosingGraph(_FakeGraph):
        def propagate(self, *args, **kwargs):
            decision = super().propagate(*args, **kwargs)
            set_config({"auto_futures_enabled": False})
            return decision

    monkeypatch.setattr(DerivativesExecutor, "plan_order", plan_order)
    results = autoloop_mod._run_cycle(lambda config, analysts: ClosingGraph("Buy"))

    assert results == []
    assert autoloop_mod._last_cycle["modes"]["auto_futures"]["status"] == "disabled"
    (cycle,) = futures_cycle_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["execution_status"] == "not_executed"
    assert cycle["payload"]["executed"] is False
    assert "disabled before planning" in cycle["payload"]["execution_reason"]
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines()] if engine_audit.exists() else []
    assert all(event.get("phase") not in {"plan", "execute"} for event in events)


def test_futures_worker_wiring_claims_and_skips_duplicate(tmp_path, monkeypatch):
    from tradingagents.execution.sizing import PlannedOrder

    now = datetime(2026, 10, 7, 8, tzinfo=UTC)
    set_config({"auto_futures_enabled": False})
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return now
        @staticmethod
        def fromtimestamp(value, tz=None):
            return datetime.fromtimestamp(value, tz)
    monkeypatch.setattr(autoloop_futures_context, "datetime", FixedDateTime)
    monkeypatch.setattr(autoloop_futures_execution, "datetime", FixedDateTime)
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda ticker, direction="long": (90.0, 110.0))
    candle_rows = {"1h": [[int((now.timestamp() - 3600) * 1000), 100, 110, 90, 105, 1]], "4h": [[int((now.timestamp() - 4 * 3600) * 1000), 100, 110, 90, 105, 1]]}

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, **kwargs):
            return candle_rows[timeframe]

        def fetch_ticker(self, symbol):
            return {"last": 100.0, "close": 100.0}

    class Bridge:
        config = {"auto_futures_enabled": False}
        def client(self): return Exchange()
        def symbol_for(self, ticker): return "BTC/USDT:USDT"
        def fetch_price(self, ticker): return 100.0
        def sync_portfolio(self): return object()

    class Derivatives:
        _bridge = Bridge()
        config = {"auto_futures_enabled": False}
        def symbol_for(self, ticker): return self._bridge.symbol_for(ticker)
        def audit_plan(self, *args, **kwargs):
            return None
        def plan_order(self, *args, **kwargs):
            return PlannedOrder(ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", market="derivatives", side="buy", quantity=1, cost=100, estimated_price=100, stop_loss=90, take_profit=110)

    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    derivatives = Derivatives()
    # The execution path owns the selected-candle identity: build it from the
    # same venue reads the cycle makes instead of hand-building a context.
    context = autoloop_futures_context._closed_futures_context(derivatives, "BTC-USD")
    assert context is not None
    context["selected_candle"] = autoloop_candles.selected_candle(
        Exchange(), context["unified_symbol"], "4h",
        as_of=datetime.fromisoformat(context["decision_as_of"]),
    )
    context["candle_identity"] = autoloop_candles.candle_identity(context["selected_candle"])
    context["timeframe_revision"] = config_key_revision("auto_futures_timeframe")

    autoloop_futures_execution._process_futures_candidate(
        derivatives, "BTC-USD", "Buy", {}, result, context=context,
        proposal=_agent_proposal(), intent="enter",
    )
    assert result.reason == "awaiting approval"
    duplicate = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_futures_execution._process_futures_candidate(
        derivatives, "BTC-USD", "Buy", {}, duplicate, context=context,
        proposal=_agent_proposal(), intent="enter",
    )
    assert duplicate.reason == "duplicate futures signal skipped"
