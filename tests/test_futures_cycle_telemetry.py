"""Unit tests for server/futures_cycle_telemetry.py.

The futures telemetry layer is the audit boundary between raw agent output
and the durable futures_cycles row: these tests pin the redaction contract
(no API keys/cookies, no free-text proposals), the typed decision coercion,
and the timestamp/terminal behavior of the persistence helpers.
"""

from datetime import datetime, timezone

import pytest

from server import futures_cycle_telemetry as fct


@pytest.mark.unit
def test_redact_payload_drops_credential_shaped_keys():
    payload = fct.redact_payload({
        "signal": "Buy",
        "api_key": "sk-proj-0123456789abcdef",
        "x_api_key": "leak",
        "cookies": "session=1",
        "authorization": "Bearer abc.def",
        "nested": {
            "refresh_token": "t0", "keep": 1,
            "list": [{"credential": "x", "ok": True}],
        },
    })
    assert payload == {"signal": "Buy", "nested": {"keep": 1, "list": [{"ok": True}]}}


@pytest.mark.unit
def test_redact_payload_scrubs_secret_values_inside_text():
    text = fct.redact_payload(
        "api_key=sk-proj-0123456789abcdef Authorization: Bearer abc.def.ghi cookie=x1234"
    )
    assert "sk-proj-0123456789abcdef" not in text
    assert "abc.def.ghi" not in text  # Bearer token body must not survive
    assert "x1234" not in text


@pytest.mark.unit
def test_redact_payload_drops_every_credential_key_shape():
    """Credential keys travel under several names — auth_token is the exact
    cookie name the X vendor integration stores (data_source_credentials.py)."""
    payload = fct.redact_payload({
        "auth_token": "v", "private_key": "v", "passphrase": "v",
        "token": "v", "session_token": "v", "apiSecret": "v", "api_secret": "v",
        "keep": 1,
    })
    assert payload == {"keep": 1}


@pytest.mark.unit
def test_redact_sensitive_text_scrubs_quoted_and_camelcase_assignments():
    """Secret values inside exception/prose text survive no matter which shape
    they were echoed back in: JSON, YAML-ish colon, or separator-less camel."""
    scrubbed = fct.redact_sensitive_text(
        '{"api_key": "abc123DEF456"} apiSecret=abc123DEF456 '
        "auth_token: abc123DEF456 private_key=abc123DEF456 "
        "passphrase=abc123DEF456"
    )
    assert "abc123DEF456" not in scrubbed
    # counters and ordinary config text are not credentials
    assert "max_tokens: 4096" in fct.redact_sensitive_text("max_tokens: 4096")


@pytest.mark.unit
def test_redact_sensitive_text_scrubs_secrets_inside_exception_prose():
    """Venue/HTTP errors embed the credential with no assignment separator —
    the channel failure_reason and execution_reason actually carry."""
    scrubbed = fct.redact_sensitive_text(
        "HTTPError 401 for key abc123DEF456; venue rejected key: abc123DEF456"
    )
    assert "abc123DEF456" not in scrubbed
    assert scrubbed.count("[REDACTED]") == 2
    # short tickers and bare counters after "key"/"token" stay readable
    assert "HTTPError 404 for key BTC-USD" in fct.redact_sensitive_text(
        "HTTPError 404 for key BTC-USD"
    )
    assert "token 4096" in fct.redact_sensitive_text("token 4096")


@pytest.mark.unit
def test_redact_payload_folds_non_json_types():
    """Venue payloads may carry Decimal/datetime/set/bytes; job_store._json
    would raise TypeError on them and take the whole cycle claim down."""
    import decimal

    folded = fct.redact_payload({
        "d": decimal.Decimal("1.5"), "bad": decimal.Decimal("Infinity"),
        "t": datetime(2026, 1, 1, 12, 0), "s": {1, 2}, "b": b"raw",
    })
    assert folded == {
        "d": 1.5, "bad": None, "t": "2026-01-01T12:00:00", "s": [1, 2], "b": "raw",
    }


@pytest.mark.unit
def test_redact_payload_maps_nonfinite_floats_to_null():
    assert fct.redact_payload({"a": float("nan"), "b": float("inf"), "c": 1.5}) == {
        "a": None, "b": None, "c": 1.5,
    }


@pytest.mark.unit
def test_redact_proposal_keeps_only_typed_numeric_terms():
    proposal = {
        "margin_mode": "isolated",
        "leverage": 3,
        "risk_fraction": 0.01,
        "margin_fraction": 0.25,
        "entry_price": None,
        "stop_loss": 80_000.0,
        "take_profit": 86_000.0,
        "rationale": "Aligned candles, isolated collateral, 1% risk.",
        "evidence": ["1h bullish", "4h bullish"],
    }
    redacted = fct.redact_proposal(proposal)
    assert redacted == {
        "margin_mode": "isolated", "leverage": 3, "risk_fraction": 0.01,
        "margin_fraction": 0.25, "entry_price": None,
        "stop_loss": 80_000.0, "take_profit": 86_000.0,
    }


@pytest.mark.unit
def test_redact_proposal_drops_unusable_values():
    assert fct.redact_proposal({
        "margin_mode": "sometimes", "leverage": float("nan"),
        "risk_fraction": "high", "stop_loss": None,
    }) == {"stop_loss": None}  # invalid values dropped, explicit None kept
    assert fct.redact_proposal("prose only") == {}


@pytest.mark.unit
def test_decision_telemetry_keeps_typed_fields_and_strips_free_text():
    state = {
        "trader_decision_telemetry": {
            "action": "Buy", "futures_execution_intent": "enter",
            "futures_execution_proposal_present": True, "structured_output": True,
        },
        "pm_decision_telemetry": {
            "rating": "Sell", "futures_execution_intent": "abstain",
            "futures_execution_status": "structured",
            "futures_execution_proposal_present": False, "structured_output": True,
        },
        "futures_execution_intent": "abstain",
        "futures_execution_status": "structured",
        "futures_execution_proposal": {"margin_mode": "cross", "rationale": "nope"},
        "final_trade_decision": "**Rating**: Sell\nThesis: wait.",
    }
    telemetry = fct.decision_telemetry(state, "Sell", lambda _text: "Sell")
    assert telemetry["trader_decision"] == {
        "action": "Buy", "futures_execution_intent": "enter",
        "futures_execution_proposal_present": True, "structured_output": True,
    }
    assert telemetry["pm_decision"]["rating"] == "Sell"
    assert telemetry["pm_decision"]["futures_execution_status"] == "structured"
    assert telemetry["pm_decision"]["futures_execution_proposal_present"] is False
    assert telemetry["agent_proposal"] == {"margin_mode": "cross"}
    assert telemetry["confirmation_result"] is None  # autoloop fills the verdict


@pytest.mark.unit
def test_decision_telemetry_coerces_untrusted_agent_fields():
    """LLM telemetry never reaches the row verbatim: unknown intents, statuses,
    ratings and actions are coerced to the safe enum or None."""
    state = {
        "trader_decision_telemetry": {"action": "STRONG BUY", "junk": "x"},
        "pm_decision_telemetry": {"rating": "BUY", "junk": "y"},
        "futures_execution_intent": "yolo",
        "futures_execution_status": "winging_it",
    }
    telemetry = fct.decision_telemetry(state, "Buy", lambda _text: "Nonsense")
    assert telemetry["trader_decision"]["action"] is None
    assert telemetry["trader_decision"]["futures_execution_intent"] == "structured_terms_unavailable"
    assert telemetry["pm_decision"]["rating"] == "REVIEW"
    assert telemetry["pm_decision"]["futures_execution_intent"] == "structured_terms_unavailable"
    assert telemetry["pm_decision"]["futures_execution_status"] == "not_applicable"
    assert "junk" not in telemetry["trader_decision"]
    assert "junk" not in telemetry["pm_decision"]


@pytest.mark.unit
def test_decision_telemetry_without_state_falls_back_to_parser():
    telemetry = fct.decision_telemetry({}, "Hold", lambda _text: "Hold")
    assert telemetry["trader_decision"]["action"] is None
    assert telemetry["trader_decision"]["futures_execution_intent"] == "structured_terms_unavailable"
    assert telemetry["pm_decision"]["rating"] == "Hold"


@pytest.mark.unit
def test_cycle_identity_uses_normalized_candle_opens():
    one_hour = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    four_hour = datetime(2026, 10, 27, 8, tzinfo=timezone.utc)
    context = {
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour_open": int(one_hour.timestamp() * 1000),
        "four_hour_open": int(four_hour.timestamp() * 1000),
    }
    cycle_id, four_open, one_open = fct.cycle_identity(context)
    assert cycle_id == f"futures:BTC/USDT:USDT:{four_hour.isoformat()}:{one_hour.isoformat()}:1"
    assert (four_open, one_open) == (four_hour.isoformat(), one_hour.isoformat())


@pytest.mark.unit
def test_cycle_identity_requires_candle_opens():
    with pytest.raises(ValueError, match="one_hour"):
        fct.cycle_identity({"unified_symbol": "BTC/USDT:USDT"})


@pytest.mark.unit
def test_persistence_helpers_redact_at_the_boundary(monkeypatch):
    """Redaction happens inside the persistence helpers: whatever reaches
    job_store is already stripped of credential-shaped keys."""
    claimed, updates = {}, []

    def fake_claim(cycle_id, **kwargs):
        claimed.update(kwargs, cycle_id=cycle_id)
        return True

    def fake_update(cycle_id, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(fct.job_store, "claim_futures_cycle", fake_claim)
    monkeypatch.setattr(fct.job_store, "update_futures_cycle", fake_update)

    assert fct.claim_cycle(
        "futures:BTC/USDT:USDT:x:y:1",
        symbol="BTC/USDT:USDT", four_hour_open="x", one_hour_open="y",
        payload={"unified_symbol": "BTC/USDT:USDT", "cookie": "session=1"},
    ) is True
    assert claimed["payload"]["unified_symbol"] == "BTC/USDT:USDT"
    assert "cookie" not in claimed["payload"]

    fct.persist_progress(
        "futures:BTC/USDT:USDT:x:y:1", phase="market",
        payload={"api_key": "sk-proj-0123456789abcdef"},
    )
    assert updates[0]["status"] == "running"
    assert "api_key" not in updates[0]["payload"]
    stamps = updates[0]["payload"]["phase_timestamps"]
    assert stamps["market_updated_at"]
    assert "market_finished_at" not in stamps  # a non-terminal phase keeps running


@pytest.mark.unit
def test_persist_progress_terminal_stamps_finished_at(monkeypatch):
    updates = []

    def fake_claim(cycle_id, **kwargs):
        return True

    def fake_update(cycle_id, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(fct.job_store, "claim_futures_cycle", fake_claim)
    monkeypatch.setattr(fct.job_store, "update_futures_cycle", fake_update)

    fct.persist_progress(
        "futures:BTC/USDT:USDT:x:y:1", phase="analysis",
        payload={}, timestamp="2026-10-27T12:00:00+00:00", terminal=True,
    )
    (update,) = updates
    stamps = update["payload"]["phase_timestamps"]
    assert stamps["analysis_updated_at"] == "2026-10-27T12:00:00+00:00"
    assert stamps["analysis_finished_at"] == "2026-10-27T12:00:00+00:00"


@pytest.mark.unit
def test_persist_rejection_marks_terminal_and_redacts(monkeypatch):
    claimed, updates = {}, []

    def fake_claim(cycle_id, **kwargs):
        claimed.update(kwargs, cycle_id=cycle_id)
        return True

    def fake_update(cycle_id, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(fct.job_store, "claim_futures_cycle", fake_claim)
    monkeypatch.setattr(fct.job_store, "update_futures_cycle", fake_update)

    fct.persist_rejection(
        "futures:BTC/USDT:USDT:x:y:1",
        symbol="BTC/USDT:USDT", four_hour_open="x", one_hour_open="y",
        phase="proposal",
        payload={"signal_reason": "agent_abstained", "authorization": "Bearer zzz"},
        status="rejected",
    )
    assert claimed["cycle_id"] == "futures:BTC/USDT:USDT:x:y:1"
    (update,) = updates
    assert "authorization" not in update["payload"]
    assert update["status"] == "rejected" and update["terminal"] is True
    assert "proposal_finished_at" in update["payload"]["phase_timestamps"]


@pytest.mark.unit
def test_effective_caps_reports_every_guard_limit():
    class Limits:
        max_derivatives_leverage = 5.0
        max_derivatives_exposure_pct = 80.0
        max_position_pct_per_asset = 40.0
        max_total_exposure_pct = 150.0

    class Guard:
        limits = Limits()

    class Derivatives:
        risk_guard = Guard()

    caps = fct.effective_caps(Derivatives(), auto_cap=3.0, config={})
    assert caps["effective_leverage"] == 3.0  # the auto cap binds below the guard cap
    assert caps["risk_guard_max_leverage"] == 5.0
    assert caps["auto_max_leverage"] == 3.0
    assert caps["max_derivatives_exposure_pct"] == 80.0
    assert caps["max_position_pct_per_asset"] == 40.0
    assert caps["max_total_exposure_pct"] == 150.0
    assert caps["max_risk_fraction"] == 0.02 and caps["max_margin_fraction"] == 0.5


@pytest.mark.unit
def test_snapshot_payload_serializes_dataclasses_and_redacts():
    from dataclasses import dataclass

    @dataclass
    class Candle:
        open_time: int
        close: float

    context = {
        "one_hour": Candle(1_700_000_000_000, 105.0),
        "note": "cookie=secret123",
        "api_key": "x",
    }
    snapshot = fct.snapshot_payload(context)
    assert snapshot["one_hour"] == {"open_time": 1_700_000_000_000, "close": 105.0}
    assert "secret123" not in snapshot["note"]
    assert "api_key" not in snapshot
