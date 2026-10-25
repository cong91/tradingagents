"""Autoloop tests (server/autoloop.py): gates, state file, scheduler, cycle.

Unit-scoped like tests/test_server_api.py: a dedicated ASGI app carrying the
autoloop + health routers (main.py is not touched by this feature yet), no
sockets, no LLM, no vendors — the screen, the venue and the graph are fakes,
and the engine's audit log lives in tmp.
"""

import json
import socket
import threading
import weakref
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import tradingagents.dataflows.screening as screening_mod
from server import (
    autoloop as autoloop_mod,
    control_store,
    daily as daily_mod,
    health as health_mod,
    runs as runs_mod,
)
from server.contract import install_error_handlers, utc_now_iso
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.portfolio import PortfolioContext

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Shadow conftest's blanket socket refusal for ASGI tests (same shadow as
    tests/test_server_api.py: the event loop's Windows self-pipe is the only
    whitelisted socket; every other connect still refuses)."""
    if request.node.get_closest_marker("integration"):
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_socketpair = socket.socketpair
    selfpipe_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()

    def refuse_connect(self, address):
        if self in selfpipe_sockets:
            return original_connect(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def refuse_connect_ex(self, address):
        if self in selfpipe_sockets:
            return original_connect_ex(self, address)
        raise OSError(f"test tried to reach the network: {address}")

    def marked_socketpair(*args, **kwargs):
        # Windows socketpair() performs its own loopback handshake through
        # socket.connect, so the refusal must step aside for its duration,
        # then re-arm. Only the pair it returns is whitelisted afterwards.
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        try:
            first, second = original_socketpair(*args, **kwargs)
        finally:
            socket.socket.connect = refuse_connect
            socket.socket.connect_ex = refuse_connect_ex
        selfpipe_sockets.add(first)
        selfpipe_sockets.add(second)
        return first, second

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect_ex)
    monkeypatch.setattr(socket, "socketpair", marked_socketpair)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@pytest.fixture(autouse=True)
def _server_state(tmp_path, monkeypatch, postgres_schema):
    """Point all server state at tmp and reset the in-memory loop state."""
    if autoloop_mod._scheduler_thread is not None and autoloop_mod._scheduler_thread.is_alive():
        autoloop_mod.stop()
    scheduler_stop = threading.Event()
    monkeypatch.setattr(autoloop_mod, "_scheduler_stop", scheduler_stop)
    monkeypatch.setattr(autoloop_mod, "_last_cycle", None)
    monkeypatch.setattr(autoloop_mod, "_last_telemetry", None)
    monkeypatch.setattr(autoloop_mod, "_cycle_thread", None)
    monkeypatch.setattr(autoloop_mod, "_scheduler_thread", None)
    autoloop_mod._wakeup.clear()
    # A run/job planted anywhere must never leak into a cycle's busy check.
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})
    yield
    scheduler_stop.set()
    wakeup_set = getattr(autoloop_mod._wakeup, "set", None)
    if wakeup_set is not None:
        wakeup_set()
    for thread in threading.enumerate():
        if thread.name == "server-autoloop-scheduler":
            thread.join(timeout=1.0)
            assert not thread.is_alive(), "autoloop scheduler leaked past test teardown"


def _test_app() -> FastAPI:
    test_app = FastAPI()
    install_error_handlers(test_app)
    test_app.include_router(autoloop_mod.router)
    test_app.include_router(health_mod.router)
    return test_app


@pytest.fixture()
def client(_server_state):
    with TestClient(_test_app()) as test_client:
        yield test_client


# --- GET /api/autoloop --------------------------------------------------------


def test_get_autoloop_defaults(client):
    # Pin the gate-closed state explicitly: the operator's default_config may
    # legitimately arm auto_confirm (the sanctioned arming path), so the
    # fail-closed contract is asserted against an explicit closed config.
    set_config({"exec_auto_confirm": False})
    body = client.get("/api/autoloop").json()
    assert body["generated_at"].endswith("Z")
    assert body["enabled"] is False
    assert body["interval_minutes"] == 1440
    assert body["running_cycle"] is False
    # conftest blanks every TRADINGAGENTS_* env var and the config defaults
    # keep every gate closed: the fail-closed default is the loop can't arm.
    assert body["gates"] == {
        "auto_confirm_config": False, "auto_confirm_env": False,
        "exec_live_config": False, "exec_live_env": False,
        "exec_derivatives_config": False, "exec_derivatives_env": False, "all_open": False,
    }
    assert body["last_cycle"] is None


# --- POST /api/autoloop: the enable gate + state file --------------------------


def test_enable_gate_closed_is_409_and_armes_nothing(client, tmp_path, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(autoloop_mod, "ensure_started", lambda: started.append(True))
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: "started")
    set_config({"exec_auto_confirm": False})  # pin the gate-closed state

    response = client.post("/api/autoloop", json={"enabled": True})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "gate_closed"
    assert error["details"]["gates"]["all_open"] is False
    assert error["details"]["gates"]["auto_confirm_config"] is False

    assert control_store.load_autoloop_state() == {}  # a refused enable persists nothing
    assert client.get("/api/autoloop").json()["enabled"] is False
    assert started == []  # the scheduler was never armed by a refused enable


def test_enable_with_gates_open_persists_state_and_arms_scheduler(client, tmp_path, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(autoloop_mod, "ensure_started", lambda: started.append(True))
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: "started")

    # exec_live stays closed on purpose: enabling needs only the two
    # auto-confirm halves, exactly like _handle_plan's first two checks.
    set_config({"exec_auto_confirm": True})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    response = client.post("/api/autoloop", json={"enabled": True, "interval_minutes": 30})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["interval_minutes"] == 30
    assert body["gates"]["all_open"] is True
    assert body["gates"]["exec_live_config"] is False  # reported, never required to enable
    assert body["gates"]["exec_derivatives_config"] is False  # futures: observability only, never required

    state = control_store.load_autoloop_state()
    assert state == {"enabled": True, "interval_minutes": 30, "updated_at": state["updated_at"]}
    assert state["updated_at"]
    assert client.get("/api/autoloop").json()["enabled"] is True
    assert started == [True]


def test_disable_is_never_gated_and_omitted_interval_defaults(client, tmp_path):
    # gates closed: turning the loop OFF must still work (the safe direction)
    response = client.post("/api/autoloop", json={"enabled": False})
    assert response.status_code == 200
    state = control_store.load_autoloop_state()
    assert state["enabled"] is False
    assert state["interval_minutes"] == 1440  # default when omitted


def test_autoloop_validates_body(client, tmp_path):
    for bad_body in (
        {"enabled": "yes"},
        {"enabled": 1},
        {"enabled": True, "interval_minutes": 10},      # below the floor
        {"enabled": True, "interval_minutes": 1441},    # above the ceiling
        {"enabled": True, "interval_minutes": True},    # bool is not an int here
        {"enabled": True, "interval_minutes": "30"},
    ):
        response = client.post("/api/autoloop", json=bad_body)
        assert response.status_code == 400, bad_body
        assert response.json()["error"]["code"] == "validation_error"
    assert control_store.load_autoloop_state() == {}


# --- scheduler -----------------------------------------------------------------


def test_ensure_started_reuses_a_live_scheduler(monkeypatch):
    class _Alive:
        def is_alive(self) -> bool:
            return True

    sentinel = _Alive()
    monkeypatch.setattr(autoloop_mod, "_scheduler_thread", sentinel)
    autoloop_mod.ensure_started()
    autoloop_mod.ensure_started()
    assert autoloop_mod._scheduler_thread is sentinel  # idempotent: never a second thread


def test_stop_joins_the_autoloop_scheduler(monkeypatch):
    monkeypatch.setattr(
        autoloop_mod,
        "_load_state",
        lambda: {"enabled": False, "interval_minutes": 1440},
    )
    autoloop_mod._wakeup.clear()

    autoloop_mod.ensure_started()
    scheduler = autoloop_mod._scheduler_thread
    assert scheduler is not None and scheduler.is_alive()

    autoloop_mod.stop()

    assert not scheduler.is_alive()
    assert autoloop_mod._scheduler_thread is None


def test_scheduler_stop_during_state_read_launches_no_new_cycle(monkeypatch):
    """A stop arriving between the loop-top check and the spawn decision must
    not launch one final cycle: shutdown never arms new work."""
    reads: list[int] = []
    spawns: list[int] = []

    def load_state():
        reads.append(1)
        if len(reads) >= 2:
            autoloop_mod._scheduler_stop.set()
        return {"enabled": True, "interval_minutes": 1440, "updated_at": f"u{len(reads)}"}

    class Wakeup:
        def wait(self, timeout=None):
            return True

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_load_state", load_state)
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle",
                        lambda *_args, **_kwargs: spawns.append(1) or "started")
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    autoloop_mod._scheduler_loop()

    assert spawns == [1]


def test_scheduler_loop_runs_enabled_state_immediately(monkeypatch, tmp_path):
    autoloop_mod._save_state({"enabled": True, "interval_minutes": 1440, "updated_at": utc_now_iso()})
    calls: list[int] = []
    stop = threading.Event()

    def spawn(*args, **kwargs):
        calls.append(1)
        autoloop_mod._save_state({"enabled": False, "interval_minutes": 1440})
        return "started"

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", spawn)
    monkeypatch.setattr(autoloop_mod, "_wakeup", stop)
    thread = threading.Thread(
        target=autoloop_mod._scheduler_loop, name="server-autoloop-scheduler", daemon=True
    )
    thread.start()
    try:
        thread.join(timeout=1.0)
        assert calls == [1]
    finally:
        autoloop_mod._scheduler_stop.set()
        stop.set()
        thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_scheduler_runs_again_after_interval_timeout(monkeypatch, tmp_path):
    autoloop_mod._save_state({
        "enabled": True,
        "interval_minutes": 15,
        "updated_at": "initial",
    })
    calls: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return False
            raise StopScheduler

        def clear(self):
            pass

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *_args, **_kwargs: calls.append(1) or "started")
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert calls == [1, 1]


def test_scheduler_ignores_pending_enable_wakeup_but_runs_changed_state(monkeypatch, tmp_path):
    autoloop_mod._save_state({
        "enabled": True,
        "interval_minutes": 15,
        "updated_at": "initial",
    })
    calls: list[int] = []

    class StopScheduler(Exception):
        pass

    class Wakeup:
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                return True
            if self.waits == 2:
                autoloop_mod._save_state({
                    "enabled": True,
                    "interval_minutes": 30,
                    "updated_at": "changed",
                })
                return True
            if self.waits == 3:
                raise StopScheduler
            return False

        def clear(self):
            pass

    def spawn(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 2:
            autoloop_mod._save_state({
                "enabled": False,
                "interval_minutes": 30,
                "updated_at": "disabled",
            })
        return "started"

    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", spawn)
    monkeypatch.setattr(autoloop_mod, "_wakeup", Wakeup())
    errors: list[BaseException] = []

    def run_scheduler():
        try:
            autoloop_mod._scheduler_loop()
        except StopScheduler:
            return
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    assert calls == [1, 1]


# --- POST /api/autoloop/run-now -------------------------------------------------


def test_run_now_spawns_a_cycle_and_reports_already_running(client, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: calls.append(1) or "started")

    response = client.post("/api/autoloop/run-now", json={})
    assert response.status_code == 202
    assert response.json()["status"] == "started"
    assert calls == [1]

    with autoloop_mod._cycle_lock:  # a cycle is "in flight"
        busy = client.post("/api/autoloop/run-now", json={})
        assert busy.status_code == 202
        assert busy.json()["status"] == "already_running"
    assert calls == [1]  # the busy probe spawned nothing


def test_run_now_skipped_busy_records_the_skip_trail(client, tmp_path):
    active = runs_mod.RunState(
        run_id="r_active", ticker="BTC-USD", trade_date=_today(),
        asset_type="crypto", mode="mock", checkpoint=None, created_at=_today(),
    )
    active.status = "running"
    runs_mod._runs[active.run_id] = active

    response = client.post("/api/autoloop/run-now", json={})
    assert response.status_code == 202
    assert response.json()["status"] == "skipped_busy"

    worker = autoloop_mod._cycle_thread  # the spawn still happens; the cycle decides
    assert worker is not None
    worker.join(timeout=5.0)
    assert not worker.is_alive()

    trail = "\n".join(json.dumps(e) for e in control_store.load_server_audit())
    assert "autoloop_cycle_skipped" in trail
    assert "run r_active" in trail
    assert autoloop_mod._last_cycle["status"] == "skipped_busy"


# --- kill switch: POST /api/halt + /api/halt/reset -------------------------------


def test_halt_writes_kill_switch_line_and_health_reflects_it(client, tmp_path):
    audit_file = tmp_path / "engine-audit.jsonl"
    set_config({"exec_log_path": str(audit_file)})

    response = client.post("/api/halt", json={})
    assert response.status_code == 200
    assert response.json()["halted"] is True

    events = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    (halt,) = [event for event in events if event.get("event") == "halt"]
    # the timestamp is what makes the halt day-scoped (risk.py:111-119): a
    # halt line without one would be invisible to _halted_today.
    assert halt["timestamp"].startswith(_today())

    body = client.get("/api/health").json()
    assert body["halted_today"] is True


def test_halt_reset_clears_today_halt(client, tmp_path):
    audit_file = tmp_path / "engine-audit.jsonl"
    set_config({"exec_log_path": str(audit_file)})
    client.post("/api/halt", json={})

    reset = client.post("/api/halt/reset", json={})
    assert reset.status_code == 200
    assert reset.json()["halted"] is False

    events = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [event.get("event") for event in events] == ["halt", "halt_reset"]
    assert client.get("/api/health").json()["halted_today"] is False


# --- the cycle: screen → pipeline → the engine's 3-gate confirm policy ----------

_STOP, _TAKE = 80246.0, 86562.0
_STUB_PRICE = 83078.0
_STUB_CASH = 9500.0


class _StubVenue:
    """Fixture venue mirroring server/daily._StubVenue: constant answers, so
    sync_portfolio and plan_order never touch ccxt or the network."""

    def fetch_ticker(self, symbol: str) -> dict:
        return {"last": _STUB_PRICE, "close": _STUB_PRICE}

    def load_markets(self) -> dict:
        return {}  # no market minimums: plan_order falls back to the rules floor

    def fetch_balance(self, params: dict | None = None) -> dict:
        # ``equity``/``info.asset`` are the futures wallet the cycle sizes
        # against; the spot ``total`` map is what sync_portfolio reads.
        return {
            "total": {"USDT": _STUB_CASH}, "free": {"USDT": _STUB_CASH},
            "equity": _STUB_CASH, "info": {"asset": "USDT"},
        }

    def fetch_positions(self, symbols=None, params: dict | None = None) -> list:
        return []

    def fetch_ohlcv(self, symbol: str, timeframe: str, **kwargs):
        duration = 3_600_000 if timeframe == "1h" else 14_400_000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        open_ms = now_ms - (now_ms % duration) - duration
        return [[open_ms, _STUB_PRICE, _STUB_PRICE + 100,
                 _STUB_PRICE - 100, _STUB_PRICE + 1, 1.0]]


def _agent_proposal(**overrides) -> dict:
    """A complete FuturesExecutionProposal-shaped decision for the fixtures."""
    proposal = {
        "margin_mode": "isolated",
        "leverage": 3,
        "risk_fraction": 0.01,
        "margin_fraction": 0.25,
        "rationale": "Aligned closed 1h/4h candles, isolated collateral, 1% risk.",
    }
    proposal.update(overrides)
    return proposal


class _FakeGraph:
    """propagate() returns a fixed signal/state and records the calls it saw."""

    def __init__(self, signal: str, proposal: dict | None = None, intent: str = "enter"):
        self._signal = signal
        self._proposal = _agent_proposal() if proposal is None else proposal
        self._intent = intent
        self.calls: list[tuple[str, str, str]] = []

    def propagate(self, ticker: str, trade_date: str, asset_type: str | None = None):
        self.calls.append((ticker, trade_date, str(asset_type)))
        state = {
            "futures_execution_proposal": self._proposal,
            "futures_execution_intent": self._intent,
        }
        return state, self._signal


def test_run_cycle_auto_confirms_dry_buy_with_bracket(tmp_path, monkeypatch):
    """Auto-confirm dry: the third gate fully open (config literal + env flag,
    exec_live closed) — the cycle's Buy plan executes as a dry fill with the
    ATR bracket attached, exactly what _handle_plan alone decides."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"exec_auto_confirm": True, "exec_live": False, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")  # the env half

    monkeypatch.setattr(screening_mod, "crypto_universe", lambda config, top_n: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: ["NVDA"])
    monkeypatch.setattr(screening_mod, "screen_universe", lambda crypto, stocks, date, top_n: [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "NVDA", "asset_type": "stock"},
    ])
    monkeypatch.setattr(screening_mod, "atr_targets_signed",
                        lambda ticker, date, direction: (_STOP, _TAKE))

    def build(config):
        from tradingagents.execution.bridge import ExchangeBridge

        return ExchangeBridge(config=config, exchange_factory=_StubVenue)

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


def test_run_cycle_skips_each_failing_candidate_but_completes(tmp_path, monkeypatch):
    """One candidate's failure never stops the cycle (execution/daily.py:170-173)."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"exec_auto_confirm": True, "exec_live": False, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")

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
    set_config({"exec_auto_confirm": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_mod, "SCREENING_TIMEOUT_SECONDS", 0.01)

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
    set_config({"exec_auto_confirm": False, "exec_live": False})
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
    monkeypatch.setattr(autoloop_mod, "_build_bridge", lambda *_args: _StubVenue())

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
    runnable, skipped = autoloop_mod._preflight_candidates(candidates, executor)

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
    runnable, skipped = autoloop_mod._preflight_candidates(candidates, executor)
    assert [c["ticker"] for c in runnable] == ["BTC-USD", "MRNA"]
    assert skipped == []


def test_cycle_candidate_timeout_is_terminal_and_does_not_execute(tmp_path, monkeypatch):
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"exec_auto_confirm": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_mod, "CYCLE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        autoloop_mod,
        "_screen_with_timeout",
        lambda *_args, **_kwargs: (
            ["BTC-USD"], [], [{"ticker": "BTC-USD", "asset_type": "crypto"}],
        ),
    )
    monkeypatch.setattr(autoloop_mod, "_build_bridge", lambda *_args: _StubVenue())
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

    set_config({"exec_auto_confirm": False, "exec_live": False})
    monkeypatch.setattr(autoloop_mod, "CYCLE_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod,
        "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
    )
    monkeypatch.setattr(autoloop_mod, "_build_bridge", lambda *_args: _StubVenue())

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
        first_elapsed = autoloop_mod._payload()["last_cycle"]["telemetry"]["active_elapsed_ms"]
        time.sleep(0.01)
        second_elapsed = autoloop_mod._payload()["last_cycle"]["telemetry"]["active_elapsed_ms"]
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
    set_config({"exec_auto_confirm": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_mod, "CYCLE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod,
        "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
    )
    monkeypatch.setattr(autoloop_mod, "_bracket_prices", lambda *_args, **_kwargs: (90.0, 110.0))

    release_sync = threading.Event()
    sync_entered = threading.Event()
    sync_finished = threading.Event()
    submitted: list[object] = []

    class SlowBridge:
        config = {"exec_auto_confirm": True, "exec_live": True}

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


class _StubDerivVenue(_StubVenue):
    """Same constant answers as the spot stub, shaped for the derivatives
    executor: unified symbols, leverage pinning, precision."""

    candle_direction = "bullish"

    def load_markets(self):
        return {
            "BTC/USDT:USDT": {
                "base": "BTC", "quote": "USDT", "settle": "USDT",
                "contract": True, "swap": True, "type": "swap", "active": True,
                "linear": True,
            }
        }

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{float(amount):.6f}"

    def fetch_ohlcv(self, symbol: str, timeframe: str, **kwargs):
        duration = 3_600_000 if timeframe == "1h" else 14_400_000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        open_ms = now_ms - (now_ms % duration) - duration
        # open, high, low, close per requested direction; a doji closes flat.
        close = {"bullish": _STUB_PRICE + 1, "bearish": _STUB_PRICE - 1,
                 "doji": _STUB_PRICE}[self.candle_direction]
        return [[open_ms, _STUB_PRICE, _STUB_PRICE + 100,
                 _STUB_PRICE - 100, close, 1.0]]

    def set_leverage(self, leverage: int, symbol: str, params=None) -> None:
        pass


def _derivatives_cycle_env(tmp_path, monkeypatch, *, config_extra=None, candle_direction="bullish"):
    """Gates + screening fakes for a derivatives-path cycle: the FR-D gates
    armed in config AND env (planning refuses otherwise), exec_live closed so
    _handle_plan can only ever dry-run, and the executor seam replaced by a
    factory over the stub venue."""
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({
        "exec_auto_confirm": True, "exec_live": False,
        "exec_derivatives": True,
        # Risk caps sized for the 3x plan the cycle requests: margin = 25% of
        # cash 9500 = 2375, notional at 3x = 7125 = 75% of equity — so every
        # percentage cap must sit at/above 75% and the leverage cap at 3x.
        "risk_max_derivatives_leverage": 3.0,
        "risk_max_derivatives_exposure_pct": 100.0,
        "risk_max_position_pct_per_asset": 100.0,
        "risk_max_total_exposure_pct": 100.0,
        "exec_log_path": str(engine_audit),
        **(config_extra or {}),
    })
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_DERIVATIVES", "true")

    monkeypatch.setattr(screening_mod, "crypto_universe", lambda config, top_n: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda config, top_n: [])
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [{"ticker": "BTC-USD", "asset_type": "crypto"}])

    class _FakeRankingClient:
        def fetch_tickers(self):
            return {"BTC/USDT:USDT": {"quoteVolume": 1_000_000_000_000.0, "last": _STUB_PRICE}}

    monkeypatch.setattr(screening_mod, "_public_futures_ranking_client", lambda config: _FakeRankingClient())

    def build(config):
        from tradingagents.execution.derivatives import DerivativesExecutor
        from tradingagents.execution.risk import RiskGuard, RiskLimits

        return DerivativesExecutor(
            config=config, exchange_factory=_StubDerivVenue,
            risk_guard=RiskGuard(
                limits=RiskLimits(
                    max_derivatives_leverage=3.0,
                    max_derivatives_exposure_pct=100.0,
                    max_position_pct_per_asset=100.0,
                    max_total_exposure_pct=100.0,
                ),
                price_of=lambda ticker: 50_000.0,
            ),
        )

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)
    monkeypatch.setattr(_StubDerivVenue, "candle_direction", candle_direction)
    monkeypatch.setattr(autoloop_mod, "_bracket_prices",
                        lambda ticker, direction="long": (
                            (_STUB_PRICE - 1_000.0, _STUB_PRICE + 1_000.0)
                            if direction == "long"
                            else (_STUB_PRICE + 1_000.0, _STUB_PRICE - 1_000.0)
                        ))
    monkeypatch.setattr(autoloop_mod, "_today_iso", lambda: "2026-10-07")
    return engine_audit


def test_run_cycle_derivatives_dry_buy_long_bracket(tmp_path, monkeypatch):
    """exec_derivatives armed: the crypto candidate plans through the
    DerivativesExecutor using the AGENT's leverage (not a code default) and the
    Buy's dry fill carries the LONG bracket (stop below entry, target above)."""
    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch)

    results = autoloop_mod._run_cycle(
        lambda config, analysts: _FakeGraph("Buy", proposal=_agent_proposal(leverage=2)),
    )

    (btc,) = results
    assert btc.executed is True
    assert btc.plan.market == "derivatives"
    assert btc.plan.leverage == 2  # the proposal's number, below the 3x ceiling
    assert btc.plan.margin_mode == "isolated"
    assert btc.plan.side == "buy"
    assert btc.plan.stop_loss < btc.plan.estimated_price < btc.plan.take_profit

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
    assert f"Maximum leverage: {autoloop_mod.MAX_AUTO_LEVERAGE:g}x" in context
    assert "A FuturesExecutionProposal is required for any entry" in context


def test_futures_sizing_book_uses_the_futures_wallet_not_spot_cash():
    """Spot cash is a different wallet: sizing futures against it would risk
    money the derivatives account does not hold."""
    from tradingagents.execution.futures_account import FuturesAccountSnapshot

    book = autoloop_mod._futures_portfolio(FuturesAccountSnapshot(
        equity=420.5, wallet_balance=400.0, available_balance=380.0,
        equity_currency="USDT", available_balance_currency="USDT",
    ))

    assert book.cash == pytest.approx(420.5)
    assert book.currency == "USDT"
    # The equity already contains open PnL; adding notional again would
    # overstate the denominator every percentage cap divides by.
    assert book.positions == []


def test_futures_sizing_book_falls_back_to_available_balance():
    from tradingagents.execution.futures_account import FuturesAccountSnapshot

    book = autoloop_mod._futures_portfolio(FuturesAccountSnapshot(
        equity=None, available_balance=125.0, available_balance_currency="USDT",
    ))

    assert book.cash == pytest.approx(125.0)


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


class _StubOffWhitelistDerivVenue(_StubDerivVenue):
    """Off-whitelist fixture: load_markets carries the NEAR/USDT:USDT swap and
    fetch_ticker answers per unified symbol so the test can prove which symbol
    the mark price was read from."""

    tickers_fetched: list[str] = []

    def load_markets(self):
        markets = super().load_markets()
        markets["NEAR/USDT:USDT"] = {
            "base": "NEAR", "quote": "USDT", "settle": "USDT",
            "contract": True, "swap": True, "type": "swap", "active": True,
        }
        return markets

    def fetch_ticker(self, symbol: str) -> dict:
        self.tickers_fetched.append(symbol)
        return {"last": 5.0, "close": 5.0}


def test_run_cycle_derivatives_doji_candle_is_confirmation_missing(tmp_path, monkeypatch):
    """A doji closed candle can never confirm a tradeable signal in either
    direction: the candidate is skipped with confirmation_missing and the
    rejection is durably recorded with the doji reason for the scanner UI."""
    from server import job_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="doji")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.signal == "Buy"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("action") != "buy" for event in events)  # nothing planned on a doji
    (cycle,) = job_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["confirmation_reason"] == "doji_candle"
    assert cycle["payload"]["unified_symbol"] == "BTC/USDT:USDT"


def test_run_cycle_derivatives_direction_mismatch_is_confirmation_missing(tmp_path, monkeypatch):
    """Short against bullish closed candles is the wrong-direction mirror of
    the Short happy path: confirmation_missing with direction_mismatch, and
    no plan ever reaches the executor."""
    from server import job_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bullish")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Sell"))

    (btc,) = results
    assert btc.signal == "Sell"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("phase") != "plan" for event in events)
    (cycle,) = job_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["confirmation_reason"] == "direction_mismatch"


def test_run_cycle_derivatives_long_against_bearish_is_confirmation_missing(tmp_path, monkeypatch):
    """Long against bearish closed candles is the mirror of the Long happy
    path: confirmation_missing with direction_mismatch, no plan, and the
    durable rejection row carries the reason for the scanner UI."""
    from server import job_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bearish")

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.signal == "Buy"
    assert btc.executed is False and btc.plan is None
    assert btc.reason == "confirmation_missing"
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert all(event.get("phase") != "plan" for event in events)
    (cycle,) = job_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "rejected"
    assert cycle["payload"]["confirmation_reason"] == "direction_mismatch"
    assert cycle["payload"]["unified_symbol"] == "BTC/USDT:USDT"


def test_run_cycle_derivatives_off_whitelist_near_plans_and_prices_through_unified_symbol(tmp_path, monkeypatch):
    """NEAR-USD sits outside the spot closed set: the cycle must still plan
    when the venue metadata lists an active USDT-settled NEAR swap, fetch the
    mark price through the RESOLVED unified symbol (never the spot resolver),
    and dry-execute with the protective bracket intact."""
    from server import job_store

    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch, candle_direction="bullish")
    monkeypatch.setattr(screening_mod, "screen_universe",
                        lambda crypto, stocks, date, top_n: [{"ticker": "NEAR-USD", "asset_type": "crypto"}])

    def build(config):
        from tradingagents.execution.derivatives import DerivativesExecutor

        return DerivativesExecutor(config=config, exchange_factory=_StubOffWhitelistDerivVenue)

    monkeypatch.setattr(autoloop_mod, "_build_derivatives_executor", build)
    # NEAR marks at 5.0 through the unified symbol: the ATR bracket must sit
    # around THAT price, not the BTC stub price, or the protective check fails.
    monkeypatch.setattr(autoloop_mod, "_bracket_prices",
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

    (cycle,) = job_store.list_futures_cycles(limit=1)
    assert cycle["status"] == "completed"
    assert cycle["symbol"] == "NEAR/USDT:USDT"
    payload = cycle["payload"]
    assert payload["requested_ticker"] == "NEAR-USD"
    assert payload["unified_symbol"] == "NEAR/USDT:USDT"
    assert payload["confirmation_result"] is True
    assert payload["confirmation_reason"] == "aligned_direction"
    assert payload["market_rejection_reason"] is None
    assert payload["execution_status"] == "executed"


def test_run_cycle_derivatives_gates_closed_refuse_planning(tmp_path, monkeypatch):
    """The derivatives executor's own gates still decide: config armed but the
    TRADINGAGENTS_EXEC_DERIVATIVES env flag missing fails closed — the
    candidate records the refusal, the cycle completes (a candidate's failure
    never stops the cycle)."""
    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch)
    monkeypatch.delenv("TRADINGAGENTS_EXEC_DERIVATIVES", raising=False)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.executed is False and btc.plan is None
    assert "TRADINGAGENTS_EXEC_DERIVATIVES" in (btc.reason or "")
    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    assert events[-1]["action"] == "derivatives_denied"
    assert autoloop_mod._last_cycle["status"] == "completed"


def test_futures_worker_wiring_claims_and_skips_duplicate(tmp_path, monkeypatch):
    from tradingagents.execution.sizing import PlannedOrder

    now = datetime(2026, 10, 7, 8, tzinfo=timezone.utc)
    set_config({"exec_auto_confirm": False})
    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            return now
        @staticmethod
        def fromtimestamp(value, tz=None):
            return datetime.fromtimestamp(value, tz)
    monkeypatch.setattr(autoloop_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(autoloop_mod, "_bracket_prices", lambda ticker, direction="long": (90.0, 110.0))
    candle_rows = {"1h": [[(now.timestamp() - 3600) * 1000, 100, 110, 90, 105, 1]], "4h": [[(now.timestamp() - 4 * 3600) * 1000, 100, 110, 90, 105, 1]]}

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, **kwargs):
            return candle_rows[timeframe]

        def fetch_ticker(self, symbol):
            return {"last": 100.0, "close": 100.0}

    class Bridge:
        config = {"exec_auto_confirm": False}
        def client(self): return Exchange()
        def symbol_for(self, ticker): return "BTC/USDT:USDT"
        def fetch_price(self, ticker): return 100.0
        def sync_portfolio(self): return object()

    class Derivatives:
        _bridge = Bridge()
        config = {}
        def symbol_for(self, ticker): return self._bridge.symbol_for(ticker)
        def audit_plan(self, *args, **kwargs):
            return None
        def plan_order(self, *args, **kwargs):
            return PlannedOrder(ticker="BTC-USD", ccxt_symbol="BTC/USDT:USDT", signal="Buy", market="derivatives", side="buy", quantity=1, cost=100, estimated_price=100, stop_loss=90, take_profit=110)

    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_mod._process_futures_candidate(
        Derivatives(), "BTC-USD", "Buy", {}, result, proposal=_agent_proposal(),
        intent="enter",
    )
    assert result.reason == "awaiting approval"
    duplicate = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_mod._process_futures_candidate(
        Derivatives(), "BTC-USD", "Buy", {}, duplicate, proposal=_agent_proposal(),
        intent="enter",
    )
    assert duplicate.reason == "duplicate futures signal skipped"


def test_futures_intent_abstain_is_recorded_without_planning(monkeypatch):
    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    context = _market_context()

    class Derivatives(_NoopDerivatives):
        def plan_order(self, *args, **kwargs):
            pytest.fail("abstain intent must not plan an order")

    monkeypatch.setattr(
        autoloop_mod,
        "_record_futures_cycle_rejection",
        lambda *args, **kwargs: None,
    )
    autoloop_mod._process_futures_candidate(
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
        autoloop_mod,
        "_record_futures_cycle_rejection",
        lambda *args, **kwargs: None,
    )
    autoloop_mod._process_futures_candidate(
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
    autoloop_mod._process_futures_candidate(
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

    monkeypatch.setattr(autoloop_mod.job_store, "claim_futures_cycle", lambda *a, **k: True)
    monkeypatch.setattr(autoloop_mod.job_store, "update_futures_cycle", lambda *a, **k: None)
    result = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_mod._process_futures_candidate(
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


def test_proposal_above_the_hard_caps_is_refused_not_clamped():
    """An over-cap proposal is refused: silently resizing it would execute a
    decision no agent made."""
    for overrides, fragment in (
        ({"leverage": autoloop_mod.MAX_AUTO_LEVERAGE + 1}, "leverage"),
        ({"risk_fraction": 0.5}, "risk_fraction"),
        ({"margin_fraction": 0.9}, "margin_fraction"),
        ({"margin_mode": "portfolio"}, "margin_mode"),
        ({"stop_loss": 120, "take_profit": 80}, "bracket"),
    ):
        terms, reason = autoloop_mod._proposal_terms(
            _agent_proposal(**overrides), mark=100.0, direction="long", config={},
        )
        assert terms is None, overrides
        assert fragment in (reason or ""), (overrides, reason)


class _NoopDerivatives:
    """Minimal executor surface for the proposal-validation paths."""

    def __init__(self):
        self._bridge = _NoopBridge()
        self.config = {}

    def symbol_for(self, ticker):
        return "BTC/USDT:USDT"

    def plan_order(self, *args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("plan_order must not run when the proposal is refused")


class _NoopBridge:
    def client(self):
        return _NoopVenue()

    def sync_portfolio(self):
        return PortfolioContext(cash=1_000.0, currency="USDT")


class _NoopVenue:
    def fetch_ticker(self, symbol):
        return {"last": 100.0, "close": 100.0}


def _market_context(ticker: str = "BTC-USD"):
    from tradingagents.dataflows.market_data import Candle

    now = int(datetime(2026, 10, 7, 8, tzinfo=timezone.utc).timestamp() * 1000)
    one_hour = Candle("BTC/USDT:USDT", "1h", now - 3_600_000, now - 1, 100, 110, 90, 105, 1.0, True)
    four_hour = Candle("BTC/USDT:USDT", "4h", now - 14_400_000, now - 1, 100, 110, 90, 105, 1.0, True)
    return {
        "requested_ticker": ticker,
        "symbol": "BTC/USDT:USDT",
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": one_hour,
        "four_hour": four_hour,
        "one_hour_open": one_hour.open_time,
        "one_hour_close": one_hour.close,
        "four_hour_open": four_hour.open_time,
        "four_hour_close": four_hour.close,
        "one_hour_direction": "bullish",
        "four_hour_direction": "bullish",
    }
