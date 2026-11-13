"""The operating brief: one read-only aggregate, honest about what it cannot say.

Every test here states which contract it pins (plan §5.1 truthfulness list, UX
plan §7). The brief must never fabricate a zero, never merge "no signal" with
"pipeline failure", never claim exposure or heat it cannot prove, and never
write anything.
"""

from decimal import Decimal

import pytest

from tests.server_api_fixtures import (  # noqa: F401 - shared fixtures re-export
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# helpers


def _seed_cycle(cycle_id: str, *, status: str, phase: str, payload: dict,
                hour: int = 0) -> None:
    """Seed one terminal cycle. ``hour`` varies the candle pair: futures_cycles
    is UNIQUE on (symbol, 4h_open, 1h_open, strategy_version), so two cycles in
    one test must sit on different candles — exactly as the runtime claims them."""
    from server import futures_cycle_store

    candle = f"2026-11-10T{hour:02d}:00:00+00:00"
    assert futures_cycle_store.claim_futures_cycle(
        cycle_id,
        symbol="BTC/USDT:USDT",
        four_hour_open=candle,
        one_hour_open=candle,
        strategy_version="1",
    )
    futures_cycle_store.update_futures_cycle(cycle_id, status=status, phase=phase,
                                            payload=payload, terminal=True)


def _seed_coverage(*, status: str, warnings: list[str], manual: int,
                   window_start: str, window_end: str, account: str) -> None:
    from server import outcome_store
    from tradingagents.outcomes.coverage import build_coverage
    from tradingagents.outcomes.currency import CurrencyAmount

    outcome_store.upsert_coverage(build_coverage(
        account_id=account,
        window_start=window_start,
        window_end=window_end,
        fills_found=1,
        attributed_fill_count=1,
        venue_realized=[CurrencyAmount("USDT", Decimal("1"))],
        ledger_realized=[CurrencyAmount("USDT", Decimal("1"))],
        unattributed_manual_detected=manual,
        partial_entry_count=0,
        source_status=status,
        warnings=warnings,
        snapshot={"cycle_id": account},
    ))


def _fake_portfolio(*, notional_currency: str, equity_currency: str,
                    notional: float | None, equity: float | None,
                    read_status: str = "complete") -> dict:
    return {
        "generated_at": "2026-11-10T08:00:00Z",
        "currency": "USDT",
        "cash": 1.0,
        "equity_est": 2.0,
        "positions": [{"ticker": "BTC-USD", "quantity": 1.0}],
        "positions_source": "venue",
        "realized_pnl": {"total": 5.0, "today": 1.0, "closed_trades": 2,
                         "unknown_basis_closes": 0, "consecutive_losses": 0},
        "futures": {
            "equity": equity,
            "wallet_balance": None,
            "available_balance": None,
            "unrealized_pnl": 3.0,
            "venue_profile": "demo",
            "equity_currency": equity_currency,
            "wallet_balance_currency": None,
            "available_balance_currency": None,
            "unrealized_pnl_currency": "USDT",
            "account_scope": "derivatives",
            "positions": [{
                "symbol": "BTC/USDT:USDT", "side": "long", "notional": notional,
                "notional_currency": notional_currency,
            }],
            "positions_read": {"status": read_status, "skipped": 0, "issues": [],
                               "observed_at": "2026-11-10T08:00:00Z"},
            "status": "ok",
            "error": None,
        },
        "halted_today": False,
        "snapshot": {"context_id": "ctx", "epoch": "e1", "dirty": False,
                     "pending": False, "error": None},
        "warnings": [],
        "stale_after": 30,
    }


@pytest.fixture()
def fake_portfolio(monkeypatch):
    """Replace the brief's portfolio read so risk maths can be driven exactly."""
    def _install(body):
        import server.operating_brief as brief

        monkeypatch.setattr(brief, "_read_portfolio", lambda: body)
        return body

    return _install


# ---------------------------------------------------------------------------
# projection unit tests without the PostgreSQL-backed server fixture


class TestBriefProjections:
    @pytest.fixture(autouse=True)
    def _server_state(self):
        pass

    def test_activity_degrades_when_funnel_read_fails(self, monkeypatch):
        from server import brief_activity

        start = "2026-11-12T00:00:00Z"
        calls = []

        def broken_counts(value):
            calls.append(value)
            raise RuntimeError("db gone")

        monkeypatch.setattr(brief_activity, "_cycle_counts", broken_counts)
        monkeypatch.setattr(
            brief_activity, "_no_entry_reasons", lambda value: pytest.fail("unexpected second read")
        )
        section = brief_activity._activity_section(start)

        assert calls == [start]
        assert section == {
            "scope": "futures",
            "window": "today_utc",
            "cycles_today": None,
            "analysed_today": None,
            "entries_today": None,
            "no_entry_top_reasons": [],
            "pipeline_error_cycles": None,
            "reason": "the cycle funnel could not be read: RuntimeError",
            "note": brief_activity._NO_ENTRY_NOTE,
        }

    def test_brief_passes_utc_cutoff_at_activity_position(self, monkeypatch):
        from server import brief_active_forecast, brief_activity, operating_brief

        events = []

        def record(name, result):
            def call(*args):
                events.append((name, args))
                return result
            return call

        monkeypatch.setattr(operating_brief, "_read_portfolio", record("portfolio", {}))
        monkeypatch.setattr(operating_brief, "_status_section", record("status", {}))
        monkeypatch.setattr(operating_brief, "_results_section", record("results", {}))
        monkeypatch.setattr(operating_brief, "_risk_section", record("risk", {}))
        monkeypatch.setattr(operating_brief, "_today_start_iso", record("cutoff", "2026-11-12T00:00:00Z"))
        monkeypatch.setattr(brief_activity, "_activity_section", record("activity", {}))
        monkeypatch.setattr(operating_brief, "utc_now_iso", record("generated_at", "now"))
        monkeypatch.setattr(operating_brief, "_funnel_section", record("funnel", {}))
        monkeypatch.setattr(brief_active_forecast, "_active_forecast_section", record("forecast", {}))
        monkeypatch.setattr(operating_brief, "_experiments_section", record("experiments", {}))
        monkeypatch.setattr(operating_brief, "_needs_attention", record("attention", []))

        operating_brief.build_brief()

        assert [name for name, _ in events] == [
            "portfolio", "status", "results", "risk", "cutoff", "activity",
            "generated_at", "funnel", "forecast", "experiments", "attention",
        ]
        assert events[5][1] == ("2026-11-12T00:00:00Z",)

    def test_active_forecast_reports_read_failure(self, monkeypatch):
        from server import brief_active_forecast

        def broken_read():
            raise RuntimeError("db gone")

        monkeypatch.setattr(brief_active_forecast.db, "read", broken_read)
        assert brief_active_forecast._active_forecast_section() == {
            "state": "error",
            "status": "error",
            "reason": "the recorded forecasts could not be read: RuntimeError",
            "note": brief_active_forecast._ACTIVE_FORECAST_NOTE,
        }


# ---------------------------------------------------------------------------
# read-only contract


def test_brief_is_read_only(client):
    response = client.get("/api/brief")
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["stale_after"] == 30
    assert "not financial advice" in payload["disclaimer"]
    # No state-changing verb may be served on this path.
    assert client.post("/api/brief", json={}).status_code in (403, 405, 415)
    assert client.put("/api/brief", json={}).status_code in (403, 405, 415)
    assert client.delete("/api/brief").status_code in (403, 405, 415)


def test_brief_does_not_write_anything(client):
    from server import db

    def counts() -> tuple[int, int]:
        with db.read() as conn:
            cycles = conn.execute("SELECT count(*) AS n FROM futures_cycles").fetchone()["n"]
            outcomes = conn.execute("SELECT count(*) AS n FROM outcome_positions").fetchone()["n"]
        return int(cycles), int(outcomes)

    before = counts()
    client.get("/api/brief")
    client.get("/api/brief")
    assert counts() == before


# ---------------------------------------------------------------------------
# risk: only what is provable


def test_brief_claims_exposure_only_when_currencies_line_up(client, fake_portfolio):
    fake_portfolio(_fake_portfolio(notional_currency="USDT", equity_currency="USDT",
                                   notional=100.0, equity=1000.0))
    exposure = client.get("/api/brief").json()["risk"]["exposure"]
    assert exposure["value_pct"] == pytest.approx(10.0)
    assert exposure["currency"] == "USDT"
    assert exposure["reason"] is None


def test_brief_refuses_a_mixed_currency_exposure(client, fake_portfolio):
    fake_portfolio(_fake_portfolio(notional_currency="USD", equity_currency="USDT",
                                   notional=100.0, equity=1000.0))
    exposure = client.get("/api/brief").json()["risk"]["exposure"]
    assert exposure["value_pct"] is None
    assert "no FX conversion" in exposure["reason"]


def test_brief_refuses_exposure_from_an_unproven_position_read(client, fake_portfolio):
    fake_portfolio(_fake_portfolio(notional_currency="USDT", equity_currency="USDT",
                                   notional=100.0, equity=1000.0, read_status="partial"))
    risk = client.get("/api/brief").json()["risk"]
    assert risk["exposure"]["value_pct"] is None
    assert "not proven complete" in risk["exposure"]["reason"]
    # A partial read is not a flat account: the count stays unknown, not 0.
    assert risk["open_positions"]["futures"] is None


def test_brief_never_claims_open_heat(client, fake_portfolio):
    fake_portfolio(_fake_portfolio(notional_currency="USDT", equity_currency="USDT",
                                   notional=100.0, equity=1000.0))
    heat = client.get("/api/brief").json()["risk"]["heat"]
    assert heat["value_pct"] is None
    assert "not part of the position snapshot" in heat["reason"]


def test_brief_shows_limits_read_only(client):
    limits = client.get("/api/brief").json()["risk"]["limits"]
    assert "total_exposure_pct" in limits
    assert "open_heat_pct" in limits
    assert "read-only" in limits["note"]


def test_brief_counts_protection_from_executed_cycles(client):
    _seed_cycle("cycle-prot-missing", hour=0, status="completed", phase="entry", payload={
        "execution_status": "executed", "executed": True, "protection_state": "missing",
        "order_id": "1",
    })
    _seed_cycle("cycle-prot-ok", hour=1, status="completed", phase="entry", payload={
        "execution_status": "executed", "executed": True, "protection_state": "configured",
        "order_id": "2",
    })
    payload = client.get("/api/brief").json()
    protection = payload["risk"]["protection"]
    assert protection["configured"] == 1
    assert protection["missing"] == 1
    # Per-position protection is never asserted: venues positions carry no
    # durable binding to their entry order.
    assert protection["open_positions_verified"] is False
    assert "no durable binding" in protection["reason"]
    kinds = {item["kind"] for item in payload["needs_attention"]}
    assert "protection_unconfirmed" in kinds


def test_brief_survives_a_failing_portfolio_read(client, fake_portfolio):
    fake_portfolio(None)
    payload = client.get("/api/brief").json()
    # The brief still answers; only the account-dependent fields go unknown.
    assert payload["status"]["venue_profile"] is None
    assert payload["risk"]["open_positions"]["futures"] is None
    assert payload["risk"]["exposure"]["value_pct"] is None
    assert payload["portfolio"] is None


def test_brief_passes_the_portfolio_body_through_verbatim(client, fake_portfolio):
    body = _fake_portfolio(notional_currency="USDT", equity_currency="USDT",
                           notional=100.0, equity=1000.0)
    fake_portfolio(body)
    payload = client.get("/api/brief").json()
    # Identity + freshness must survive so the cards keep their own contracts.
    # (The wire is JSON, so this compares the transported body field by field.)
    assert payload["portfolio"] == body
    assert payload["portfolio"]["snapshot"]["context_id"] == "ctx"
    assert payload["portfolio"]["generated_at"] == body["generated_at"]
    assert payload["status"]["venue_profile"] == "demo"


def test_brief_reads_the_real_dry_mode_portfolio_path(client):
    """No monkeypatch: the real read path runs offline in dry mode."""
    payload = client.get("/api/brief").json()
    assert payload["portfolio"] is not None
    assert payload["status"]["exec_mode"] in ("dry", "live")
    assert payload["portfolio"]["stale_after"] == 30


# ---------------------------------------------------------------------------
# activity: no signal is not a pipeline failure


def test_brief_separates_no_signal_from_pipeline_error(client):
    _seed_cycle("cycle-market", hour=0, status="rejected", phase="market",
                payload={"market_rejection_reason": "no_tradeable_signal"})
    _seed_cycle("cycle-confirm", hour=1, status="rejected", phase="confirmation",
                payload={"confirmation_reason": "candle_not_confirmed"})
    _seed_cycle("cycle-failed", hour=2, status="failed", phase="execution",
                payload={"execution_status": "failed", "failure_reason": "bridge_error"})
    payload = client.get("/api/brief").json()
    activity = payload["activity"]

    assert activity["cycles_today"] == 3
    assert activity["entries_today"] == 0
    # The failure is counted on its own line, never as a "reason not to trade".
    assert activity["pipeline_error_cycles"] == 1
    codes = {row["code"] for row in activity["no_entry_top_reasons"]}
    assert codes == {"market_rejected", "confirmation_rejected"}
    assert all("pipeline_error" not in code for code in codes)
    assert "never merged" in activity["note"]


def test_brief_no_entry_reasons_carry_a_sample_cycle_and_top_three(client):
    for index in range(4):
        _seed_cycle(f"cycle-{index}", hour=index, status="rejected", phase="market",
                    payload={"market_rejection_reason": f"reason_{index}"})
    payload = client.get("/api/brief").json()
    reasons = payload["activity"]["no_entry_top_reasons"]
    assert len(reasons) == 3
    assert all(row["count"] == 1 for row in reasons)
    assert all(row["sample_cycle_id"] for row in reasons)


def test_brief_excludes_in_flight_cycles_from_no_entry(client):
    from server import futures_cycle_store

    # claim only: status 'claimed' — the decision has not been made yet.
    futures_cycle_store.claim_futures_cycle(
        "cycle-running", symbol="BTC/USDT:USDT", four_hour_open="2026-11-10T00:00:00+00:00",
        one_hour_open="2026-11-10T00:00:00+00:00", strategy_version="1",
    )
    payload = client.get("/api/brief").json()
    assert payload["activity"]["no_entry_top_reasons"] == []
    assert payload["activity"]["pipeline_error_cycles"] == 0
    assert payload["activity"]["cycles_today"] == 1


def test_brief_marks_the_activity_window_and_scope(client):
    activity = client.get("/api/brief").json()["activity"]
    # Cycles are the derivatives path; saying "futures" avoids implying Spot cover.
    assert activity["scope"] == "futures"
    assert activity["window"] == "today_utc"


# ---------------------------------------------------------------------------
# experiments (stage 2) surfaced, never promoted


def test_brief_reports_an_empty_experiment_registry_honestly(client):
    experiments = client.get("/api/brief").json()["experiments"]
    assert experiments["status"] == "unavailable"
    assert "empty" in experiments["reason"]


def test_brief_experiments_are_never_a_promotion(client):
    from server import experiment_store
    from tradingagents.experiments import artifacts
    from tradingagents.experiments.runner import build_manifest

    manifest = build_manifest(
        run_id="run-1", hypothesis="memory on/off", window="2026-06-01:2026-08-31",
        holdout_start="2026-09-01", horizon_days=1, embargo_days=6,
        seed=7, repeat=3, variants=["no_trade", "rule"], data_source="fixture",
        config={"a": 1},
    )
    manifest["manifest_digest"] = artifacts.manifest_digest(dict(manifest))
    experiment_store.register_run(manifest, artifact_dir="/tmp/exp_run1")

    experiments = client.get("/api/brief").json()["experiments"]
    assert experiments["status"] == "ok"
    assert experiments["latest"]["run_id"] == "run-1"
    assert experiments["latest"]["hypothesis"] == "memory on/off"
    assert "never a promotion" in experiments["note"]


# ---------------------------------------------------------------------------
# needs attention: prioritised, deduplicated, actionable


def test_brief_needs_attention_is_sorted_and_deduplicated(client):
    _seed_coverage(status="error", warnings=["venue_unreadable"], manual=0,
                   account="binance:demo:" + "d" * 64,
                   window_start="2026-11-10T00:00:00+00:00",
                   window_end="2026-11-10T01:00:00+00:00")
    _seed_coverage(status="error", warnings=["venue_unreadable_again"], manual=0,
                   account="binance:demo:" + "e" * 64,
                   window_start="2026-11-10T02:00:00+00:00",
                   window_end="2026-11-10T03:00:00+00:00")
    payload = client.get("/api/brief").json()
    items = payload["needs_attention"]
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)), "one entry per kind, never a repeated banner"
    assert ids.count("coverage_error") == 1
    assert [item["priority"] for item in items] == sorted(item["priority"] for item in items)
    # Only kinds with real evidence appear.
    assert {item["kind"] for item in items} >= {"coverage_error"}
    # Every actionable item routes somewhere.
    assert all(item["link"].startswith("/") for item in items)
    assert all(item["message_code"].startswith("brief.attention.") for item in items)


def test_brief_attention_keeps_the_most_urgent_priority_per_kind(client, monkeypatch):
    """A kind that is reported at two priorities must surface once, at P1."""
    import server.operating_brief as brief

    monkeypatch.setattr(brief, "_needs_attention", brief._needs_attention)
    items = brief._needs_attention(
        {"server_status": "degraded", "audit_log_readable": False,
         "halted_today": True, "halt_reason": "loss limit", "pending_approvals": 2},
        {"ledger": {"status": "unavailable", "reason": "empty",
                    "coverage": [{"status": "partial", "warnings": ["drift"]}]}},
        {"protection": {"missing": 1}},
        {"pipeline_error_cycles": 3},
        None,
    )
    kinds = [item["kind"] for item in items]
    assert len(kinds) == len(set(kinds))
    priorities = {item["kind"]: item["priority"] for item in items}
    assert priorities["risk_halted"] == 1
    assert priorities["coverage_partial"] == 2
    assert priorities["pipeline_errors"] == 3
    assert kinds == [item["kind"] for item in sorted(items, key=lambda i: (i["priority"], i["kind"]))]


def test_brief_attention_is_empty_when_nothing_is_wrong(client):
    assert client.get("/api/brief").json()["needs_attention"] == []


# ---------------------------------------------------------------------------
# status block


def test_brief_reports_spot_and_futures_modes_separately(client):
    status = client.get("/api/brief").json()["status"]
    # Spot and Futures are never collapsed into one "Auto" flag.
    assert "auto_spot" in status and "auto_futures" in status
    assert status["auto_spot"] is False and status["auto_futures"] is False
    assert status["any_auto_enabled"] is False


def test_brief_carries_the_funnel_buckets(client):
    _seed_cycle("cycle-funnel", hour=3, status="completed", phase="entry", payload={
        "execution_status": "executed", "executed": True, "protection_state": "configured",
    })
    funnel = client.get("/api/brief").json()["funnel"]
    assert funnel["status"] == "ok"
    assert funnel["buckets"]["entered"] == 1
    assert funnel["total"] == 1


# ---------------------------------------------------------------------------
# active kronos forecast: latest recorded Futures cycle


def _forecast_block(*, as_of=None, status="ok") -> dict:
    """The wire shape of ``ForecastBlock.as_json``: a failed block carries the
    status and reason and NO numbers."""
    from datetime import UTC, datetime, timedelta

    moment = as_of or (datetime.now(UTC) - timedelta(hours=1))
    if status != "ok":
        return {
            "symbol": "BTC/USDT:USDT", "status": status,
            "reason": "the isolated Kronos interpreter is absent",
            "direction": None,
            "calibration_label": "sample_ratio", "distribution_kind": "model_samples",
        }
    return {
        "symbol": "BTC/USDT:USDT", "status": status,
        "reason": None,
        "direction": "up",
        "calibration_label": "sample_ratio", "distribution_kind": "model_samples",
        "direction_counts": {"up": 9, "down": 5, "flat": 2},
        "direction_ratio": {"up": 0.5625, "down": 0.3125, "flat": 0.125},
        "band": {"low": 58000.0, "p50": 60100.0, "high": 62500.0},
        "band_spread_ratio": 0.0765, "reference_close": 60000.0,
        "data_open_time": 1762761600000,
        "as_of": moment.isoformat(), "horizon_hours": 24, "timeframe": "4h",
        "paths": 16, "per_path_samples": 1, "generation_kind": "forward",
        "temperature": 0.5, "model_revision": "test-rev", "artifact_hash": "abc",
        "latency_ms": 2500.0, "created_at": moment.isoformat(),
    }


def _seed_forecast_cycle(cycle_id: str, block: dict, *, policy: str = "use") -> None:
    _seed_cycle(cycle_id, hour=5, status="completed", phase="entry", payload={
        "execution_status": "executed", "executed": True,
        "kronos_forecast": block, "kronos_policy": policy,
    })


def test_brief_active_forecast_reports_the_latest_recorded_cycle(client):
    _seed_forecast_cycle("cycle-fc-1", _forecast_block(), policy="use")
    section = client.get("/api/brief").json()["active_forecast"]
    assert section["state"] == "ok"
    assert section["status"] == "ok"
    assert section["symbol"] == "BTC/USDT:USDT"
    assert section["quote_currency"] == "USDT"
    assert section["policy"] == "use"
    assert section["cycle_id"] == "cycle-fc-1"
    forecast = section["forecast"]
    assert forecast["paths"] == 16
    assert forecast["direction_counts"]["up"] == 9
    assert forecast["band"]["p50"] == 60100.0
    assert forecast["latency_ms"] == 2500.0
    assert forecast["model_revision"] == "test-rev"
    # The ratio travels with its denominator; nothing here claims a probability.
    assert forecast["calibration_label"] == "sample_ratio"


def test_brief_active_forecast_marks_an_elapsed_window_as_stale(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    stale_block = _forecast_block(as_of=datetime.now(UTC) - timedelta(hours=48))
    _seed_forecast_cycle("cycle-fc-stale", stale_block)
    section = client.get("/api/brief").json()["active_forecast"]
    assert section["state"] == "stale"
    assert section["status"] == "stale"


def test_brief_active_forecast_keeps_a_failed_block_numberless(client, monkeypatch):
    _seed_forecast_cycle("cycle-fc-fail", _forecast_block(status="unavailable"))
    section = client.get("/api/brief").json()["active_forecast"]
    assert section["state"] == "unavailable"
    # A failed block carries NO numbers: the numeric fields are absent.
    forecast = section["forecast"]
    assert forecast["status"] == "unavailable"
    assert "direction_ratio" not in forecast
    assert "band" not in forecast
    assert forecast["reason"]


def test_brief_active_forecast_is_empty_when_no_cycle_recorded_one(client, monkeypatch):
    section = client.get("/api/brief").json()["active_forecast"]
    assert section["state"] == "empty"
    assert "nothing is claimed" in section["reason"]


def test_brief_active_forecast_survives_a_read_failure(client, monkeypatch):
    import server.brief_active_forecast as forecast


    class Broken:
        def __enter__(self):
            raise RuntimeError("db gone")

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(forecast.db, "read", lambda: Broken())
    section = forecast._active_forecast_section()
    assert section["state"] == "error"
    assert "could not be read" in section["reason"]
