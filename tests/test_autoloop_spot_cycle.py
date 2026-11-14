"""test autoloop spot cycle."""

import json
import threading

import pytest

import tradingagents.dataflows.screening as screening_mod
from server import (
    autoloop as autoloop_mod,
    autoloop_api,
    autoloop_candidates,
    autoloop_execution,
    autoloop_futures_context,
    control_store,
)
from tests.autoloop_runtime_fixtures import (
    _STOP,
    _TAKE,
    _derivatives_cycle_env,
    _FakeGraph,
    _no_network as _no_network,
    _server_state as _server_state,
    _StubDerivVenue,
    _StubVenue,
    client as client,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.execution.bridge import ExchangeBridge

pytestmark = pytest.mark.unit


def test_run_cycle_auto_confirms_dry_buy_with_bracket(tmp_path, monkeypatch):
    """Auto Spot executes a dry Buy with the protective bracket attached."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": False, "exec_log_path": str(engine_audit)})

    monkeypatch.setattr(screening_mod, "crypto_universe", lambda config, top_n: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: ["NVDA"])
    monkeypatch.setattr(screening_mod, "screen_universe", lambda crypto, stocks, date, top_n: [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "NVDA", "asset_type": "stock"},
    ])
    monkeypatch.setattr(screening_mod, "atr_targets_signed",
                        lambda ticker, date, direction: (_STOP, _TAKE))

    # Do not forward managed authorization: this case explicitly exercises dry fills.
    def build(config):
        bridge = ExchangeBridge(config=config, exchange_factory=_StubVenue)
        assert bridge.effective_mode() == "dry"
        return bridge

    monkeypatch.setattr(autoloop_mod, "_build_bridge", build)

    built: list[tuple[tuple[str, ...], _FakeGraph]] = []

    def graph_builder(config, analysts):
        graph = _FakeGraph("Buy")
        built.append((tuple(analysts), graph))
        return graph

    results = autoloop_mod._run_cycle(graph_builder)

    assert [result.ticker for result in results] == ["BTC-USD", "NVDA"]
    btc, nvda = results
    assert btc.signal == "Buy"
    assert btc.executed is True  # the engine policy executed the dry fill itself
    assert btc.plan.stop_loss == _STOP and btc.plan.take_profit == _TAKE
    assert nvda.signal == "Buy"
    assert nvda.executed is False
    assert nvda.plan is None and "advisory only" in nvda.reason  # stock candidates never plan
    # analyst sets per asset type, exactly like server/daily._analyze
    assert built[0][0] == ("market", "social", "news")
    assert built[1][0] == ("market", "social", "news", "fundamentals")

    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    plan_buys = [event for event in events if event.get("phase") == "plan" and event.get("action") == "buy"]
    (dry_buy,) = [event for event in events if event.get("phase") == "execute" and event.get("action") == "buy"]
    assert plan_buys  # the plan line was audited before the policy decided
    assert dry_buy["mode"] == "dry" and dry_buy["confirmed"] is True
    assert dry_buy["stop_loss"] == _STOP and dry_buy["take_profit"] == _TAKE
    assert all(event.get("mode") != "live" for event in events)  # never goes live on its own

    trail = "\n".join(json.dumps(e) for e in control_store.load_server_audit())
    assert "autoloop_cycle_started" in trail
    assert "autoloop_cycle_completed" in trail

    last = autoloop_mod._last_cycle
    assert last["status"] == "completed"
    assert last["candidates"] == ["BTC-USD", "NVDA"]
    assert last["results"][0]["executed"] is True
    assert last["results"][1]["executed"] is False


def test_managed_spot_cycle_submits_only_to_fake_venue(tmp_path, monkeypatch):
    class RecordingVenue(_StubVenue):
        apiKey = "test-key"
        secret = "test-secret"

        def __init__(self):
            self.orders = []

        def amount_to_precision(self, symbol, amount):
            return f"{float(amount):.6f}"

        def create_order(self, symbol, order_type, side, amount):
            self.orders.append((symbol, order_type, side, amount))
            return {"id": "fake-spot-order"}

        def market_id(self, symbol):
            return symbol.replace("/", "")

    venue = RecordingVenue()
    audit_file = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": False, "exec_log_path": str(audit_file)})
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(screening_mod, "screen_universe", lambda *_args: [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
    ])
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda *_args: (None, None))
    bridges = []

    def build(config, *, authorization):
        bridge = ExchangeBridge(config=config, exchange_factory=lambda: venue, authorization=authorization)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(autoloop_mod, "_build_bridge", build)

    (result,) = autoloop_mod._run_cycle(lambda *_args: _FakeGraph("Buy"))

    assert bridges[0].effective_mode() == "live"
    assert result.executed is True
    assert result.order_id == "fake-spot-order"
    assert len(venue.orders) == 1
    assert venue.orders[0][:3] == ("BTC/USDT", "market", "buy")
    events = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    assert any(event.get("phase") == "execute" and event.get("mode") == "live" for event in events)


def test_run_cycle_skips_each_failing_candidate_but_completes(tmp_path, monkeypatch):
    """One candidate's failure never stops the cycle (execution/daily.py:170-173)."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": False, "exec_log_path": str(engine_audit)})

    monkeypatch.setattr(screening_mod, "crypto_universe", lambda config, top_n: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: [])
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [{"ticker": "BTC-USD", "asset_type": "crypto"}])

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_StubVenue)

    monkeypatch.setattr(autoloop_mod, "_build_bridge", build)

    class _BrokenGraph:
        def propagate(self, *args, **kwargs):
            raise RuntimeError("LLM relay down")

    results = autoloop_mod._run_cycle(lambda config, analysts: _BrokenGraph())

    (btc,) = results
    assert btc.executed is False
    assert btc.reason == "RuntimeError: LLM relay down"
    assert autoloop_mod._last_cycle["status"] == "completed"  # the cycle itself completed
    (completed,) = [json.loads(line) for line in
                    [json.dumps(e) for e in control_store.load_server_audit()]
                    if "autoloop_cycle_completed" in line]
    assert completed["results"][0]["reason"] == "RuntimeError: LLM relay down"


def test_cycle_screening_timeout_is_terminal_and_does_not_build_venue(tmp_path, monkeypatch):
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_candidates, 'SCREENING_TIMEOUT_SECONDS', 0.01)

    def slow_crypto(*_args, **_kwargs):
        import time
        time.sleep(0.05)
        return ["BTC-USD"]

    monkeypatch.setattr(screening_mod, "crypto_universe", slow_crypto)
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(screening_mod, "screen_universe", lambda *_args: [])
    monkeypatch.setattr(
        autoloop_mod,
        "_build_bridge",
        lambda *_args: pytest.fail("screening timeout must not build a venue"),
    )

    assert autoloop_mod._run_cycle(lambda *_args: pytest.fail("graph must not build")) == []
    assert autoloop_mod._last_cycle["status"] == "timeout"
    assert "screening" in autoloop_mod._last_cycle["error"]
    audit = "\n".join(json.dumps(e) for e in control_store.load_server_audit())
    assert "autoloop_cycle_timeout" in audit
    assert not engine_audit.exists()


def test_cycle_runs_crypto_candidates_before_advisory_stocks(tmp_path, monkeypatch):
    """The venue only executes crypto, so crypto must be processed first: a
    cycle that runs out of time on the stock tail has still seen every
    executable candidate."""
    set_config({"auto_spot_enabled": True, "exec_live": False})
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    # Screen score order puts stocks first — exactly the real-world pattern
    # that used to burn the whole cycle budget on advisory-only candidates.
    monkeypatch.setattr(
        screening_mod, "screen_universe",
        lambda *_args: [
            {"ticker": "AAPL", "asset_type": "stock"},
            {"ticker": "MRNA", "asset_type": "stock"},
            {"ticker": "BTC-USD", "asset_type": "crypto"},
            {"ticker": "ETH-USD", "asset_type": "crypto"},
        ],
    )
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config, **_kwargs: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )

    seen: list[str] = []

    class RecordingGraph:
        def propagate(self, ticker, *_args, **_kwargs):
            seen.append(ticker)
            return {}, "Hold"

    results = autoloop_mod._run_cycle(lambda *_args: RecordingGraph())
    # The run is capped at AUTO_TOP_N after crypto-first ordering: the screen
    # pool is wider than the run so filtering cannot starve the cycle.
    assert seen == ["BTC-USD", "ETH-USD", "AAPL"]
    assert [result.ticker for result in results] == seen
    assert autoloop_mod._last_cycle["status"] == "completed"
    assert autoloop_mod._last_cycle["candidates"] == seen


def test_preflight_drops_stocks_and_contractless_symbols_in_futures_mode(tmp_path, monkeypatch):
    """In futures mode a stock or a symbol with no live contract can never end
    in an order, yet each costs a full LLM pipeline. Preflight drops them
    before the pipeline runs and records why, so a cycle spends its budget on
    candidates that can actually trade."""
    _derivatives_cycle_env(tmp_path, monkeypatch)
    from tradingagents.execution.derivatives import DerivativesExecutor

    executor = DerivativesExecutor(config=get_config(), exchange_factory=_StubDerivVenue)

    candidates = [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "SPCXB-USD", "asset_type": "crypto"},  # no contract in the stub venue
        {"ticker": "MRNA", "asset_type": "stock"},
    ]
    runnable, skipped = autoloop_candidates._preflight_candidates(candidates, executor)

    assert [c["ticker"] for c in runnable] == ["BTC-USD"]
    assert [s["ticker"] for s in skipped] == ["SPCXB-USD", "MRNA"]
    assert "no active futures contract" in skipped[0]["reason"]
    assert "advisory only in futures mode" in skipped[1]["reason"]


def test_preflight_keeps_everything_when_market_metadata_is_unavailable(tmp_path, monkeypatch):
    """An unreachable venue must not empty the cycle: the per-candidate path
    still re-checks and rejects on its own."""
    _derivatives_cycle_env(tmp_path, monkeypatch)
    from tradingagents.execution.derivatives import DerivativesExecutor

    executor = DerivativesExecutor(config=get_config(), exchange_factory=_StubDerivVenue)

    class DeadVenue:
        def load_markets(self):
            raise OSError("venue down")

    monkeypatch.setattr(executor._bridge, "client", lambda: DeadVenue())

    candidates = [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "MRNA", "asset_type": "stock"},
    ]
    runnable, skipped = autoloop_candidates._preflight_candidates(candidates, executor)
    assert [c["ticker"] for c in runnable] == ["BTC-USD"]
    assert [s["ticker"] for s in skipped] == ["MRNA"]
    assert "advisory only in futures mode" in skipped[0]["reason"]


def test_cycle_candidate_timeout_is_terminal_and_does_not_execute(tmp_path, monkeypatch):
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_execution, 'CYCLE_TIMEOUT_SECONDS', 1.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        autoloop_candidates,
        '_screen_with_timeout',
        lambda *_args, **_kwargs: (
            ["BTC-USD"], [], [{"ticker": "BTC-USD", "asset_type": "crypto"}],
        ),
    )
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config, **_kwargs: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )
    release_graph = threading.Event()
    graph_returned = threading.Event()

    class SlowGraph:
        def propagate(self, *args, telemetry=None, **kwargs):
            span_id = telemetry.start("graph_node:Market Analyst", "BTC-USD")
            try:
                assert release_graph.wait(timeout=2.0)
                telemetry.finish(span_id)
                return {}, "Buy"
            finally:
                graph_returned.set()

    try:
        results = autoloop_mod._run_cycle(lambda *_args: SlowGraph())
        snapshot = json.dumps(autoloop_mod._last_cycle, sort_keys=True)
        assert results == []
        assert autoloop_mod._last_cycle["status"] == "timeout"
        assert "candidate" in autoloop_mod._last_cycle["error"]
        assert "stage graph_node:Market Analyst" in autoloop_mod._last_cycle["error"]
        assert autoloop_mod._last_cycle["telemetry"]["active_stage"] == "graph_node:Market Analyst"
    finally:
        release_graph.set()
        assert graph_returned.wait(timeout=1.0)

    assert json.dumps(autoloop_mod._last_cycle, sort_keys=True) == snapshot
    assert not engine_audit.exists()


def test_cycle_publishes_active_graph_stage_while_candidate_is_running(tmp_path, monkeypatch):
    import time

    set_config({"auto_spot_enabled": True, "exec_live": False})
    monkeypatch.setattr(autoloop_execution, 'CYCLE_TIMEOUT_SECONDS', 2.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod,
        "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
    )
    monkeypatch.setattr(
        autoloop_mod, "_build_bridge",
        lambda config, **_kwargs: ExchangeBridge(config=config, exchange_factory=_StubVenue),
    )

    release_graph = threading.Event()
    graph_entered = threading.Event()

    class WaitingGraph:
        def propagate(self, *args, telemetry=None, **kwargs):
            graph_entered.set()
            assert release_graph.wait(timeout=1.0)
            return {}, "Hold"

    worker = threading.Thread(
        target=autoloop_mod._run_cycle,
        args=(lambda *_args: WaitingGraph(),),
        daemon=True,
    )
    worker.start()
    try:
        assert graph_entered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            cycle = autoloop_mod._last_cycle
            if cycle and cycle.get("telemetry", {}).get("active_stage") == "graph_analysis":
                break
            time.sleep(0.005)
        assert autoloop_mod._last_cycle["status"] == "running"
        assert autoloop_mod._last_cycle["telemetry"]["active_stage"] == "graph_analysis"
        assert autoloop_mod._last_cycle["telemetry"]["active_ticker"] == "BTC-USD"
        first_elapsed = autoloop_api._payload()["last_cycle"]["telemetry"]["active_elapsed_ms"]
        time.sleep(0.01)
        second_elapsed = autoloop_api._payload()["last_cycle"]["telemetry"]["active_elapsed_ms"]
        assert second_elapsed > first_elapsed
    finally:
        release_graph.set()
        worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert autoloop_mod._last_cycle["status"] == "completed"


def test_timed_out_worker_cannot_execute_after_slow_sync(tmp_path, monkeypatch):
    from tradingagents.execution.sizing import PlannedOrder
    from tradingagents.portfolio import PortfolioContext

    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"auto_spot_enabled": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_execution, 'CYCLE_TIMEOUT_SECONDS', 5.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod,
        "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
    )
    monkeypatch.setattr(autoloop_futures_context, '_bracket_prices', lambda *_args, **_kwargs: (90.0, 110.0))

    release_sync = threading.Event()
    sync_entered = threading.Event()
    sync_finished = threading.Event()
    submitted: list[object] = []

    class SlowBridge:
        config = {"auto_spot_enabled": True, "exec_live": True}

        # The spot path reads the selected Auto candle off the same venue
        # client the bridge plans and executes through.
        def client(self):
            return _StubVenue()

        def symbol_for(self, ticker):
            return "BTC/USDT"

        def sync_portfolio(self):
            sync_entered.set()
            release_sync.wait(timeout=30.0)
            sync_finished.set()
            return PortfolioContext(cash=1_000.0)

        def plan_order(self, *args, **kwargs):
            return PlannedOrder(
                ticker="BTC-USD",
                ccxt_symbol="BTC/USDT",
                signal="Buy",
                futures_execution_intent="enter",
                side="buy",
                quantity=0.01,
                cost=500.0,
                estimated_price=50_000.0,
            )

        def effective_mode(self):
            return "dry"

        def execute_order(self, *args, **kwargs):
            submitted.append(object())
            return None

        def audit_plan(self, *args, **kwargs):
            return None

    monkeypatch.setattr(autoloop_mod, "_build_bridge", lambda *_args: SlowBridge())

    cycle = threading.Thread(
        target=lambda: autoloop_mod._run_cycle(lambda *_args: _FakeGraph("Buy")),
        name="test-cycle-worker", daemon=True,
    )
    cycle.start()
    # Deterministic ordering: the worker must be blocked inside sync_portfolio
    # before the deadline can fire. Waiting for sync_entered replaces the old
    # 0.02s race that made the suite-order run flaky under CPU contention.
    assert sync_entered.wait(timeout=10.0)
    cycle.join(timeout=10.0)
    assert not cycle.is_alive()
    assert autoloop_mod._last_cycle["status"] == "timeout"

    release_sync.set()
    assert sync_finished.wait(timeout=15.0)
    assert submitted == []
    assert not engine_audit.exists()
