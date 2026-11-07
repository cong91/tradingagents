"""Unit tests for the analyzed time-stop position review worker."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from server import position_review
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS


def _payload(**overrides):
    payload = {
        "position_key": "TIA/USDT:USDT:sell:entry-1",
        "bracket_task_id": "bracket:TIA/USDT:USDT:entry-1",
        "symbol": "TIA/USDT:USDT",
        "side": "short",
        "entry": 100.0,
        "mark": 105.0,
        "contracts": 1.0,
        "held_hours": 80.0,
        "max_hold_hours": 72.0,
        "time_stop_min_r": 1.0,
        "favorable_r": -0.5,
        "funding_r": 0.2,
        "fee_r": 0.01,
        "funding_note": None,
        "completed_targets": [],
        "close_side": "buy",
    }
    payload.update(overrides)
    return payload


class _FakeStore:
    """In-memory stand-in for the job_store position-state surface."""

    def __init__(self, fail_first: int = 0):
        self.states: dict[str, dict] = {}
        self.fail_first = fail_first
        self.saves: list[tuple[str, int, dict]] = []

    def load_position_state(self, position_key):
        return self.states.get(position_key)

    def save_position_state(self, position_key, expected_revision, state):
        self.saves.append((position_key, expected_revision, state))
        if self.fail_first > 0:
            self.fail_first -= 1
            return False
        current = self.states.get(position_key)
        if current is not None and current["revision"] != expected_revision:
            return False
        self.states[position_key] = {**state, "revision": expected_revision + 1}
        return True

    def list_futures_cycles(self, limit=20, symbol=None):
        return list(self.cycles) if hasattr(self, "cycles") else []


class _FakeBridge:
    exchange_id = "binance"

    def __init__(self):
        self.audit: list[dict] = []
        self.config = {"exec_base_url": "https://demo-api.binance.com", "exec_sandbox": True}

    def client(self):
        raise RuntimeError("no venue in unit tests")

    def _redact(self, message):
        return message

    def audit_plan(self, order, **kwargs):
        self.audit.append(kwargs)


class _FakeStructured:
    def __init__(self, verdict):
        self.verdict = verdict

    def invoke(self, prompt):
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class _FakeLLM:
    def __init__(self, verdict):
        self.verdict = verdict

    def with_structured_output(self, schema):
        return _FakeStructured(self.verdict)


class _FakeClient:
    def __init__(self, verdict):
        self.verdict = verdict

    def get_llm(self):
        return _FakeLLM(self.verdict)


def _seed(store: _FakeStore, position_key: str) -> None:
    store.states[position_key] = {
        "position_key": position_key,
        "revision": 1,
        "snapshot": {"side": "short", "phase": "initial", "stop": 110.0},
        "bracket_task_id": "bracket:TIA/USDT:USDT:entry-1",
    }


def test_review_schema_coerces_llm_junk():
    verdict = position_review.PositionHoldReview.model_validate({
        "verdict": "hold",
        "reasoning": "thesis intact",
        "review_after_hours": "24",
    })
    assert verdict.review_after_hours == 24.0
    assert position_review.PositionHoldReview.model_validate({
        "verdict": "close",
        "reasoning": "broken",
        "review_after_hours": "N/A",
    }).review_after_hours is None
    with pytest.raises(ValidationError):
        position_review.PositionHoldReview.model_validate({
            "verdict": "reduce", "reasoning": "half",
        })
    with pytest.raises(ValidationError):
        position_review.PositionHoldReview.model_validate({
            "verdict": "hold", "reasoning": "",
        })


def test_clamp_review_hours_bounds_and_default():
    assert position_review._clamp_review_hours(None, {}) == 24.0
    assert position_review._clamp_review_hours(float("nan"), {}) == 24.0
    assert position_review._clamp_review_hours(500.0, {}) == 72.0
    assert position_review._clamp_review_hours(0.5, {}) == 4.0
    assert position_review._clamp_review_hours(
        None, {"risk_derivatives_time_stop_review_interval_hours": 12.0},
    ) == 12.0


def test_prompt_contains_the_evidence_and_the_output_contract():
    prompt = position_review._build_prompt(_payload(), _FakeBridge())
    assert "TIA/USDT:USDT (short)" in prompt
    assert "price PnL: -0.500 R" in prompt
    assert "cumulative funding: +0.200 R" in prompt
    assert "minimum R (+1.000)" in prompt
    assert "ORIGINAL ENTRY THESIS" in prompt
    assert "unavailable" in prompt  # thesis and market reads fail closed
    assert NO_EXTERNAL_TOOLS in prompt


def test_prompt_marks_a_paying_short():
    class _RateClient:
        def fetch_funding_rate(self, symbol, params=None):
            return {
                "fundingRate": -0.0001,
                "nextFundingTimestamp": int(
                    (datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000
                ),
            }

    class _Bridge:
        def client(self):
            return _RateClient()

    prompt = position_review._build_prompt(_payload(), _Bridge())
    assert "PAYING" in prompt


def test_review_hold_persists_extension_and_audits(monkeypatch):
    store = _FakeStore()
    _seed(store, "TIA/USDT:USDT:sell:entry-1")
    monkeypatch.setattr(position_review, "job_store", store)
    verdict = position_review.PositionHoldReview(
        verdict="hold", reasoning="thesis intact", review_after_hours=12.0,
    )
    monkeypatch.setattr(
        position_review, "create_tier_client",
        lambda config, tier: _FakeClient(verdict),
    )
    bridge = _FakeBridge()

    position_review._review_once(
        "TIA/USDT:USDT:sell:entry-1", _payload(), bridge, {},
    )

    snapshot = store.states["TIA/USDT:USDT:sell:entry-1"]["snapshot"]
    assert snapshot["time_stop_review_status"] == "hold"
    assert snapshot["time_stop_review_count"] == 1
    until = datetime.fromisoformat(snapshot["time_stop_review_until"])
    assert timedelta(hours=11) < until - datetime.now(UTC) <= timedelta(hours=12)
    assert snapshot["time_stop_review_last"]["reasoning"] == "thesis intact"
    assert [event["action"] for event in bridge.audit] == ["time_stop_review_hold"]
    assert "next review in 12h" in bridge.audit[0]["reason"]


def test_review_close_persists_without_an_audit(monkeypatch):
    store = _FakeStore()
    _seed(store, "TIA/USDT:USDT:sell:entry-1")
    monkeypatch.setattr(position_review, "job_store", store)
    verdict = position_review.PositionHoldReview(
        verdict="close", reasoning="thesis broken",
    )
    monkeypatch.setattr(
        position_review, "create_tier_client",
        lambda config, tier: _FakeClient(verdict),
    )
    bridge = _FakeBridge()

    position_review._review_once(
        "TIA/USDT:USDT:sell:entry-1", _payload(), bridge, {},
    )

    snapshot = store.states["TIA/USDT:USDT:sell:entry-1"]["snapshot"]
    assert snapshot["time_stop_review_status"] == "close"
    assert snapshot["time_stop_review_last"]["verdict"] == "close"
    assert bridge.audit == []


def test_structured_fallback_persists_unavailable(monkeypatch):
    store = _FakeStore()
    _seed(store, "TIA/USDT:USDT:sell:entry-1")
    monkeypatch.setattr(position_review, "job_store", store)
    monkeypatch.setattr(
        position_review, "create_tier_client",
        lambda config, tier: _FakeClient(ValueError("model answered in prose")),
    )
    bridge = _FakeBridge()

    position_review._review_once(
        "TIA/USDT:USDT:sell:entry-1", _payload(), bridge,
        {"risk_derivatives_time_stop_review_grace_hours": 2.0},
    )

    snapshot = store.states["TIA/USDT:USDT:sell:entry-1"]["snapshot"]
    assert snapshot["time_stop_review_status"] == "unavailable"
    grace_until = datetime.fromisoformat(snapshot["time_stop_review_until"])
    assert timedelta(hours=1) < grace_until - datetime.now(UTC) <= timedelta(hours=2)
    assert [event["action"] for event in bridge.audit] == ["time_stop_review_unavailable"]


def test_worker_crash_persists_unavailable(monkeypatch):
    store = _FakeStore()
    _seed(store, "TIA/USDT:USDT:sell:entry-1")
    monkeypatch.setattr(position_review, "job_store", store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("prompt build failed")

    monkeypatch.setattr(position_review, "_build_prompt", boom)
    position_review._run_review(
        "TIA/USDT:USDT:sell:entry-1", _payload(),
        lambda: _FakeBridge(), {},
    )

    snapshot = store.states["TIA/USDT:USDT:sell:entry-1"]["snapshot"]
    assert snapshot["time_stop_review_status"] == "unavailable"
    assert "TIA/USDT:USDT:sell:entry-1" not in position_review._IN_FLIGHT


def test_cas_conflict_is_retried(monkeypatch):
    store = _FakeStore(fail_first=1)
    _seed(store, "TIA/USDT:USDT:sell:entry-1")
    monkeypatch.setattr(position_review, "job_store", store)
    verdict = position_review.PositionHoldReview(
        verdict="close", reasoning="stagnant",
    )
    monkeypatch.setattr(
        position_review, "create_tier_client",
        lambda config, tier: _FakeClient(verdict),
    )

    position_review._review_once(
        "TIA/USDT:USDT:sell:entry-1", _payload(), _FakeBridge(), {},
    )

    snapshot = store.states["TIA/USDT:USDT:sell:entry-1"]["snapshot"]
    assert snapshot["time_stop_review_status"] == "close"
    # Unrelated snapshot keys survive the merge.
    assert snapshot["side"] == "short"
    assert snapshot["phase"] == "initial"


def test_missing_position_state_aborts_the_persist(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(position_review, "job_store", store)
    verdict = position_review.PositionHoldReview(
        verdict="close", reasoning="gone",
    )
    monkeypatch.setattr(
        position_review, "create_tier_client",
        lambda config, tier: _FakeClient(verdict),
    )

    position_review._review_once(
        "TIA/USDT:USDT:sell:entry-1", _payload(), _FakeBridge(), {},
    )

    assert store.states == {}


def test_request_review_dedupes_concurrent_requests(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_review(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(position_review, "_review_once", slow_review)
    assert position_review.request_review(
        "TIA/USDT:USDT:sell:entry-1", _payload(),
        bridge_factory=_FakeBridge, config={},
    )
    started.wait(timeout=5)
    assert not position_review.request_review(
        "TIA/USDT:USDT:sell:entry-1", _payload(),
        bridge_factory=_FakeBridge, config={},
    )
    release.set()
    for _ in range(100):
        if "TIA/USDT:USDT:sell:entry-1" not in position_review._IN_FLIGHT:
            break
        time.sleep(0.05)
    assert "TIA/USDT:USDT:sell:entry-1" not in position_review._IN_FLIGHT
    assert position_review.request_review(
        "TIA/USDT:USDT:sell:entry-1", _payload(),
        bridge_factory=_FakeBridge, config={},
    )
    for _ in range(100):
        if "TIA/USDT:USDT:sell:entry-1" not in position_review._IN_FLIGHT:
            break
        time.sleep(0.05)


def test_entry_thesis_reads_the_latest_executed_cycle(monkeypatch):
    store = _FakeStore()
    store.cycles = [
        {"payload": {"execution_status": "failed", "agent_decision_note": "bad"}},
        {"payload": {}},
        {"payload": {"execution_status": "executed",
                     "agent_decision_note": "short ZEC: funding turned, OI unwinding"}},
        {"payload": {"execution_status": "executed",
                     "agent_decision_note": "older note"}},
    ]
    monkeypatch.setattr(position_review, "job_store", store)

    thesis = position_review._entry_thesis("TIA/USDT:USDT")

    assert thesis == "short ZEC: funding turned, OI unwinding"
    assert position_review._entry_thesis("") == ""


def test_position_worker_never_takes_longer_than_the_clamp():
    # The hold horizon is hard-bounded even when the model answers 500h.
    assert position_review._clamp_review_hours(500.0, {}) == 72.0
    assert position_review._clamp_review_hours(float("inf"), {}) == 24.0
