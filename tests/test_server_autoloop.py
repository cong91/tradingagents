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
    daily as daily_mod,
    health as health_mod,
    paths as server_paths,
    runs as runs_mod,
)
from server.contract import install_error_handlers, utc_now_iso
from tradingagents.dataflows.config import set_config

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
def _server_state(tmp_path, monkeypatch):
    """Point all server state at tmp and reset the in-memory loop state."""
    monkeypatch.setattr(server_paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server_paths, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(server_paths, "APPROVALS_PATH", tmp_path / "approvals.jsonl")
    monkeypatch.setattr(server_paths, "SERVER_AUDIT_PATH", tmp_path / "server-audit.jsonl")
    monkeypatch.setattr(server_paths, "AUTOLOOP_PATH", tmp_path / "autoloop.json")
    monkeypatch.setattr(autoloop_mod, "_last_cycle", None)
    monkeypatch.setattr(autoloop_mod, "_last_telemetry", None)
    monkeypatch.setattr(autoloop_mod, "_cycle_thread", None)
    autoloop_mod._wakeup.clear()
    # A run/job planted anywhere must never leak into a cycle's busy check.
    monkeypatch.setattr(runs_mod, "_runs", {})
    monkeypatch.setattr(daily_mod, "_jobs", {})
    yield


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

    assert not (tmp_path / "autoloop.json").exists()  # a refused enable persists nothing
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

    state = json.loads((tmp_path / "autoloop.json").read_text(encoding="utf-8"))
    assert state == {"enabled": True, "interval_minutes": 30, "updated_at": state["updated_at"]}
    assert state["updated_at"]
    assert client.get("/api/autoloop").json()["enabled"] is True
    assert started == [True]


def test_disable_is_never_gated_and_omitted_interval_defaults(client, tmp_path):
    # gates closed: turning the loop OFF must still work (the safe direction)
    response = client.post("/api/autoloop", json={"enabled": False})
    assert response.status_code == 200
    state = json.loads((tmp_path / "autoloop.json").read_text(encoding="utf-8"))
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
    assert not (tmp_path / "autoloop.json").exists()


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
    thread = threading.Thread(target=autoloop_mod._scheduler_loop, daemon=True)
    thread.start()
    thread.join(timeout=1.0)
    assert calls == [1]


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

    trail = (tmp_path / "server-audit.jsonl").read_text(encoding="utf-8")
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

    def fetch_balance(self) -> dict:
        return {"total": {"USDT": _STUB_CASH}, "free": {"USDT": _STUB_CASH}}

    def fetch_ohlcv(self, symbol: str, timeframe: str, **kwargs):
        duration = 3_600_000 if timeframe == "1h" else 14_400_000
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        open_ms = now_ms - (now_ms % duration) - duration
        return [[open_ms, _STUB_PRICE, _STUB_PRICE + 100,
                 _STUB_PRICE - 100, _STUB_PRICE + 1, 1.0]]


class _FakeGraph:
    """propagate() returns a fixed signal and records the calls it saw."""

    def __init__(self, signal: str):
        self._signal = signal
        self.calls: list[tuple[str, str, str]] = []

    def propagate(self, ticker: str, trade_date: str, asset_type: str | None = None):
        self.calls.append((ticker, trade_date, str(asset_type)))
        return {}, self._signal


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

    trail = (tmp_path / "server-audit.jsonl").read_text(encoding="utf-8")
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
                    (tmp_path / "server-audit.jsonl").read_text(encoding="utf-8").splitlines()
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
    audit = server_paths.SERVER_AUDIT_PATH.read_text(encoding="utf-8")
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
    assert seen == ["BTC-USD", "ETH-USD", "AAPL", "MRNA"]
    assert [result.ticker for result in results] == seen
    assert autoloop_mod._last_cycle["status"] == "completed"
    assert autoloop_mod._last_cycle["candidates"] == seen


def test_cycle_candidate_timeout_is_terminal_and_does_not_execute(tmp_path, monkeypatch):
    engine_audit = tmp_path / "engine-audit.jsonl"
    set_config({"exec_auto_confirm": True, "exec_live": True, "exec_log_path": str(engine_audit)})
    monkeypatch.setenv("TRADINGAGENTS_EXEC_AUTO_CONFIRM", "true")
    monkeypatch.setenv("TRADINGAGENTS_EXEC_LIVE", "true")
    monkeypatch.setattr(autoloop_mod, "CYCLE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod, "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
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
    monkeypatch.setattr(autoloop_mod, "CYCLE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(screening_mod, "crypto_universe", lambda *_args: ["BTC-USD"])
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(
        screening_mod,
        "screen_universe",
        lambda *_args: [{"ticker": "BTC-USD", "asset_type": "crypto"}],
    )
    monkeypatch.setattr(autoloop_mod, "_bracket_prices", lambda *_args, **_kwargs: (90.0, 110.0))

    release_sync = threading.Event()
    sync_finished = threading.Event()
    submitted: list[object] = []

    class SlowBridge:
        config = {"exec_auto_confirm": True, "exec_live": True}

        def sync_portfolio(self):
            release_sync.wait(timeout=1.0)
            sync_finished.set()
            return PortfolioContext(cash=1_000.0)

        def plan_order(self, *args, **kwargs):
            return PlannedOrder(
                ticker="BTC-USD",
                ccxt_symbol="BTC/USDT",
                signal="Buy",
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

    assert autoloop_mod._run_cycle(lambda *_args: _FakeGraph("Buy")) == []
    assert autoloop_mod._last_cycle["status"] == "timeout"

    release_sync.set()
    assert sync_finished.wait(timeout=1.0)
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
    DerivativesExecutor (leverage clamped to the system cap) and the Buy's
    dry fill carries the LONG bracket (stop below entry, target above)."""
    engine_audit = _derivatives_cycle_env(tmp_path, monkeypatch)

    results = autoloop_mod._run_cycle(lambda config, analysts: _FakeGraph("Buy"))

    (btc,) = results
    assert btc.executed is True
    assert btc.plan.market == "derivatives"
    assert btc.plan.leverage == autoloop_mod.MAX_AUTO_LEVERAGE
    assert btc.plan.side == "buy"
    assert btc.plan.stop_loss < btc.plan.estimated_price < btc.plan.take_profit

    events = [json.loads(line) for line in engine_audit.read_text(encoding="utf-8").splitlines() if line.strip()] if engine_audit.exists() else []
    (plan_buy,) = [event for event in events if event.get("phase") == "plan" and event.get("action") == "buy"]
    (dry_buy,) = [event for event in events if event.get("phase") == "execute" and event.get("action") == "buy"]
    assert plan_buy["market"] == "derivatives" and plan_buy["leverage"] == autoloop_mod.MAX_AUTO_LEVERAGE
    assert dry_buy["mode"] == "dry" and dry_buy["confirmed"] is True
    assert all(event.get("mode") != "live" for event in events)


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
    autoloop_mod._process_futures_candidate(Derivatives(), "BTC-USD", "Buy", {}, result)
    assert result.reason == "awaiting approval"
    duplicate = autoloop_mod.DailyResult(ticker="BTC-USD")
    autoloop_mod._process_futures_candidate(Derivatives(), "BTC-USD", "Buy", {}, duplicate)
    assert duplicate.reason == "duplicate futures signal skipped"


def test_auto_leverage_clamped_to_system_cap():
    """The cycle's requested leverage = the configured risk cap clamped to
    MAX_AUTO_LEVERAGE; a missing entry means 1x (RiskLimits' default)."""
    assert autoloop_mod._auto_leverage({}) == 1.0
    assert autoloop_mod._auto_leverage({"risk_max_derivatives_leverage": 2.0}) == 2.0
    assert autoloop_mod._auto_leverage({"risk_max_derivatives_leverage": 10.0}) == autoloop_mod.MAX_AUTO_LEVERAGE
