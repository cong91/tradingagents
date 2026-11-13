"""test autoloop cycle records."""

import pytest

from server import (
    autoloop_cycle_records,
    autoloop_futures_context,
    futures_cycle_store,
)
from tests.autoloop_runtime_fixtures import (
    _market_context,
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_derivative_metrics_reads_funding_and_open_interest():
    """The trader is told to justify leverage against funding, so the cycle has
    to hand it the venue's funding rate and open interest."""

    class Venue:
        def fetch_funding_rate(self, symbol):
            return {"fundingRate": 0.0001, "fundingDatetime": "2026-10-26T08:00:00Z",
                    "markPrice": 737.06}

        def fetch_open_interest(self, symbol):
            return {"openInterestAmount": 30_960_870_652.53, "openInterestValue": 2.2e10}

    metrics = autoloop_futures_context._futures_derivative_metrics(Venue(), "BNB/USDT:USDT")
    assert metrics["funding_rate"] == pytest.approx(0.0001)
    assert metrics["funding_datetime"] == "2026-10-26T08:00:00Z"
    assert metrics["mark_price"] == pytest.approx(737.06)
    assert metrics["open_interest"] == pytest.approx(30_960_870_652.53)
    assert metrics["open_interest_value"] == pytest.approx(2.2e10)


@pytest.mark.unit
def test_derivative_metrics_degrades_when_the_venue_cannot_answer():
    """A venue that does not publish these metrics must not fail the cycle."""

    class Venue:
        def fetch_funding_rate(self, symbol):
            raise RuntimeError("not supported")

        def fetch_open_interest(self, symbol):
            raise RuntimeError("not supported")

    metrics = autoloop_futures_context._futures_derivative_metrics(Venue(), "BNB/USDT:USDT")
    assert metrics["funding_rate"] is None
    assert metrics["open_interest"] is None


@pytest.mark.unit
def test_rejected_cycle_persists_the_candle_snapshot(monkeypatch):
    """A cycle that stops at the proposal still read the closed candles; without
    the snapshot the UI can only report "no data" for a cycle that had data."""
    claimed, updated = {}, {}

    def fake_claim(cycle_id, **kwargs):
        claimed.update(kwargs)
        return True

    def fake_update(cycle_id, **kwargs):
        updated.update(kwargs)

    monkeypatch.setattr(futures_cycle_store, "claim_futures_cycle", fake_claim)
    monkeypatch.setattr(futures_cycle_store, "update_futures_cycle", fake_update)

    autoloop_cycle_records._record_futures_cycle_rejection(
        "BNB-USD",
        unified_symbol="BNB/USDT:USDT",
        signal="Buy",
        signal_reason="agent_abstained",
        phase="proposal",
        snapshot=_market_context("BNB-USD"),
        agent_note="**Rating**: Underweight",
    )

    payload = claimed["payload"]
    assert payload["snapshot"]["one_hour_direction"] == "bullish"
    assert payload["snapshot"]["four_hour_close"] == pytest.approx(105.0)
    assert payload["confirmation_result"] is True
    assert payload["confirmation_reason"] == "aligned_direction"
    assert updated["phase"] == "proposal" and updated["status"] == "rejected"


@pytest.mark.unit
def test_rejected_cycle_records_a_direction_mismatch(monkeypatch):
    """A short signal against bullish candles must persist the mismatch, so the
    UI can say why instead of reporting no data."""
    claimed = {}

    def fake_claim(cycle_id, **kwargs):
        claimed.update(kwargs)
        return True

    monkeypatch.setattr(futures_cycle_store, "claim_futures_cycle", fake_claim)
    monkeypatch.setattr(futures_cycle_store, "update_futures_cycle", lambda *a, **k: None)

    autoloop_cycle_records._record_futures_cycle_rejection(
        "BNB-USD",
        unified_symbol="BNB/USDT:USDT",
        signal="Underweight",
        signal_reason="agent_abstained",
        phase="proposal",
        snapshot=_market_context("BNB-USD"),
    )

    payload = claimed["payload"]
    assert payload["confirmation_result"] is False
    assert payload["confirmation_reason"] == "direction_mismatch"
