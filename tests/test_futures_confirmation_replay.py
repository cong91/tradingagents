"""Deterministic replay + counterfactual tests for the futures confirmation gate.

``server/autoloop.py::_confirmation_state`` is the rule under test: a tradeable
signal may only enter when BOTH closed candles (1h and 4h) carry its direction,
and a doji candle refuses either side. This module replays that gate three ways,
all offline and look-ahead-free:

1. **Grouped rule tests** — every reject class the telemetry names: signal,
   1h direction, 4h direction, doji, bracket/R:R, intent. The intent group is
   pinned from the rule itself because the archived telemetry predates intent
   recording (zero intent rejects recorded; see the fixture's
   ``intent_rejects_recorded`` and ``confirmation_reject_missing_fields``).
2. **Telemetry replay audit** — the fixture
   ``tests/fixtures/futures_confirmation_replay.json`` (exported by
   ``scripts/export_futures_confirmation_replay.py`` from
   ``archive/legacy-server-data-20261021/control.db``) is replayed back through
   the live rule: every direction-bearing completed cycle must classify the way
   production recorded it, and the reject mix is pinned.
3. **Counterfactual harness** — the alternative rule the evidence was asked
   about (higher-timeframe dominance + a bounded lower-timeframe pullback)
   already lives in ``server/futures_replay.py`` with the real snapshot export
   ``tests/fixtures/futures_cycles_replay.json``; this module pins its replay
   integrity, invariants and metrics. The real sample scores 77 snapshot
   rows (1 of 58 confirmation-phase rejects): the counterfactual would
   confirm 3 additional candidate candles, but ALL 3 cycles were refused
   upstream at the intent gate — loosening the confirmation gate creates
   ZERO new entries in this sample, 2 of the 3 "confirmations" are
   negative-retrace range-breaks, and no flip has outcome data — which is
   why the production gate stays exactly as it is: see the rationale
   comment on ``_confirmation_state``.

No network: the conftest socket guard holds and no fixture reaches a venue.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import autoloop as autoloop_mod, futures_replay
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.dataflows.market_data import Candle, fetch_closed_candles
from tradingagents.execution.daily import DailyResult
from tradingagents.execution.futures_scheduler import closed_candle_pair

pytestmark = pytest.mark.unit

_BUY, _OVERWEIGHT, _HOLD, _UNDERWEIGHT, _SELL = RATINGS_5_TIER

FIXTURE = Path(__file__).parent / "fixtures" / "futures_confirmation_replay.json"

# A fixed UTC instant, far in the past: every replay decision below is a pure
# function of these candles, never of wall-clock or venue state.
AS_OF = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)
_MARK = 50_000.0
_CONFIG = {
    "risk_derivatives_max_risk_fraction": 0.02,
    "risk_derivatives_max_margin_fraction": 0.5,
}

_TRADEABLE = ("Buy", "Overweight", "Underweight", "Sell")


# --- deterministic candle fixtures ------------------------------------------------


def _open_ms(open_at: datetime) -> int:
    return int(open_at.timestamp() * 1000)


def _candle(timeframe: str, open_at: datetime, direction: str,
            *, open_: float = 100.0, close_: float = 101.0,
            high: float = 102.0, low: float = 99.0) -> Candle:
    """A closed candle with explicit OHLC; the direction comes from close-open."""
    duration_ms = 3_600_000 if timeframe == "1h" else 14_400_000
    if direction == "doji":
        open_, close_ = 100.5, 100.5
    elif direction == "bullish":
        open_, close_ = min(open_, close_), max(open_, close_)
    else:
        open_, close_ = max(open_, close_), min(open_, close_)
    return Candle(
        "BTC/USDT:USDT", timeframe, _open_ms(open_at), _open_ms(open_at) + duration_ms,
        open_, high, low, close_, 1.0, True,
    )


# The production alignment at AS_OF: latest closed 1h is [16:00, 17:00), latest
# closed 4h is [12:00, 16:00) — the 1h hour sits AFTER the 4h block it closes.
_ONE_HOUR_OPEN = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)
_FOUR_HOUR_OPEN = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _aligned_pair(direction: str) -> tuple[Candle, Candle]:
    return (
        _candle("1h", _ONE_HOUR_OPEN, direction),
        _candle("4h", _FOUR_HOUR_OPEN, direction),
    )


def _context(one_h: Candle, four_h: Candle) -> dict:
    """The subset of _closed_futures_context's output the gate replays."""
    return {
        "requested_ticker": "BTC-USD",
        "symbol": "BTC/USDT:USDT",
        "unified_symbol": "BTC/USDT:USDT",
        "one_hour": one_h,
        "four_hour": four_h,
        "one_hour_open": one_h.open_time,
        "one_hour_close": one_h.close,
        "four_hour_open": four_h.open_time,
        "four_hour_close": four_h.close,
        "one_hour_direction": autoloop_mod._candle_direction(one_h),
        "four_hour_direction": autoloop_mod._candle_direction(four_h),
    }


def _mark_derivatives(last: float | None) -> SimpleNamespace:
    """A venue client that answers the mark-price read only."""
    ticker = {} if last is None else {"last": last, "close": last}
    return SimpleNamespace(
        _bridge=SimpleNamespace(
            client=lambda: SimpleNamespace(fetch_ticker=lambda symbol: ticker),
        ),
    )


# --- capture seam for the rejection writers ----------------------------------------


@pytest.fixture()
def rejections(monkeypatch):
    """Replay runs never touch the store: capture the rows at the
    persist_rejection seam — the telemetry dict arrives merged into payload."""
    captured: list[dict] = []
    monkeypatch.setattr(
        autoloop_mod, "persist_futures_cycle_rejection",
        lambda cycle_id, **kwargs: captured.append({"cycle_id": cycle_id, **kwargs}),
    )
    return captured


def _run_candidate(**overrides) -> DailyResult:
    """Drive _process_futures_candidate to its first rejection and back.

    The non-empty cycle_payload skips the claim seam; guard_baseline skips the
    guard snapshot; persistence writers are captured by the ``rejections``
    fixture; the venue answers a constant mark. Nothing here touches a store,
    a network, or the clock.
    """
    kwargs = {
        "derivatives": _mark_derivatives(_MARK),
        "context": _context(*_aligned_pair("bullish")),
        "cycle_payload": {"effective_caps": {}},
        "guard_baseline": 0,
        "intent": "enter",
        "proposal": None,
    }
    kwargs.update(overrides)
    result = DailyResult(ticker="BTC-USD")
    autoloop_mod._process_futures_candidate(
        kwargs.pop("derivatives", _mark_derivatives(_MARK)), "BTC-USD",
        kwargs.pop("signal", "Buy"), _CONFIG, result, **kwargs
    )
    return result


# --- exact 1h+4h alignment baseline -------------------------------------------------


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return AS_OF.astimezone(tz) if tz is not None else AS_OF.replace(tzinfo=None)


class _CandleVenue:
    """A venue that serves canned candle rows per timeframe — no network."""

    def __init__(self, one_h: list[list], four_h: list[list]):
        self._rows = {"1h": one_h, "4h": four_h}

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None, **kwargs):
        return self._rows.get(timeframe, [])

    def fetch_funding_rate(self, symbol):
        return {}

    def fetch_open_interest(self, symbol):
        return {}


def test_closed_context_requires_exact_alignment_with_the_closed_pair(monkeypatch):
    """The alignment contract _closed_futures_context enforces: the served 1h
    and 4h rows must open exactly at closed_candle_pair(as_of) — this is the
    replay baseline; any drift refuses the cycle instead of confirming."""
    monkeypatch.setattr(autoloop_mod, "datetime", _FrozenDatetime)
    one, four = _aligned_pair("bullish")
    venue = _CandleVenue(
        one_h=[[one.open_time, one.open, one.high, one.low, one.close, one.volume]],
        four_h=[[four.open_time, four.open, four.high, four.low, four.close, four.volume]],
    )
    derivatives = SimpleNamespace(
        resolve_market=lambda ticker: "BTC/USDT:USDT",
        _bridge=SimpleNamespace(client=lambda: venue),
    )

    context = autoloop_mod._closed_futures_context(derivatives, "BTC-USD")

    pair_one, pair_four = closed_candle_pair(AS_OF)
    assert context is not None
    assert context["one_hour_open"] == int(pair_one[0].timestamp() * 1000)
    assert context["four_hour_open"] == int(pair_four[0].timestamp() * 1000)
    assert context["one_hour_direction"] == "bullish"
    assert context["four_hour_direction"] == "bullish"


@pytest.mark.parametrize("which", ["1h", "4h"])
def test_closed_context_is_fail_closed_when_the_pair_drifts(monkeypatch, which):
    """A forming bar is dropped by fetch_closed_candles and a stale bar fails
    the open-time equality: both leave the context None and the cycle refuses
    with confirmation_missing — never a confirmation from drifting candles."""
    monkeypatch.setattr(autoloop_mod, "datetime", _FrozenDatetime)
    one, four = _aligned_pair("bullish")
    if which == "1h":
        # Open at the CONTAINING hour: the forming bar (close_time > cutoff),
        # the only 1h row on offer.
        one_h = [[_open_ms(AS_OF), 100.0, 102.0, 99.0, 100.5, 1.0]]
        four_h = [[four.open_time, four.open, four.high, four.low, four.close, four.volume]]
    else:
        one_h = [[one.open_time, one.open, one.high, one.low, one.close, one.volume]]
        four_h = [[_open_ms(datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)),
                   100.0, 102.0, 99.0, 100.5, 1.0]]  # one 4h block stale
    venue = _CandleVenue(one_h=one_h, four_h=four_h)
    derivatives = SimpleNamespace(
        resolve_market=lambda ticker: "BTC/USDT:USDT",
        _bridge=SimpleNamespace(client=lambda: venue),
    )

    context = autoloop_mod._closed_futures_context(derivatives, "BTC-USD")

    assert context is None


def test_fetch_closed_candles_drops_the_forming_bar():
    """The adapter never returns a candle whose close is at/after as_of — the
    property the alignment equality above leans on."""
    forming_open = _open_ms(AS_OF)  # [17:00, 18:00) is forming at 17:00
    closed_open = _open_ms(_ONE_HOUR_OPEN)
    venue = _CandleVenue(
        one_h=[
            [forming_open, 100.0, 102.0, 99.0, 100.5, 1.0],
            [closed_open, 100.0, 102.0, 99.0, 101.0, 1.0],
        ], four_h=[],
    )
    candles = fetch_closed_candles(venue, "BTC/USDT:USDT", "1h", as_of=AS_OF, limit=2)
    assert [c.open_time for c in candles] == [closed_open]
    assert all(c.is_closed for c in candles)


# --- group: signal ------------------------------------------------------------------


@pytest.mark.parametrize("signal", ["Hold", "REVIEW", None])
def test_non_tradeable_signal_skips_confirmation_entirely(signal):
    """Hold/REVIEW/unparsed are not futures positions: the gate confirms them
    whatever the candles say — the signal group's reject lives downstream at
    signal_not_tradeable, not in confirmation."""
    ok, reason = autoloop_mod._confirmation_state(
        _context(*_aligned_pair("bearish")), signal)
    assert ok is True
    assert reason == "not_required_for_non_tradeable_signal"


@pytest.mark.parametrize("signal,expected", [
    ("Buy", "bullish"), ("Overweight", "bullish"),
    ("Underweight", "bearish"), ("Sell", "bearish"),
])
def test_tradeable_signal_maps_to_expected_direction(signal, expected):
    ok, reason = autoloop_mod._confirmation_state(
        _context(*_aligned_pair(expected)), signal)
    assert (ok, reason) == (True, "aligned_direction")


# --- group: 1h direction (lower timeframe) -------------------------------------------


@pytest.mark.parametrize("signal,aligned,against", [
    ("Buy", "bullish", "bearish"), ("Sell", "bearish", "bullish"),
])
def test_ltf_against_htf_aligned_rejects_direction_mismatch(signal, aligned, against):
    """4h agrees, the closed 1h does not: direction_mismatch — this is the
    pullback class the counterfactual rule below would admit with a bound."""
    ok, reason = autoloop_mod._confirmation_state(
        _context(_candle("1h", _ONE_HOUR_OPEN, against),
                 _candle("4h", _FOUR_HOUR_OPEN, aligned)), signal)
    assert (ok, reason) == (False, "direction_mismatch")


# --- group: 4h direction (higher timeframe) ------------------------------------------


@pytest.mark.parametrize("signal,aligned,against", [
    ("Buy", "bullish", "bearish"), ("Sell", "bearish", "bullish"),
])
def test_htf_against_ltf_aligned_rejects_direction_mismatch(signal, aligned, against):
    """1h agrees, the closed 4h does not: still direction_mismatch — the gate
    cannot tell the two apart on reason alone, which is exactly why the
    alternative rule's admission mix is unscorable on the archived rejects."""
    ok, reason = autoloop_mod._confirmation_state(
        _context(_candle("1h", _ONE_HOUR_OPEN, aligned),
                 _candle("4h", _FOUR_HOUR_OPEN, against)), signal)
    assert (ok, reason) == (False, "direction_mismatch")


# --- group: doji ----------------------------------------------------------------------


def test_doji_is_exact_close_equals_open():
    candle = Candle("BTC/USDT:USDT", "1h", 0, 1, 100.0, 101.0, 99.0, 100.0, 1.0, True)
    assert autoloop_mod._candle_direction(candle) == "doji"


@pytest.mark.parametrize("one_dir,four_dir", [
    ("doji", "bullish"), ("bullish", "doji"), ("doji", "doji"),
    ("doji", "bearish"),  # doji wins over the mismatch below it
])
def test_doji_in_either_closed_candle_rejects(one_dir, four_dir):
    ok, reason = autoloop_mod._confirmation_state(
        _context(_candle("1h", _ONE_HOUR_OPEN, one_dir),
                 _candle("4h", _FOUR_HOUR_OPEN, four_dir)), "Buy")
    assert (ok, reason) == (False, "doji_candle")


# --- group: bracket / R:R --------------------------------------------------------------


def _proposal(**overrides) -> dict:
    proposal = {
        "margin_mode": "isolated", "leverage": 2, "risk_fraction": 0.01,
        "margin_fraction": 0.25, "rationale": "aligned candles",
        "entry_price": _MARK,
    }
    proposal.update(overrides)
    return proposal


@pytest.mark.parametrize("proposal,mark,fragment", [
    (None, _MARK, "agent_proposal_missing"),
    ({}, _MARK, "agent_proposal_missing"),
    (_proposal(rationale=""), _MARK, "rationale is required"),
    (_proposal(margin_mode="naked"), _MARK, "not isolated/cross"),
    (_proposal(leverage=2.5), _MARK, "must be an integer"),
    (_proposal(leverage=True), _MARK, "must be an integer"),
    (_proposal(risk_fraction=float("inf")), _MARK, "must be finite"),
    (_proposal(leverage=0), _MARK, "outside 1-3"),
    (_proposal(leverage=4), _MARK, "outside 1-3"),
    (_proposal(risk_fraction=0.0), _MARK, "risk_fraction"),
    (_proposal(risk_fraction=0.05), _MARK, "risk_fraction"),
    (_proposal(margin_fraction=0.6), _MARK, "margin_fraction"),
    (_proposal(entry_price=_MARK * 1.01), _MARK, "non-market entry_price"),
    (_proposal(entry_price=-1.0), _MARK, "entry_price must be positive"),
    (_proposal(stop_loss=49_000.0), _MARK, "proposed together"),
    (_proposal(stop_loss=51_000.0, take_profit=51_000.0), _MARK,
     "does not protect"),
])
def test_proposal_terms_rejects_the_bracket_group(proposal, mark, fragment):
    terms, error = autoloop_mod._proposal_terms(
        proposal, mark=mark, direction="long", config=_CONFIG)
    assert terms is None
    assert fragment in (error or "")


def test_proposal_terms_keeps_a_protecting_bracket():
    terms, error = autoloop_mod._proposal_terms(
        _proposal(stop_loss=49_000.0, take_profit=51_000.0),
        mark=_MARK, direction="long", config=_CONFIG)
    assert error is None
    assert (terms["stop_loss"], terms["take_profit"]) == (49_000.0, 51_000.0)
    # The reward:risk of this fixture bracket — measured, never gated: no R:R
    # rule exists in the cycle today (see the fixture's plan_rr_summary).
    assert (terms["take_profit"] - _MARK) / (_MARK - terms["stop_loss"]) == pytest.approx(1.0)


def test_proposal_terms_leaves_levels_open_for_the_atr_fallback():
    """No stop/take in the proposal is valid: the cycle falls back to the
    engine's ATR bracket for the protective levels only."""
    terms, error = autoloop_mod._proposal_terms(
        _proposal(), mark=_MARK, direction="long", config=_CONFIG)
    assert error is None
    assert terms["stop_loss"] is None and terms["take_profit"] is None


def test_direction_brackets_reuses_the_callers_reads():
    brackets = {"long": (49_000.0, 51_000.0), "short": (51_000.0, 49_000.0)}
    assert autoloop_mod._direction_brackets("BTC-USD", brackets) == (
        (49_000.0, 51_000.0), (51_000.0, 49_000.0))


def test_invalid_fallback_bracket_is_reflected(monkeypatch, rejections):
    """When the ATR fallback bracket does not protect the mark the cycle
    refuses with its own reason — a replayed protective-bracket reject."""
    monkeypatch.setattr(autoloop_mod, "_bracket_prices",
                        lambda ticker, direction="long": (51_000.0, 49_000.0))
    result = _run_candidate(proposal=_proposal())

    assert result.reason == "invalid protective bracket"
    (row,) = rejections
    assert row["phase"] == "proposal"
    assert row["payload"]["proposal_validation"] == {
        "valid": False, "reason": "invalid protective bracket"}


def test_missing_mark_price_refuses_before_terms(rejections):
    result = _run_candidate(proposal=_proposal(), derivatives=_mark_derivatives(None))

    assert result.reason == "derivatives entry requires a price"
    (row,) = rejections
    assert row["phase"] == "proposal"
    assert row["payload"]["proposal_validation"] == {
        "valid": False, "reason": "derivatives entry requires a price"}


# --- group: intent (telemetry predates intent recording; rule pinned from code) ---------


@pytest.mark.parametrize("intent,expected_reason", [
    ("abstain", "agent_abstained"),
    ("manage_existing", "position_management_only"),
    (None, "structured_terms_unavailable"),
    ("swing", "invalid_futures_intent: swing"),
])
def test_intent_other_than_enter_is_refused_at_proposal(intent, expected_reason, rejections):
    """Every non-enter intent is a proposal-phase refusal recorded with the
    exact production vocabulary the funnel buckets on."""
    result = _run_candidate(intent=intent)

    assert result.reason == expected_reason
    (row,) = rejections
    assert row["phase"] == "proposal"
    assert row["payload"]["proposal_validation"] == {
        "valid": False, "reason": expected_reason}
    assert row["payload"]["execution_status"] == "not_started"


def test_confirmation_reject_records_phase_confirmation(rejections):
    """The replay of the gate's own rejection: intent=enter with a mismatched
    closed 1h lands on phase=confirmation with the exact reason the fixture's
    86 rows carry."""
    result = _run_candidate(
        context=_context(_candle("1h", _ONE_HOUR_OPEN, "bearish"),
                         _candle("4h", _FOUR_HOUR_OPEN, "bullish")))

    assert result.reason == "confirmation_missing"
    (row,) = rejections
    assert row["phase"] == "confirmation"
    assert row["payload"]["confirmation_reason"] == "direction_mismatch"
    assert row["payload"]["proposal_validation"] == {
        "valid": False, "reason": "direction_mismatch"}


def test_signal_not_tradeable_refuses_after_confirmation_passes(rejections):
    """Precedence replayed: a Hold with mismatched candles is NOT a direction
    reject — confirmation skips it, then the tradeable-signal gate refuses."""
    result = _run_candidate(
        signal="Hold",
        context=_context(_candle("1h", _ONE_HOUR_OPEN, "bearish"),
                         _candle("4h", _FOUR_HOUR_OPEN, "bullish")))

    assert result.reason == "signal_not_tradeable: Hold"
    (row,) = rejections
    assert row["phase"] == "proposal"
    assert row["payload"]["proposal_validation"] == {
        "valid": True, "reason": "not_required_for_non_tradeable_signal"}


# --- telemetry replay audit --------------------------------------------------------------


@pytest.fixture(scope="module")
def replay_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_funnel_matches_the_archived_telemetry(replay_fixture):
    """The replay's ground truth, pinned: 211 archived cycles split exactly
    into the funnel buckets job_store classifies."""
    assert len(replay_fixture["rows"]) == 211
    assert replay_fixture["funnel"] == {
        "completed_without_execution_status": 7,
        "confirmation_rejected": 86,
        "entered": 33,
        "execution_failed": 1,
        "market_rejected": 72,
        "not_executed": 9,
        "proposal_rejected": 3,
    }


def test_every_confirmation_reject_is_direction_mismatch(replay_fixture):
    """Zero doji rejects and zero of anything else: 86/86 rows carry
    direction_mismatch — and none recorded a doji case, so the doji branch has
    no production evidence either way."""
    assert replay_fixture["confirmation_reject_reasons"] == {"direction_mismatch": 86}


def test_confirmation_rejects_record_no_gate_inputs(replay_fixture):
    """The evidence gap, pinned: the archived reject payloads carry none of the
    fields the counterfactual needs — no candle pair, no signal, no intent —
    which is why the alternative rule cannot be scored on the real rejects.
    Current code persists the snapshot on every reject (autoloop.py passes
    snapshot=context into _record_futures_cycle_rejection); the archive simply
    predates it."""
    assert replay_fixture["intent_rejects_recorded"] == 0
    assert replay_fixture["confirmation_reject_payload_keys"] == [
        "confirmation_reason", "market_rejection_reason", "requested_ticker",
        "signal", "signal_reason", "unified_symbol",
    ]
    assert replay_fixture["confirmation_reject_missing_fields"] == [
        "snapshot.one_hour_direction", "snapshot.four_hour_direction",
        "signal_at_reject", "intent", "candle_ohlcv",
    ]


def test_completed_cycles_replay_consistently_through_the_live_gate(replay_fixture):
    """Row-level replay: every direction-bearing completed cycle must classify
    through _confirmation_state exactly as production recorded it — tradeable
    signals were aligned, non-tradeable signals skipped the rule."""
    replayed = 0
    for row in replay_fixture["rows"]:
        if row["status"] != "completed" or not row["one_hour_direction"]:
            continue
        context = {
            "one_hour_direction": row["one_hour_direction"],
            "four_hour_direction": row["four_hour_direction"],
        }
        ok, reason = autoloop_mod._confirmation_state(context, row["signal"])
        if row["signal"] in _TRADEABLE:
            # A tradeable completion could only have passed an aligned pair.
            expected = ("bearish"
                        if autoloop_mod._direction(row["signal"]) == "short"
                        else "bullish")
            assert (ok, reason) == (True, "aligned_direction"), row
            assert (row["one_hour_direction"], row["four_hour_direction"]) == (
                expected, expected), row
        else:
            assert (ok, reason) == (True, "not_required_for_non_tradeable_signal"), row
        replayed += 1
    assert replayed == 42  # every direction-bearing completed row replayed


def test_reject_rows_carry_no_directions_to_replay(replay_fixture):
    for row in replay_fixture["rows"]:
        if row["status"] == "rejected" and row["phase"] == "confirmation":
            assert row["one_hour_direction"] is None
            assert row["four_hour_direction"] is None


def test_plan_rr_distribution_is_measured_but_never_gated(replay_fixture):
    """R:R of the recorded plans: measured for the report, gated nowhere. No
    R:R rule exists in the cycle; the bracket group's only hard rule is that
    the bracket must protect the entry."""
    summary = replay_fixture["plan_rr_summary"]
    assert summary["plans_with_computable_rr"] == 40
    assert summary["min_rr"] == 0.7087
    assert summary["median_rr"] == 1.3539
    assert summary["max_rr"] == 1.8984


def test_direction_bearing_snapshot_matrix_groups_by_signal_and_directions(replay_fixture):
    """The grouping the ask names, over the real direction-bearing snapshots:
    short signals completed only on (bearish, bearish); the counter-moved
    completions are all non-tradeable signals the rule never gated. The 28th
    Underweight row is the archive's single failed cycle — its snapshot was
    already recorded before the entry failed."""
    matrix = {(m["signal"], m["one_hour_direction"], m["four_hour_direction"]): m["count"]
              for m in replay_fixture["direction_bearing_snapshot_matrix"]}
    assert matrix == {
        ("Hold", "bearish", "bearish"): 1,
        ("Hold", "bearish", "bullish"): 3,
        ("REVIEW", "bearish", "bullish"): 2,
        ("REVIEW", "bullish", "bearish"): 1,
        ("Sell", "bearish", "bearish"): 8,
        ("Underweight", "bearish", "bearish"): 28,
    }


# --- counterfactual: HTF dominance + bounded LTF pullback (server/futures_replay) -------


def test_replay_baseline_is_mirror_consistent_with_recorded_reasons():
    """Mirror-consistency, pinned: server.futures_replay replays all 77
    snapshot-bearing rows of tests/fixtures/futures_cycles_replay.json and the
    production rule agrees with every recorded confirmation reason — zero
    flips in either direction. This is NOT out-of-sample validation: the
    recorded reason on a snapshot row was derived at persist time by the same
    function this replay runs (_record_futures_cycle_rejection derives it via
    _confirmation_state when the payload carries snapshot+signal), so the
    agreement certifies the harness mirror, not the rule against fresh
    outcomes. The replay is deterministic and look-ahead-free: it reads only
    the closed-candle pair snapshotted at cycle time."""
    cycles = futures_replay.load_cycles()
    outcomes = futures_replay.replay_rule(cycles, futures_replay.RULES["baseline"])
    summary = futures_replay.summary(outcomes)

    assert len(cycles) == 551
    assert summary["total"] == 77  # 474 snapshot-free rows are skipped, never scored
    assert summary["rule_reasons"] == summary["recorded_reasons"]
    assert summary["flips_to_confirm"] == []
    assert summary["flips_to_reject"] == []


def test_replay_counterfactual_confirms_three_candles_but_zero_new_entries():
    """The counterfactual on the real snapshots, stated precisely: it would
    CONFIRM 3 additional candidate candles (all bounded pullbacks the baseline
    refused) and refuse nothing the baseline confirms — but NONE of the 3
    becomes an entry, because every flip cycle was already refused UPSTREAM at
    the intent gate (phase=proposal with an intent-level signal_reason; the
    intent gate runs before the confirmation check in
    _process_futures_candidate). Loosening the confirmation gate creates zero
    new entries in this sample — the gate stays. The positive evidence is
    thinner still: 2 of the 3 flips share ONE snapshot state and both carry a
    NEGATIVE retrace (1h close below the 4h low: a range-break the harness's
    bound passes and labels a pullback), and no flip has any outcome data."""
    cycles = futures_replay.load_cycles()
    by_id = {c.cycle_id: c for c in cycles}
    outcomes = futures_replay.replay_rule(
        cycles, futures_replay.RULES["htf_dominance_bounded_pullback"])
    summary = futures_replay.summary(outcomes)

    assert summary["confirmed_by_rule"] == 27
    assert summary["rejected_by_rule"] == 50
    assert summary["flips_to_reject"] == []
    flips = summary["flips_to_confirm"]
    assert len(flips) == 3
    for flip in flips:
        assert flip["rule_reason"] == "bounded_pullback"
        assert flip["recorded_confirmation_reason"] == "direction_mismatch"
        # Every flip is a short whose 4h agreed and whose 1h pulled back.
        assert flip["signal"] == "Underweight"
        assert (flip["one_hour_direction"], flip["four_hour_direction"]) == (
            "bullish", "bearish")
        # The reviewer's material finding, pinned: the flip cycle never
        # reached the confirmation gate — the intent gate refused it first
        # (phase=proposal, intent-level signal_reason), so a looser
        # confirmation rule creates zero new entries here.
        assert flip["phase"] == "proposal"
        cycle = by_id[flip["cycle_id"]]
        assert cycle.signal_reason in (
            "structured_terms_unavailable", "position_management_only",
            "agent_abstained")
        # Short retracement of each flip: 2 of 3 are NEGATIVE (1h close below
        # the 4h low — a range-break, not a pullback) and pass the harness
        # bound anyway; that edge is documented, not production behavior.
        one, four = cycle.context["one_hour"], cycle.context["four_hour"]
        assert (one.close - four.low) / (four.high - four.low) <= 0.2
    negative = [f for f in flips
                if by_id[f["cycle_id"]].context["one_hour"].close
                < by_id[f["cycle_id"]].context["four_hour"].low]
    assert len(negative) == 2
    # 2 of the 3 flips share one identical snapshot state (one candle state
    # drives two "confirmations").
    states = {
        (by_id[f["cycle_id"]].context["one_hour"].open,
         by_id[f["cycle_id"]].context["one_hour"].high,
         by_id[f["cycle_id"]].context["one_hour"].low,
         by_id[f["cycle_id"]].context["one_hour"].close,
         by_id[f["cycle_id"]].context["four_hour"].open,
         by_id[f["cycle_id"]].context["four_hour"].high,
         by_id[f["cycle_id"]].context["four_hour"].low,
         by_id[f["cycle_id"]].context["four_hour"].close)
        for f in flips}
    assert len(states) == 2
    # The only snapshot-bearing CONFIRMATION-phase reject is not among them.
    confirmation_phase = [o for o in outcomes
                          if o["status"] == "rejected" and o["phase"] == "confirmation"]
    assert len(confirmation_phase) == 1
    assert confirmation_phase[0]["cycle_id"] not in {f["cycle_id"] for f in flips}


def test_replay_groups_real_outcomes_by_signal_and_directions():
    """The ask's grouping, over the real replayable rows: every group the
    funnel records, grouped by signal, 1h direction and 4h direction. The
    shorts that completed all saw an aligned pair; every aligned pair for a
    short is (bearish, bearish) — 8 Sell + 15 Underweight rows."""
    cycles = futures_replay.load_cycles()
    outcomes = futures_replay.replay_rule(cycles, futures_replay.RULES["baseline"])
    grouped = futures_replay.group_counts(
        outcomes, "signal", "one_hour_direction", "four_hour_direction")
    assert grouped == {
        ("Hold", "bearish", "bearish"): 1,
        ("Sell", "bearish", "bearish"): 8,
        ("Sell", "bearish", "bullish"): 4,
        ("Sell", "bullish", "bearish"): 2,
        ("Sell", "bullish", "bullish"): 7,
        ("Underweight", "bearish", "bearish"): 15,
        ("Underweight", "bearish", "bullish"): 14,
        ("Underweight", "bullish", "bearish"): 14,
        ("Underweight", "bullish", "bullish"): 12,
    }


def test_counterfactual_admits_superset_of_the_current_rule():
    """Invariant 1: everything the current rule admits, the alternative admits
    too — each signal's aligned pair is never lost."""
    for signal in _TRADEABLE:
        for direction in ("bullish", "bearish"):
            context = _context(*_aligned_pair(direction))
            baseline = futures_replay.RULES["baseline"](context, signal)
            alternative = futures_replay.RULES["htf_dominance_bounded_pullback"](context, signal)
            if baseline[0]:
                assert alternative[0] is True, (signal, direction, baseline, alternative)


def test_counterfactual_keeps_htf_dominance_and_doji_hard():
    """Invariants 2–3 on the alternative rule: the 4h disagreement is still an
    absolute reject, a doji on either timeframe never confirms, and an unknown
    direction fails closed."""
    alternative = futures_replay.RULES["htf_dominance_bounded_pullback"]
    for signal, aligned, against in (("Buy", "bullish", "bearish"),
                                     ("Sell", "bearish", "bullish")):
        context = _context(_candle("1h", _ONE_HOUR_OPEN, aligned),
                           _candle("4h", _FOUR_HOUR_OPEN, against))
        assert alternative(context, signal) == (False, "htf_direction_mismatch")
    for one_dir, four_dir in (("doji", "bullish"), ("bullish", "doji"), ("doji", "doji")):
        context = _context(_candle("1h", _ONE_HOUR_OPEN, one_dir),
                           _candle("4h", _FOUR_HOUR_OPEN, four_dir))
        assert alternative(context, "Buy") == (False, "doji_candle")
    no_directions = _context(*_aligned_pair("bullish"))
    no_directions["one_hour_direction"] = None
    assert alternative(no_directions, "Buy") == (False, "missing_candle_direction")


def test_counterfactual_bounds_the_pullback():
    """Invariant 4: the 1h counter-move confirms only as a shallow pullback of
    the 4h range (retrace <= 20%, wick not dominating the body) — a deep
    retracement or a wick-dominated bar is a reject, and the current rule
    rejects every one of these cells."""
    alternative = futures_replay.RULES["htf_dominance_bounded_pullback"]
    baseline = futures_replay.RULES["baseline"]
    # Long: 4h bullish 100->101 (range 3.0); a bounded 1h pullback closes in
    # the top fifth of that range with a small counter-wick.
    shallow = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bearish",
                open_=101.8, close_=101.5, high=101.85, low=101.45),
        _candle("4h", _FOUR_HOUR_OPEN, "bullish"))
    deep = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bearish",
                open_=100.8, close_=100.5, high=100.85, low=100.45),
        _candle("4h", _FOUR_HOUR_OPEN, "bullish"))
    wicky = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bearish",
                open_=101.8, close_=101.5, high=101.85, low=100.8),
        _candle("4h", _FOUR_HOUR_OPEN, "bullish"))
    # Short mirror: 4h bearish 101->100; bounded 1h bounce closes in the
    # bottom fifth.
    shallow_short = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bullish",
                open_=99.2, close_=99.5, high=99.55, low=99.15),
        _candle("4h", _FOUR_HOUR_OPEN, "bearish"))
    deep_short = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bullish",
                open_=100.2, close_=100.5, high=100.55, low=100.15),
        _candle("4h", _FOUR_HOUR_OPEN, "bearish"))

    assert alternative(shallow, "Buy") == (True, "bounded_pullback")
    assert alternative(shallow_short, "Sell") == (True, "bounded_pullback")
    assert alternative(deep, "Buy") == (False, "ltf_pullback_too_deep")
    assert alternative(deep_short, "Sell") == (False, "ltf_pullback_too_deep")
    assert alternative(wicky, "Buy") == (False, "ltf_pullback_wick_dominant")
    for context, signal in ((shallow, "Buy"), (shallow_short, "Sell"),
                            (deep, "Buy"), (deep_short, "Sell"), (wicky, "Buy")):
        assert baseline(context, signal)[0] is False


def test_counterfactual_negative_retrace_range_break_passes_the_bound():
    """The reviewer-flagged harness edge, pinned: for a short the rule measures
    retrace = 1h.close - 4h.low, so a 1h candle that closes BELOW the 4h low (a
    range-break, retrace < 0) satisfies the <=0.2 bound and is labelled
    'bounded_pullback'. Two of the three real fixture flips are exactly this
    shape. The harness is analysis-only so this stays a documented edge — but
    any future production promotion of this rule MUST make it explicit config
    with a fail-closed case for out-of-range closes, not a silent pass."""
    alternative = futures_replay.RULES["htf_dominance_bounded_pullback"]
    baseline = futures_replay.RULES["baseline"]
    # Short: 4h bearish 203.74->201.75 (range 4.16); the 1h "bounce" closes at
    # 200.2, BELOW the 4h low 200.67 — retrace = -0.47, not a pullback at all.
    range_break = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bullish",
                open_=198.81, close_=200.2, high=200.57, low=198.34),
        _candle("4h", _FOUR_HOUR_OPEN, "bearish",
                open_=203.74, close_=201.75, high=204.83, low=200.67))
    assert alternative(range_break, "Sell") == (True, "bounded_pullback")
    assert baseline(range_break, "Sell") == (False, "direction_mismatch")


def test_replay_cycle_identity_drift_is_documented_not_scored():
    """Data quirk pinned: 22 of the 77 snapshot rows carry a cycle_id whose
    1h-open is exactly 1h NEWER than the snapshot's 1h candle (the identity
    context and the snapshot were two separate fetches). No look-ahead — the
    snapshotted candle closed before the identity hour — and the replay scores
    rows by their snapshot, never by cycle_id time; but grouping by cycle_id
    timestamps would misattribute rows. Only 1 of the 3 counterfactual flips
    carries a drifted identity (and for that one the identity's 4h block is 4h
    newer too: the cycle_id names a different candle pair entirely)."""
    cycles = [c for c in futures_replay.load_cycles() if c.had_snapshot]
    flipped_ids = {
        "futures:QNT-USD:2026-10-26T16:00:00+00:00:2026-10-26T21:00:00+00:00:1",
        "futures:QNT-USD:2026-10-27T00:00:00+00:00:2026-10-27T06:00:00+00:00:1",
        "futures:QNT-USD:2026-10-27T04:00:00+00:00:2026-10-27T07:00:00+00:00:1",
    }
    drifted, drifted_flips = 0, 0
    for cycle in cycles:
        match = re.search(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00):"
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00):1$", cycle.cycle_id)
        assert match, cycle.cycle_id
        identity_one = datetime.fromisoformat(match.group(2)).astimezone(timezone.utc)
        snapshot_one = datetime.fromtimestamp(
            cycle.context["one_hour"].open_time / 1000, tz=timezone.utc)
        if identity_one != snapshot_one:
            drifted += 1
            # The drift is always exactly one 1h candle newer, never older —
            # no look-ahead enters the replay through it.
            assert identity_one == snapshot_one + timedelta(hours=1)
            if cycle.cycle_id in flipped_ids:
                drifted_flips += 1
    assert drifted == 22
    assert drifted_flips == 1


def test_counterfactual_fails_closed_on_missing_data():
    """Invariant 5: the harness never scores a snapshot-free cycle (the only
    rows that flip are fully snapshotted ones), and a zero-range 4h candle
    cannot measure a pullback — nothing is admitted from absent data."""
    skipped = [c for c in futures_replay.load_cycles() if not c.had_snapshot]
    assert len(skipped) == 474
    scored = futures_replay.replay_rule(skipped, futures_replay.RULES["htf_dominance_bounded_pullback"])
    assert scored == []  # snapshot-free rows never reach a rule, never flip

    zero_range = _context(
        _candle("1h", _ONE_HOUR_OPEN, "bearish",
                open_=101.8, close_=101.5, high=101.85, low=101.45),
        _candle("4h", _FOUR_HOUR_OPEN, "bullish", open_=100.0, close_=101.0,
                high=100.0, low=100.0))
    assert futures_replay.RULES["htf_dominance_bounded_pullback"](
        zero_range, "Buy") == (False, "htf_zero_range")


def test_counterfactual_metrics_report():
    """The metrics the ask asks for, printed deterministically (visible with
    pytest -rP): the real-fixture replay of both rules plus a 12-cell shape
    matrix over explicit candle fixtures. The admission counts are asserted,
    so the report cannot drift."""
    cycles = futures_replay.load_cycles()
    for name, rule in futures_replay.RULES.items():
        summary = futures_replay.summary(futures_replay.replay_rule(cycles, rule))
        print(f"counterfactual | {name}: total={summary['total']} "
              f"confirmed={summary['confirmed_by_rule']} "
              f"flips_to_confirm={len(summary['flips_to_confirm'])} "
              f"flips_to_reject={len(summary['flips_to_reject'])} "
              f"reasons={summary['rule_reasons']}")

    cells = [
        # (label, signal, 1h direction, 1h OHLC overrides, 4h direction)
        ("aligned", "Buy", "bullish", {}, "bullish"),
        ("aligned", "Sell", "bearish", {}, "bearish"),
        ("bounded_pullback", "Buy", "bearish",
         {"open_": 101.8, "close_": 101.5, "high": 101.85, "low": 101.45}, "bullish"),
        ("bounded_pullback", "Sell", "bullish",
         {"open_": 99.2, "close_": 99.5, "high": 99.55, "low": 99.15}, "bearish"),
        ("pullback_too_deep", "Buy", "bearish",
         {"open_": 100.8, "close_": 100.5, "high": 100.85, "low": 100.45}, "bullish"),
        ("pullback_too_deep", "Sell", "bullish",
         {"open_": 100.2, "close_": 100.5, "high": 100.55, "low": 100.15}, "bearish"),
        ("htf_violation", "Buy", "bullish", {}, "bearish"),
        ("htf_violation", "Sell", "bearish", {}, "bullish"),
        ("ltf_doji", "Buy", "doji", {}, "bullish"),
        ("ltf_doji", "Sell", "doji", {}, "bearish"),
        ("htf_doji", "Buy", "bullish", {}, "doji"),
        ("both_doji", "Sell", "doji", {}, "doji"),
    ]
    alternative = futures_replay.RULES["htf_dominance_bounded_pullback"]
    baseline = futures_replay.RULES["baseline"]
    current_admits = alt_admits = 0
    for label, signal, one_dir, one_kwargs, four_dir in cells:
        one = _candle("1h", _ONE_HOUR_OPEN, one_dir, **one_kwargs)
        context = _context(one, _candle("4h", _FOUR_HOUR_OPEN, four_dir))
        ok_now, reason_now = baseline(context, signal)
        ok_alt, reason_alt = alternative(context, signal)
        current_admits += ok_now
        alt_admits += ok_alt
        print(f"counterfactual | {signal:4} | {label:18} | "
              f"current={str(ok_now):5}({reason_now}) | "
              f"alternative={str(ok_alt):5}({reason_alt})")
    print(f"counterfactual | matrix: current admitted {current_admits}/12 cells, "
          f"alternative {alt_admits}/12; real fixture: 77 replayed rows, baseline "
          f"mirror-consistent 1:1, counterfactual confirms 3 bounded-pullback "
          f"candles on cycles refused upstream at intent (zero new entries; "
          f"2/3 negative-retrace range-breaks; no outcome data) "
          f"-> production gate unchanged")
    assert current_admits == 2  # the two aligned cells only
    assert alt_admits == 4      # aligned + the two bounded-pullback cells
