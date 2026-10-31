"""Unit coverage for futures candidate filtering without PostgreSQL state."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from server import autoloop

pytestmark = pytest.mark.unit


class _MarketsBridge:
    def __init__(self, markets: dict[str, dict]):
        self._markets = markets
        self.calls: list[str] = []

    def client(self):
        return SimpleNamespace(load_markets=lambda: self._markets)

    def futures_symbol_for(self, ticker: str, *, markets: dict[str, dict]) -> str:
        self.calls.append(ticker)
        for symbol, market in markets.items():
            if (
                market.get("base") == ticker.split("-", 1)[0]
                and market.get("quote") == "USDT"
                and market.get("settle") == "USDT"
                and market.get("contract") is True
                and market.get("type") in {"swap", "future"}
                and market.get("active") is not False
            ):
                return symbol
        raise ValueError(f"no active USDT-settled futures contract for {ticker!r}")


class _Derivatives:
    def __init__(self, bridge):
        self._bridge = bridge


def _contract(base: str) -> dict:
    return {
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "contract": True,
        "type": "swap",
        "active": True,
    }


def test_screen_timeout_cancels_worker_and_prevents_overlap(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    cancel_events: list[threading.Event] = []
    monkeypatch.setattr(autoloop, "SCREENING_TIMEOUT_SECONDS", 0.01)

    def slow_screen(_crypto, _stocks, _date, _top_n, *, cancel_event):
        cancel_events.append(cancel_event)
        entered.set()
        release.wait(timeout=2.0)
        assert cancel_event.is_set()
        exited.set()
        return []

    with pytest.raises(TimeoutError, match="screening timed out"):
        autoloop._screen_with_timeout(
            lambda *_args: ["BTC-USD"],
            lambda *_args: [],
            slow_screen,
            {},
            "2026-10-07",
        )

    assert entered.wait(timeout=1.0)
    assert len(cancel_events) == 1
    assert cancel_events[0].is_set()
    with pytest.raises(TimeoutError, match="worker from a timed-out cycle is still stopping"):
        autoloop._screen_with_timeout(
            lambda *_args: pytest.fail("overlapping screen worker must not start"),
            lambda *_args: [],
            lambda *_args: [],
            {},
            "2026-10-07",
        )

    release.set()
    assert exited.wait(timeout=1.0)
    assert autoloop._screen_with_timeout(
        lambda *_args: ["BTC-USD"],
        lambda *_args: [],
        lambda crypto, stocks, _date, _top_n: [],
        {},
        "2026-10-07",
    ) == (["BTC-USD"], [], [])


def test_screen_universe_stops_before_next_ticker_after_cancellation(monkeypatch):
    from tradingagents.dataflows import screening

    cancel_event = threading.Event()
    calls: list[str] = []

    def screen_one(ticker, *_args, **_kwargs):
        calls.append(ticker)
        cancel_event.set()
        return None

    monkeypatch.setattr(screening, "_benchmark_momentum", lambda *_args: 0.0)
    monkeypatch.setattr(screening, "screen_one", screen_one)

    result = screening.screen_universe(
        ["BTC-USD", "ETH-USD"], ["AAPL"], "2026-10-07", 10,
        cancel_event=cancel_event,
    )

    assert result == []
    assert calls == ["BTC-USD"]


def test_screening_pool_expands_for_futures_but_not_spot():
    discovery_calls: list[tuple[int, int]] = []
    stock_calls: list[int] = []
    screen_calls: list[tuple[list[str], list[str], int]] = []

    def crypto_universe(_config, top_n):
        discovery_calls.append((top_n, len(discovery_calls)))
        return ["QNT-USD", "BTC-USD"]

    def stock_universe(_config, top_n):
        stock_calls.append(top_n)
        return ["AAPL"]

    def screen(crypto, stocks, _date, top_n):
        screen_calls.append((crypto, stocks, top_n))
        return []

    for derivatives_enabled in (True, False):
        autoloop._screen_with_timeout(
            crypto_universe,
            stock_universe,
            screen,
            {"exec_derivatives": derivatives_enabled},
            "2026-10-07",
        )

    assert [call[0] for call in discovery_calls] == [
        autoloop.AUTO_FUTURES_SCREEN_POOL,
        autoloop.AUTO_UNIVERSE_PER_ASSET,
    ]
    assert stock_calls == [autoloop.AUTO_UNIVERSE_PER_ASSET]
    assert screen_calls == [
        (["QNT-USD", "BTC-USD"], [], autoloop.AUTO_FUTURES_SCREEN_POOL),
        (["QNT-USD", "BTC-USD"], ["AAPL"], autoloop.AUTO_SCREEN_POOL),
    ]
    assert autoloop.AUTO_FUTURES_SCREEN_POOL == autoloop.AUTO_UNIVERSE_PER_ASSET * 2


def test_discovery_pool_refills_executable_futures_after_unusable_candidates(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    discovered: list[int] = []
    screened: list[tuple[list[str], list[str], int]] = []
    bridge = _MarketsBridge({"BTC/USDT:USDT": _contract("BTC")})
    candidates = [
        {"ticker": "QNT-USD", "asset_type": "crypto"},
        {"ticker": "AAPL", "asset_type": "stock"},
        {"ticker": "BTC-USD", "asset_type": "crypto"},
    ]

    def crypto_universe(_config, limit):
        discovered.append(limit)
        return ["QNT-USD", "BTC-USD"]

    def stock_universe(*_args):
        pytest.fail("futures discovery must not query the stock universe")

    def screen(crypto, stocks, _date, top_n):
        screened.append((crypto, stocks, top_n))
        # Simulate the ranked broad pool: a contractless crypto ranks above a
        # stock, while the executable crypto is lower in that same pool.
        assert crypto == ["QNT-USD", "BTC-USD"]
        assert stocks == []
        assert top_n == autoloop.AUTO_FUTURES_SCREEN_POOL
        return candidates

    crypto, stocks, screened_candidates = autoloop._screen_with_timeout(
        crypto_universe,
        stock_universe,
        screen,
        {"exec_derivatives": True},
        "2026-10-07",
    )
    selected, skipped, selection = autoloop._select_cycle_candidates(
        screened_candidates, _Derivatives(bridge), limit=1,
    )

    assert discovered == [autoloop.AUTO_FUTURES_SCREEN_POOL]
    assert screened == [(crypto, stocks, autoloop.AUTO_FUTURES_SCREEN_POOL)]
    assert crypto == ["QNT-USD", "BTC-USD"] and stocks == []
    assert [candidate["ticker"] for candidate in selected] == ["BTC-USD"]
    assert selected[0]["asset_type"] == "crypto"
    assert [entry["ticker"] for entry in skipped] == ["QNT-USD", "AAPL"]
    assert bridge.calls == ["QNT-USD", "BTC-USD"]
    assert selection["requested"] == 1
    assert selection["executable"] == 1
    assert selection["shortfall"] == 0
    assert selection["pool_exhausted"] is False


def test_preflight_reports_exhausted_futures_pool_and_rejects_unknown_asset_type(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    bridge = _MarketsBridge({"BTC/USDT:USDT": _contract("BTC")})
    candidates = [
        {"ticker": "QNT-USD", "asset_type": "crypto"},
        {"ticker": "AAPL", "asset_type": "stock"},
        {"ticker": "NO-TYPE"},
    ]

    selected, skipped, selection = autoloop._select_cycle_candidates(
        candidates, _Derivatives(bridge), limit=2,
    )

    assert selected == []
    assert [entry["ticker"] for entry in skipped] == ["QNT-USD", "AAPL", "NO-TYPE"]
    assert "unsupported asset_type" in skipped[2]["reason"]
    assert selection["executable"] == 0
    assert selection["selected"] == 0
    assert selection["shortfall"] == 2
    assert selection["pool_exhausted"] is True


def test_preflight_reports_metadata_failure_but_keeps_only_crypto(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    class DeadBridge:
        def client(self):
            raise OSError("venue down")

    candidates = [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "AAPL", "asset_type": "stock"},
    ]

    runnable, skipped, metadata_available = autoloop._preflight_candidates_with_metadata(
        candidates, _Derivatives(DeadBridge())
    )

    assert metadata_available is False
    assert [candidate["ticker"] for candidate in runnable] == ["BTC-USD"]
    assert [entry["ticker"] for entry in skipped] == ["AAPL"]
    assert "advisory only in futures mode" in skipped[0]["reason"]


def test_preflight_keeps_stock_screening_unchanged_without_futures_executor():
    candidates = [
        {"ticker": "AAPL", "asset_type": "stock"},
        {"ticker": "BTC-USD", "asset_type": "crypto"},
    ]

    runnable, skipped = autoloop._preflight_candidates(candidates, None)

    assert runnable == candidates
    assert skipped == []


# --- discovery refill -------------------------------------------------------
#
# A single broad pool is not enough: its top can be all contractless pairs (and,
# defensively, a stock-typed entry), so preflight leaves the cycle short while
# the LLM budget goes unspent. These tests pin the widening behaviour and the
# transparent exhaustion report, and that spot/daily selection is untouched.


class _RefillPool:
    """Discovery fake: a fixed market of ``available`` tickers, cut to ``limit``."""

    def __init__(self, tickers: list[str]):
        self.tickers = tickers
        self.requests: list[int] = []

    def __call__(self, _config, limit):
        self.requests.append(limit)
        return self.tickers[:limit]


class _SnapshotPool:
    """Discovery fake whose answer depends on the requested width.

    ``crypto_universe`` refetches the venue every call, so a widened round is a
    *fresh* snapshot: modelling it per-limit lets a test prove that a contract
    present in round 1 can be absent from the wider round.
    """

    def __init__(self, snapshots: dict[int, list[str]]):
        self.snapshots = snapshots
        self.requests: list[int] = []

    def __call__(self, _config, limit):
        self.requests.append(limit)
        return list(self.snapshots.get(limit, []))[:limit]


def _candidate(ticker: str, asset_type: str = "crypto", score: float = 1.0) -> dict:
    return {"ticker": ticker, "asset_type": asset_type, "score": score}


def _refill(
    pool: _RefillPool,
    screen,
    *,
    limit: int,
    derivatives,
    deadline: float | None = None,
):
    """Run one screen + preflight, then the refill, like ``_run_cycle`` does."""
    crypto, stocks, candidates = autoloop._screen_with_timeout(
        pool,
        lambda *_args: pytest.fail("futures discovery must not query the stock universe"),
        screen,
        {"exec_derivatives": True},
        "2026-10-07",
    )
    selected, skipped, selection = autoloop._select_cycle_candidates(
        candidates, derivatives, limit=limit,
    )
    return autoloop._refill_futures_candidates(
        pool,
        lambda *_args: pytest.fail("futures refill must not query the stock universe"),
        screen,
        {"exec_derivatives": True},
        "2026-10-07",
        derivatives,
        limit=limit,
        selected=selected,
        skipped=skipped,
        selection=selection,
        crypto=crypto,
        inspected=autoloop.AUTO_FUTURES_SCREEN_POOL,
        deadline=deadline,
    )[2:]  # drop the universes: these tests assert on selection only


def test_refill_pulls_lower_ranked_executable_futures_past_contractless_and_stock(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    # Only the three majors have a live USDT-settled contract; everything else
    # in the ranked pool is a spot-only pair.
    bridge = _MarketsBridge({
        "ETH/USDT:USDT": _contract("ETH"),
        "SOL/USDT:USDT": _contract("SOL"),
        "BNB/USDT:USDT": _contract("BNB"),
    })
    contractless = [f"NOC{index:03d}-USD" for index in range(60)]
    executable = ["ETH-USD", "SOL-USD", "BNB-USD"]
    # The market is wider than the first pool, so the executable contracts sit
    # below the initial cut and are only reachable by widening discovery.
    pool = _RefillPool(contractless + executable + [f"TAIL{index:03d}-USD" for index in range(57)])

    screened: list[tuple[list[str], list[str], int]] = []

    def screen(crypto, stocks, _date, top_n):
        screened.append((crypto, stocks, top_n))
        assert stocks == []
        ranked = [_candidate(ticker) for ticker in crypto]
        if len(screened) == 1:
            # A stock-typed entry can only reach the futures path if screening
            # mislabels it; preflight must drop it rather than plan it.
            ranked.insert(0, _candidate("AAPL", "stock", score=9.0))
        return ranked

    selected, skipped, selection = _refill(pool, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge))

    # Discovery widened exactly once: 60 -> 120 inspected tickers.
    assert pool.requests == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    assert [len(entry[0]) for entry in screened] == [60, 120]
    assert [entry[2] for entry in screened] == [60, 120]

    # The lower-ranked contracts filled the cycle; nothing stock-typed got in.
    assert [candidate["ticker"] for candidate in selected] == executable
    assert {candidate["asset_type"] for candidate in selected} == {"crypto"}
    assert "AAPL" not in [candidate["ticker"] for candidate in selected]

    skipped_tickers = [entry["ticker"] for entry in skipped]
    # The stock is dropped first, and no contractless pair is dropped twice
    # even though the wider round re-screens the same tickers.
    assert skipped_tickers[0] == "AAPL"
    assert len(set(skipped_tickers)) == len(skipped_tickers)
    assert set(contractless) <= set(skipped_tickers)
    assert not {"ETH-USD", "SOL-USD", "BNB-USD"} & set(skipped_tickers)
    assert "advisory only in futures mode" in skipped[0]["reason"]

    assert selection["executable"] == 3
    assert selection["selected"] == 3
    assert selection["shortfall"] == 0
    assert selection["pool_exhausted"] is False
    assert selection["refill_rounds"] == 1
    assert selection["discovery_universe"] == 120
    assert selection["discovery_pool"] == 120


def test_refill_does_not_persist_the_same_rejection_twice(monkeypatch):
    """A widened round re-screens the same pool; each rejection is written once."""
    recorded: list[str] = []
    monkeypatch.setattr(
        autoloop, "_record_futures_cycle_rejection",
        lambda ticker, **kwargs: recorded.append(ticker),
    )
    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH")})
    pool = _RefillPool(
        [f"NOC{index:03d}-USD" for index in range(60)] + ["ETH-USD"],
    )

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, _skipped, _selection = _refill(
        pool, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge),
    )

    assert [candidate["ticker"] for candidate in selected] == ["ETH-USD"]
    assert len(recorded) == len(set(recorded))  # no duplicate cycle rows
    assert set(recorded) == {f"NOC{index:03d}-USD" for index in range(60)}


def test_refill_keeps_contracts_a_wider_snapshot_no_longer_lists(monkeypatch):
    """A refill round must MERGE, not replace, the already-vetted candidates.

    ``crypto_universe`` refetches tickers every call (screening.py:138-167), so a
    wider round is a fresh snapshot, not a superset. A contract that already
    passed preflight must not be dropped just because the new ranking omits it —
    otherwise the cycle loses an executable candidate and misreports the
    shortfall.
    """
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    bridge = _MarketsBridge({
        "ETH/USDT:USDT": _contract("ETH"),
        "SOL/USDT:USDT": _contract("SOL"),
    })
    contractless = [f"NOC{index:03d}-USD" for index in range(58)]
    snapshots = {
        # Round 1 ranks both contracts inside the pool.
        autoloop.AUTO_FUTURES_SCREEN_POOL: ["ETH-USD", "SOL-USD"] + contractless,
        # Round 2 is a fresh, wider snapshot in which ETH-USD has dropped out.
        autoloop.AUTO_FUTURES_SCREEN_POOL * 2: contractless + ["SOL-USD"] + [
            f"TAIL{index:03d}-USD" for index in range(62)
        ],
    }
    pool = _SnapshotPool(snapshots)

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, _skipped, selection = _refill(
        pool, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge),
    )

    assert pool.requests[:2] == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    # ETH-USD still runs: it was executable in round 1 and the wider snapshot
    # simply omitted it.
    assert [candidate["ticker"] for candidate in selected] == ["ETH-USD", "SOL-USD"]
    assert selection["executable"] == 2  # the union of vetting, not the last snapshot
    assert selection["selected"] == 2
    assert selection["shortfall"] == 1
    assert selection["pool_exhausted"] is True


def test_refill_keeps_vetted_candidates_when_a_later_round_fails(monkeypatch):
    """A vendor failure in a refill round must not fail the cycle.

    The refill is an optimisation: whatever preflight already vetted in round 1
    must still run, and the failed round is reported instead.
    """
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH")})
    contractless = [f"NOC{index:03d}-USD" for index in range(59)]
    calls: list[int] = []

    def flaky_universe(_config, limit):
        calls.append(limit)
        if len(calls) > 1:
            raise RuntimeError("crypto universe vendor failed during refill")
        return ["ETH-USD"] + contractless

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, _skipped, selection = _refill(
        flaky_universe, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge),
    )

    assert calls == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    assert [candidate["ticker"] for candidate in selected] == ["ETH-USD"]
    assert selection["refill_rounds"] == 0  # the failed round does not count
    assert "RuntimeError" in selection["refill_error"]
    assert selection["executable"] == 1
    assert selection["pool_exhausted"] is True


def test_refill_stops_before_a_round_that_would_outlive_the_cycle_deadline(monkeypatch):
    """A refill round is capped by the cycle budget, so it cannot eat the run.

    Without the cap each round could burn ``SCREENING_TIMEOUT_SECONDS`` past the
    deadline and the post-refill ``_cycle_timeout`` would discard a full
    selection instead of running it.
    """
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    monkeypatch.setattr(autoloop, "SCREENING_TIMEOUT_SECONDS", 30.0)
    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH")})
    contractless = [f"NOC{index:03d}-USD" for index in range(59)]
    calls: list[int] = []

    def stalling_universe(_config, limit):
        calls.append(limit)
        if len(calls) > 1:
            time.sleep(1.5)  # would consume the whole budget if not capped
        return ["ETH-USD"] + contractless

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    started = time.monotonic()
    try:
        selected, _skipped, selection = _refill(
            stalling_universe, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge),
            deadline=time.monotonic() + 2.0,
        )
        elapsed = time.monotonic() - started
    finally:
        # The cancelled worker keeps the shared screening lock until it wakes;
        # do not let that leak into the next test.
        wait_until = time.monotonic() + 5.0
        while autoloop._screening_worker_lock.locked() and time.monotonic() < wait_until:
            time.sleep(0.05)

    # Capped at half the remaining budget (~1s), not at 30s.
    assert elapsed < 1.8
    # The stalled round did not cost the cycle the candidate round 1 vetted.
    assert [candidate["ticker"] for candidate in selected] == ["ETH-USD"]
    assert selection["refill_rounds"] == 0
    assert selection["executable"] == 1
    assert "TimeoutError" in selection["refill_error"]


def test_refill_does_not_start_a_round_when_the_cycle_budget_is_spent(monkeypatch):
    """No budget left means no round at all — the vetted selection still runs."""
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH")})
    contractless = [f"NOC{index:03d}-USD" for index in range(59)]
    calls: list[int] = []

    def counting_universe(_config, limit):
        calls.append(limit)
        return ["ETH-USD"] + contractless

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, _skipped, selection = _refill(
        counting_universe, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge),
        deadline=time.monotonic() - 1.0,  # the cycle budget is already gone
    )

    assert calls == [autoloop.AUTO_FUTURES_SCREEN_POOL]  # only the initial screen
    assert [candidate["ticker"] for candidate in selected] == ["ETH-USD"]
    assert selection["refill_rounds"] == 0
    assert "refill_error" not in selection
    assert selection["executable"] == 1
    assert selection["shortfall"] == autoloop.AUTO_TOP_N - 1


def test_refill_reports_pool_exhaustion_when_discovery_runs_dry(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    bridge = _MarketsBridge({})  # no live contracts at all
    pool = _RefillPool([f"NOC{index:03d}-USD" for index in range(60)])

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, skipped, selection = _refill(pool, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(bridge))

    # Discovery could not offer a wider pool than the one just inspected, so
    # the shortfall is reported instead of pretending the cycle was full.
    assert pool.requests == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    assert selected == []
    assert len(skipped) == 60
    assert selection["executable"] == 0
    assert selection["shortfall"] == autoloop.AUTO_TOP_N
    assert selection["pool_exhausted"] is True
    assert selection["refill_rounds"] == 1
    assert selection["discovery_universe"] == autoloop.AUTO_FUTURES_SCREEN_POOL * 2
    assert selection["discovery_pool"] == 60


def test_refill_skips_when_venue_metadata_is_unavailable(monkeypatch):
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)

    class DeadBridge:
        def client(self):
            raise OSError("venue down")

    pool = _RefillPool([f"NOC{index:03d}-USD" for index in range(60)])

    def screen(crypto, _stocks, _date, _top_n):
        return [_candidate(ticker) for ticker in crypto]

    selected, _skipped, selection = _refill(
        pool, screen, limit=autoloop.AUTO_TOP_N, derivatives=_Derivatives(DeadBridge()),
    )

    # Without market metadata a contractless pair is indistinguishable from an
    # unreachable venue, so the per-candidate path must decide: no refill.
    assert pool.requests == [autoloop.AUTO_FUTURES_SCREEN_POOL]
    assert len(selected) == autoloop.AUTO_TOP_N  # fail-open crypto candidates kept
    assert selection["market_metadata_available"] is False
    assert selection["shortfall"] is None
    assert selection["refill_rounds"] == 0


def test_refill_leaves_spot_selection_unchanged():
    candidates = [
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "AAPL", "asset_type": "stock"},
    ]
    selected, skipped, selection = autoloop._select_cycle_candidates(
        candidates, None, limit=autoloop.AUTO_TOP_N,
    )

    assert selected == candidates and skipped == [] and selection is None

    # Spot/daily selection never widens or re-screens: no derivatives executor
    # means the helper returns the preflight result untouched.
    assert autoloop._refill_futures_candidates(
        lambda *_args: pytest.fail("spot selection must not re-discover"),
        lambda *_args: pytest.fail("spot selection must not re-discover"),
        lambda *_args: pytest.fail("spot selection must not re-screen"),
        {},
        "2026-10-07",
        None,
        limit=autoloop.AUTO_TOP_N,
        selected=selected,
        skipped=skipped,
        selection=selection,
        crypto=["BTC-USD"],
        inspected=autoloop.AUTO_UNIVERSE_PER_ASSET,
    ) == (["BTC-USD"], [], candidates, [], None)


def test_cycle_refills_discovery_before_running_the_pipeline(monkeypatch):
    """``_run_cycle`` widens discovery itself, not just the selection helper."""
    from tradingagents.dataflows import screening as screening_mod
    from tradingagents.execution.daily import DailyResult

    monkeypatch.setattr(autoloop.audit_log, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(autoloop.runs_mod, "active_run_id", lambda: None)
    monkeypatch.setattr(autoloop.daily_mod, "active_job_id", lambda: None)
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    monkeypatch.setattr(autoloop, "get_config", lambda: {"exec_derivatives": True})

    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH"), "SOL/USDT:USDT": _contract("SOL")})
    monkeypatch.setattr(autoloop, "_build_bridge", lambda *_args: bridge)
    monkeypatch.setattr(autoloop, "_build_derivatives_executor", lambda *_args: _Derivatives(bridge))

    contractless = [f"NOC{index:03d}-USD" for index in range(60)]
    pool = _RefillPool(contractless + ["ETH-USD", "SOL-USD"])
    screened: list[tuple[int, int]] = []

    monkeypatch.setattr(screening_mod, "crypto_universe", pool)
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])

    def screen(crypto, stocks, _date, top_n, cancel_event=None):
        screened.append((len(crypto), top_n))
        assert stocks == []
        return [_candidate(ticker) for ticker in crypto]

    monkeypatch.setattr(screening_mod, "screen_universe", screen)

    ran: list[str] = []

    def fake_run_candidate(_bridge, candidate, *_args, **_kwargs):
        ran.append(candidate["ticker"])
        return DailyResult(ticker=candidate["ticker"])

    monkeypatch.setattr(autoloop, "_run_candidate_with_timeout", fake_run_candidate)

    autoloop._run_cycle(lambda *_args: pytest.fail("no graph should be built for the stub"))

    # Round 1 inspected 60 contractless pairs and found nothing executable;
    # round 2 widened to 120 and reached the two contracts at the tail.
    assert pool.requests == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    assert screened == [(60, 60), (62, 120)]
    assert ran == ["ETH-USD", "SOL-USD"]  # lower-ranked contracts filled the cycle
    selection = autoloop._last_cycle["candidate_selection"]
    assert selection["refill_rounds"] == 1
    assert selection["executable"] == 2
    assert selection["shortfall"] == autoloop.AUTO_TOP_N - 2
    assert selection["pool_exhausted"] is True
    assert autoloop._last_cycle["status"] == "completed"
    # Discovery covered the union of both rounds, not just the last snapshot.
    assert autoloop._last_cycle["universe"]["crypto"] == 62


def _wire_cycle(monkeypatch, pool, screen, bridge):
    """Install the fakes ``_run_cycle`` needs and return the executed tickers."""
    from tradingagents.dataflows import screening as screening_mod
    from tradingagents.execution.daily import DailyResult

    monkeypatch.setattr(autoloop.audit_log, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(autoloop.runs_mod, "active_run_id", lambda: None)
    monkeypatch.setattr(autoloop.daily_mod, "active_job_id", lambda: None)
    monkeypatch.setattr(autoloop, "_record_futures_cycle_rejection", lambda *args, **kwargs: None)
    monkeypatch.setattr(autoloop, "get_config", lambda: {"exec_derivatives": True})
    monkeypatch.setattr(autoloop, "_build_bridge", lambda *_args: bridge)
    monkeypatch.setattr(autoloop, "_build_derivatives_executor", lambda *_args: _Derivatives(bridge))
    monkeypatch.setattr(screening_mod, "crypto_universe", pool)
    monkeypatch.setattr(screening_mod, "stock_universe", lambda *_args: [])
    monkeypatch.setattr(screening_mod, "screen_universe", screen)

    ran: list[str] = []

    def fake_run_candidate(_bridge, candidate, *_args, **_kwargs):
        ran.append(candidate["ticker"])
        return DailyResult(ticker=candidate["ticker"])

    monkeypatch.setattr(autoloop, "_run_candidate_with_timeout", fake_run_candidate)
    autoloop._run_cycle(lambda *_args: pytest.fail("no graph should be built for the stub"))
    return ran


def test_cycle_runs_a_contract_the_wider_snapshot_dropped(monkeypatch):
    """The reviewer's repro: a widened round must not un-vet a contract.

    Round 1 vets ETH-USD and SOL-USD; round 2's fresh, wider snapshot omits
    ETH-USD. Replacing the selection lost ETH and reported executable=1 while
    two contracts were executable.
    """
    bridge = _MarketsBridge({
        "ETH/USDT:USDT": _contract("ETH"),
        "SOL/USDT:USDT": _contract("SOL"),
    })
    contractless = [f"NOC{index:03d}-USD" for index in range(58)]
    pool = _SnapshotPool({
        autoloop.AUTO_FUTURES_SCREEN_POOL: ["ETH-USD", "SOL-USD"] + contractless,
        autoloop.AUTO_FUTURES_SCREEN_POOL * 2: contractless + ["SOL-USD"] + [
            f"TAIL{index:03d}-USD" for index in range(62)
        ],
    })

    def screen(crypto, stocks, _date, _top_n, cancel_event=None):
        assert stocks == []
        return [_candidate(ticker) for ticker in crypto]

    ran = _wire_cycle(monkeypatch, pool, screen, bridge)

    assert ran == ["ETH-USD", "SOL-USD"]
    selection = autoloop._last_cycle["candidate_selection"]
    assert selection["executable"] == 2
    assert selection["shortfall"] == 1
    assert autoloop._last_cycle["status"] == "completed"


def test_cycle_completes_when_the_refill_round_fails(monkeypatch):
    """A vendor error during the refill must not fail or empty the cycle."""
    bridge = _MarketsBridge({"ETH/USDT:USDT": _contract("ETH")})
    contractless = [f"NOC{index:03d}-USD" for index in range(59)]
    calls: list[int] = []

    def flaky_universe(_config, limit):
        calls.append(limit)
        if len(calls) > 1:
            raise RuntimeError("crypto universe vendor failed during refill")
        return ["ETH-USD"] + contractless

    def screen(crypto, stocks, _date, _top_n, cancel_event=None):
        assert stocks == []
        return [_candidate(ticker) for ticker in crypto]

    ran = _wire_cycle(monkeypatch, flaky_universe, screen, bridge)

    assert calls == [autoloop.AUTO_FUTURES_SCREEN_POOL, autoloop.AUTO_FUTURES_SCREEN_POOL * 2]
    assert ran == ["ETH-USD"]  # the round-1 candidate still ran
    assert autoloop._last_cycle["status"] == "completed"
    selection = autoloop._last_cycle["candidate_selection"]
    assert "RuntimeError" in selection["refill_error"]
    assert selection["executable"] == 1
