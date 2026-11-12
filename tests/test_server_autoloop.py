"""test server autoloop."""

import json

import pytest

from server import (
    autoloop as autoloop_mod,
    control_store,
    runs as runs_mod,
)
from tests.autoloop_runtime_fixtures import (
    _no_network as _no_network,
    _server_state as _server_state,
    _today,
    client as client,
)
from tradingagents.dataflows.config import set_config

pytestmark = pytest.mark.unit


def test_get_autoloop_defaults(client):
    body = client.get("/api/autoloop").json()
    assert body["generated_at"].endswith("Z")
    assert body["enabled"] is False
    assert body["interval_minutes"] == 1440
    assert body["running_cycle"] is False
    assert body["gates"] == {
        "auto_spot": False, "auto_futures": False, "any_auto_enabled": False,
    }
    assert body["modes"] == body["gates"]
    assert body["last_cycle"] is None


def test_enable_modes_closed_is_409_and_arms_nothing(client, tmp_path, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr(autoloop_mod, "ensure_started", lambda: started.append(True))
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: "started")

    response = client.post("/api/autoloop", json={"enabled": True})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "mode_closed"
    assert error["details"]["gates"] == {
        "auto_spot": False, "auto_futures": False, "any_auto_enabled": False,
    }

    assert control_store.load_autoloop_state() == {}  # a refused enable persists nothing
    assert client.get("/api/autoloop").json()["enabled"] is False
    assert started == []  # the scheduler was never armed by a refused enable


@pytest.mark.parametrize("spot,futures", [(True, False), (False, True), (True, True)])
def test_enable_with_mode_open_persists_state_and_arms_scheduler(
    client, tmp_path, monkeypatch, spot, futures,
):
    started: list[bool] = []
    monkeypatch.setattr(autoloop_mod, "ensure_started", lambda: started.append(True))
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: "started")

    # Execution authorization is separate from scheduler enablement.
    set_config({"auto_spot_enabled": spot, "auto_futures_enabled": futures, "exec_live": False})
    response = client.post("/api/autoloop", json={"enabled": True, "interval_minutes": 30})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["interval_minutes"] == 30
    expected = {"auto_spot": spot, "auto_futures": futures, "any_auto_enabled": True}
    assert body["gates"] == body["modes"] == expected

    state = control_store.load_autoloop_state()
    assert state == {"enabled": True, "interval_minutes": 30, "updated_at": state["updated_at"]}
    assert state["updated_at"]
    assert client.get("/api/autoloop").json()["enabled"] is True
    assert started == [True]


def test_disable_is_never_gated_and_omitted_interval_defaults(client, tmp_path):
    # Both modes closed: turning the loop OFF must still work.
    response = client.post("/api/autoloop", json={"enabled": False})
    assert response.status_code == 200
    state = control_store.load_autoloop_state()
    assert state["enabled"] is False
    assert state["interval_minutes"] == 1440  # default when omitted


@pytest.mark.parametrize("spot,futures", [(True, False), (False, True), (True, True)])
def test_scheduler_request_cannot_disable_an_enabled_mode(client, monkeypatch, spot, futures):
    started: list[bool] = []
    monkeypatch.setattr(autoloop_mod, "ensure_started", lambda: started.append(True))
    set_config({"auto_spot_enabled": spot, "auto_futures_enabled": futures})

    response = client.post("/api/autoloop", json={"enabled": False, "interval_minutes": 30})

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["modes"] == {
        "auto_spot": spot, "auto_futures": futures, "any_auto_enabled": True,
    }
    assert control_store.load_autoloop_state()["enabled"] is True
    assert started == [True]


@pytest.mark.parametrize("spot,futures", [(True, False), (False, True), (True, True)])
def test_scheduler_read_derives_enabled_from_modes_even_with_stale_state(client, spot, futures):
    autoloop_mod._save_state({"enabled": False, "interval_minutes": 30, "updated_at": "stale"})
    set_config({"auto_spot_enabled": spot, "auto_futures_enabled": futures})

    body = client.get("/api/autoloop").json()

    assert body["enabled"] is True
    assert body["interval_minutes"] == 30
    assert control_store.load_autoloop_state()["enabled"] is False


@pytest.mark.parametrize("remaining", ["auto_spot_enabled", "auto_futures_enabled", None])
def test_scheduler_state_rechecks_modes_after_one_or_both_are_disabled(client, remaining):
    set_config({"auto_spot_enabled": True, "auto_futures_enabled": True})
    autoloop_mod._save_state({"enabled": True, "interval_minutes": 30, "updated_at": "armed"})
    set_config({
        "auto_spot_enabled": remaining == "auto_spot_enabled",
        "auto_futures_enabled": remaining == "auto_futures_enabled",
    })

    body = client.get("/api/autoloop").json()

    assert body["enabled"] is (remaining is not None)
    assert body["modes"] == {
        "auto_spot": remaining == "auto_spot_enabled",
        "auto_futures": remaining == "auto_futures_enabled",
        "any_auto_enabled": remaining is not None,
    }
    assert body["gates"] == body["modes"]
    assert control_store.load_autoloop_state()["enabled"] is True


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


def _seed_cycle(cycle_id: str, symbol: str, *, status: str, phase: str,
                payload: dict | None = None) -> None:
    from server import job_store

    assert job_store.claim_futures_cycle(
        cycle_id, symbol=symbol, four_hour_open="2026-10-25T04:00:00+00:00",
        one_hour_open="2026-10-25T07:00:00+00:00", strategy_version="1",
    )
    job_store.update_futures_cycle(
        cycle_id, status=status, phase=phase, payload=payload or {}, terminal=True,
    )


def test_autoloop_cycles_summary_and_symbol_rollup(client):
    _seed_cycle(
        "c-market", "SNDKB-USD", status="rejected", phase="market",
        payload={"market_rejection_reason": "no active USDT-settled futures contract"},
    )
    _seed_cycle(
        "c-confirm", "BTC/USDT:USDT", status="rejected", phase="confirmation",
        payload={"signal": "Buy", "confirmation_reason": "doji_candle"},
    )
    _seed_cycle(
        "c-abstain", "AAVE/USDT:USDT", status="rejected", phase="proposal",
        payload={"signal": "Underweight", "signal_reason": "agent_abstained"},
    )
    _seed_cycle(
        "c-entered", "UNI/USDT:USDT", status="completed", phase="entry",
        # entered requires the executed confirmation: a completed row without it
        # must land in "other", never in the entered bucket.
        payload={"signal": "Sell", "signal_reason": "tradeable_signal",
                 "execution_status": "executed"},
    )

    body = client.get("/api/autoloop/cycles").json()

    assert body["summary"] == {
        "market_rejected": 1, "confirmation_rejected": 1, "forecast_deferred": 0, "proposal_rejected": 1,
        "entered": 1, "awaiting_approval": 0, "risk_rejected": 0,
        "not_executed": 0, "execution_failed": 0,
        "in_flight": 0, "interrupted": 0, "other": 0, "total": 4,
    }
    rollup = {row["symbol"]: row for row in body["symbols"]}
    assert set(rollup) == {"SNDKB-USD", "BTC/USDT:USDT", "AAVE/USDT:USDT", "UNI/USDT:USDT"}
    assert rollup["AAVE/USDT:USDT"]["total"] == 1
    assert rollup["AAVE/USDT:USDT"]["proposal_rejected"] == 1
    assert rollup["AAVE/USDT:USDT"]["last_signal"] == "Underweight"
    assert rollup["AAVE/USDT:USDT"]["last_signal_reason"] == "agent_abstained"
    assert rollup["SNDKB-USD"]["market_rejected"] == 1
    assert rollup["SNDKB-USD"]["last_market_rejection_reason"] == (
        "no active USDT-settled futures contract"
    )
    assert rollup["UNI/USDT:USDT"]["entered"] == 1
    assert body["symbols"][0]["last_updated"] >= body["symbols"][-1]["last_updated"]


def test_autoloop_cycles_summary_maps_execution_outcomes_explicitly(client):
    """``entered`` requires the executed confirmation. Awaiting approval, plan
    risk refusal, no-order and execute failure each keep their own bucket —
    including legacy rows whose terminal write pre-dated the phase split, which
    are mapped at read time from the outcome their payload recorded (history is
    never rewritten)."""
    _seed_cycle(
        "o-enter", "BTC/USDT:USDT", status="completed", phase="entry",
        payload={"signal": "Buy", "signal_reason": "tradeable_signal",
                 "execution_status": "executed", "executed": True},
    )
    _seed_cycle(
        "o-approve", "ETH/USDT:USDT", status="rejected", phase="execution",
        payload={"signal": "Buy", "execution_status": "awaiting_approval",
                 "execution_reason": "awaiting approval (auto-confirm env gate closed)"},
    )
    _seed_cycle(
        "o-risk", "SOL/USDT:USDT", status="rejected", phase="planning",
        payload={"signal": "Sell", "execution_status": "risk_rejected",
                 "execution_reason": "risk guard halted trading: exposure cap"},
    )
    _seed_cycle(
        "o-noorder", "XRP/USDT:USDT", status="rejected", phase="execution",
        payload={"signal": "Buy", "execution_status": "not_executed",
                 "plan_reason": "sizing refused: risk budget below minimum"},
    )
    _seed_cycle(
        "o-failed", "ADA/USDT:USDT", status="failed", phase="execution",
        payload={"signal": "Buy", "execution_status": "failed",
                 "failure_reason": "RuntimeError: venue down"},
    )
    # Legacy shapes from the pre-split contract: completed/entry rows whose
    # payload says awaiting approval / no order, a failed row still on the old
    # entry phase, and a completed row with no execution_status at all.
    _seed_cycle(
        "o-legacy-approve", "BNB/USDT:USDT", status="completed", phase="entry",
        payload={"execution_status": "awaiting_approval"},
    )
    _seed_cycle(
        "o-legacy-noorder", "DOGE/USDT:USDT", status="completed", phase="entry",
        payload={"execution_status": "not_executed"},
    )
    _seed_cycle(
        "o-legacy-failed", "LINK/USDT:USDT", status="failed", phase="entry",
        payload={"execution_status": "failed"},
    )
    _seed_cycle(
        "o-legacy-blind", "AVAX/USDT:USDT", status="completed", phase="entry",
        payload={"signal": "Buy"},
    )
    # A payload that records executed: false must never be counted as entered
    # even when status/phase/execution_status all look like an entry — the
    # fill confirmation is the only truth the funnel trusts.
    _seed_cycle(
        "o-contradiction", "DOT/USDT:USDT", status="completed", phase="entry",
        payload={"signal": "Buy", "execution_status": "executed", "executed": False},
    )

    body = client.get("/api/autoloop/cycles").json()

    assert body["summary"] == {
        "market_rejected": 0, "confirmation_rejected": 0, "forecast_deferred": 0, "proposal_rejected": 0,
        "entered": 1, "awaiting_approval": 2, "risk_rejected": 1,
        "not_executed": 2, "execution_failed": 2,
        "in_flight": 0, "interrupted": 0, "other": 2, "total": 10,
    }
    rollup = {row["symbol"]: row for row in body["symbols"]}
    assert rollup["BTC/USDT:USDT"]["entered"] == 1
    assert rollup["DOT/USDT:USDT"]["entered"] == 0
    assert rollup["ETH/USDT:USDT"]["awaiting_approval"] == 1
    assert rollup["SOL/USDT:USDT"]["risk_rejected"] == 1
    assert rollup["XRP/USDT:USDT"]["not_executed"] == 1
    assert rollup["ADA/USDT:USDT"]["execution_failed"] == 1
    # legacy rows are bucketed by their recorded outcome, not dropped
    assert rollup["BNB/USDT:USDT"]["awaiting_approval"] == 1
    assert rollup["BNB/USDT:USDT"]["entered"] == 0
    assert rollup["DOGE/USDT:USDT"]["not_executed"] == 1
    assert rollup["LINK/USDT:USDT"]["execution_failed"] == 1
    assert rollup["LINK/USDT:USDT"]["interrupted"] == 0
    assert rollup["AVAX/USDT:USDT"]["entered"] == 0
    # the newest cycle's outcome rides on status/phase/execution_status, so the
    # UI never derives "entered" from a signal reason
    assert rollup["ETH/USDT:USDT"]["last_status"] == "rejected"
    assert rollup["ETH/USDT:USDT"]["last_execution_status"] == "awaiting_approval"
    assert rollup["ADA/USDT:USDT"]["last_status"] == "failed"
    assert rollup["ADA/USDT:USDT"]["last_execution_reason"] == "RuntimeError: venue down"
    assert rollup["BTC/USDT:USDT"]["last_status"] == "completed"
    assert rollup["BTC/USDT:USDT"]["last_phase"] == "entry"
    assert rollup["BTC/USDT:USDT"]["last_execution_status"] == "executed"
    assert rollup["BTC/USDT:USDT"]["last_executed"] == "true"
    # the executed=false contradiction row rides on last_executed, so the
    # symbol rollup's "latest outcome" can never read as an entry either
    assert rollup["DOT/USDT:USDT"]["last_execution_status"] == "executed"
    assert rollup["DOT/USDT:USDT"]["last_executed"] == "false"
    assert rollup["DOT/USDT:USDT"]["other"] == 1
    assert rollup["BTC/USDT:USDT"]["other"] == 0


def test_autoloop_cycles_item_carries_every_audit_layer(client):
    """The drill-down payload is the audit surface: it must carry, for one
    cycle, the requested-vs-unified symbol, both candle directions, the
    confirmation verdict, both agents' typed decisions, the effective caps,
    the proposal validation, the RiskGuard decision, the execution outcome and
    order id, and the phase timestamps — everything needed to say WHICH layer
    blocked the cycle. The endpoint renders the stored payload verbatim, so a
    field that stops round-tripping here is a field the UI silently loses."""
    _seed_cycle(
        "a-blocked", "BTC/USDT:USDT", status="rejected", phase="proposal",
        payload={
            "requested_ticker": "BTC-USD",
            "unified_symbol": "BTC/USDT:USDT",
            "snapshot": {
                "one_hour_direction": "bearish",
                "four_hour_direction": "bullish",
                "one_hour_close": 67_000.0,
                "four_hour_close": 66_500.0,
            },
            "confirmation_result": False,
            "confirmation_reason": "direction_mismatch",
            "trader_decision": {
                "action": "Sell",
                "futures_execution_intent": "enter",
                "futures_execution_proposal_present": True,
                "structured_output": True,
            },
            "pm_decision": {
                "rating": "Sell",
                "futures_execution_intent": "enter",
                "futures_execution_status": "structured",
                "futures_execution_proposal_present": True,
                "structured_output": True,
            },
            "agent_proposal": {"leverage": 3, "margin_mode": "isolated"},
            "effective_caps": {
                "effective_leverage": 3.0,
                "max_derivatives_exposure_pct": 80.0,
                "max_position_pct_per_asset": 40.0,
            },
            "proposal_validation": {"valid": False, "reason": "direction_mismatch"},
            "risk_guard": {"allowed": None, "halted": None, "reason": "not_called"},
            "execution_status": "not_started",
            "order_id": None,
            "signal": "Sell",
            "signal_reason": "tradeable_signal",
            "phase_timestamps": {
                "market_context_finished_at": "2026-10-25T07:05:00+00:00",
                "decision_context_finished_at": "2026-10-25T07:05:02+00:00",
                "analysis_finished_at": "2026-10-25T07:09:00+00:00",
                "proposal_finished_at": "2026-10-25T07:09:01+00:00",
            },
        },
    )

    (item,) = client.get("/api/autoloop/cycles").json()["items"]
    payload = item["payload"]

    assert (payload["requested_ticker"], payload["unified_symbol"]) == (
        "BTC-USD", "BTC/USDT:USDT",
    )
    assert payload["snapshot"]["one_hour_direction"] == "bearish"
    assert payload["snapshot"]["four_hour_direction"] == "bullish"
    assert payload["confirmation_result"] is False
    assert payload["confirmation_reason"] == "direction_mismatch"
    assert payload["trader_decision"] == {
        "action": "Sell", "futures_execution_intent": "enter",
        "futures_execution_proposal_present": True, "structured_output": True,
    }
    assert payload["pm_decision"]["rating"] == "Sell"
    assert payload["pm_decision"]["futures_execution_status"] == "structured"
    assert payload["agent_proposal"] == {"leverage": 3, "margin_mode": "isolated"}
    assert payload["effective_caps"]["effective_leverage"] == 3.0
    assert payload["proposal_validation"] == {"valid": False, "reason": "direction_mismatch"}
    assert payload["risk_guard"] == {"allowed": None, "halted": None, "reason": "not_called"}
    assert payload["execution_status"] == "not_started"
    assert payload["order_id"] is None
    assert set(payload["phase_timestamps"]) >= {
        "market_context_finished_at",
        "decision_context_finished_at",
        "analysis_finished_at",
        "proposal_finished_at",
    }
    # Nothing above is dropped, re-keyed or coerced on the way back out.
    assert set(payload) >= {
        "requested_ticker", "unified_symbol", "snapshot", "confirmation_result",
        "confirmation_reason", "trader_decision", "pm_decision", "agent_proposal",
        "effective_caps", "proposal_validation", "risk_guard", "execution_status",
        "order_id", "phase_timestamps",
    }


def test_autoloop_cycles_symbol_filter_and_validation(client):
    _seed_cycle(
        "f-aave", "AAVE/USDT:USDT", status="rejected", phase="proposal",
        payload={"signal": "Sell", "signal_reason": "agent_abstained"},
    )
    _seed_cycle(
        "f-bnb", "BNB/USDT:USDT", status="rejected", phase="proposal",
        payload={"signal": "Underweight", "signal_reason": "agent_abstained"},
    )

    body = client.get(
        "/api/autoloop/cycles", params={"symbol": "AAVE/USDT:USDT"},
    ).json()
    assert [item["symbol"] for item in body["items"]] == ["AAVE/USDT:USDT"]
    # the summary/rollup stay global — the filter only narrows the items list
    assert body["summary"]["total"] == 2
    assert len(body["symbols"]) == 2

    response = client.get("/api/autoloop/cycles", params={"symbol": "   "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("spot,futures", [(True, False), (False, True), (True, True)])
def test_run_now_spawns_a_cycle_and_reports_already_running(client, monkeypatch, spot, futures):
    set_config({"auto_spot_enabled": spot, "auto_futures_enabled": futures})
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


def test_run_now_rejects_when_both_modes_are_disabled(client, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(autoloop_mod, "_spawn_cycle", lambda *args, **kwargs: calls.append(1))

    response = client.post("/api/autoloop/run-now", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mode_closed"
    assert calls == []


def test_run_now_skipped_busy_records_the_skip_trail(client, tmp_path):
    set_config({"auto_spot_enabled": True})
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
