"""Results projection for the read-only operating brief."""

from decimal import Decimal

import pytest

from tests.server_api_fixtures import (  # noqa: F401 - shared fixtures re-export
    _no_network as _no_network,
    _server_state as _server_state,
    client as client,
)

pytestmark = pytest.mark.unit


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


def _fake_portfolio() -> dict:
    return {
        "currency": "USDT",
        "realized_pnl": {
            "total": 5.0,
            "today": 1.0,
            "closed_trades": 2,
            "unknown_basis_closes": 0,
        },
        "futures": {
            "status": "ok",
            "unrealized_pnl": 3.0,
            "unrealized_pnl_currency": "USDT",
        },
    }


@pytest.fixture()
def fake_portfolio(monkeypatch):
    """Replace the brief's portfolio read for projection-focused tests."""
    def _install(body):
        import server.operating_brief as brief

        monkeypatch.setattr(brief, "_read_portfolio", lambda: body)
        return body

    return _install


def test_brief_reports_an_empty_ledger_as_unavailable_not_zero(client):
    payload = client.get("/api/brief").json()
    ledger = payload["results"]["ledger"]
    assert ledger["status"] == "unavailable"
    assert "not zero" in ledger["reason"]
    assert ledger["totals"] is None


def test_brief_keeps_verified_and_estimated_separate(client, fake_portfolio):
    from server import outcome_store

    outcome_store.record_outcome(
        account_id="binance:demo:" + "a" * 64,
        position={
            "cycle_id": "cycle-v", "client_order_id": "ta_v", "venue_order_id": "1",
            "symbol": "BTC/USDT:USDT", "market_type": "perpetual", "side": "long",
            "planned_quantity": Decimal("1"), "filled_quantity": Decimal("1"),
            "opened_at": "2026-11-10T00:00:00+00:00",
            "closed_at": "2026-11-10T05:00:00+00:00",
            "attribution": "managed", "strategy_version": "1", "provenance": {},
        },
        fills=[{
            "venue_trade_id": "901", "venue_order_id": "1", "symbol": "BTC/USDT:USDT",
            "side": "sell", "role": "exit", "quantity": Decimal("1"),
            "price": Decimal("105"), "fee": Decimal("0.1"), "fee_currency": "USDT",
            "realized_pnl": Decimal("5"), "realized_pnl_currency": "USDT",
            "occurred_at": "2026-11-10T05:00:00+00:00",
        }],
        exits=[], funding=[],
    )
    fake_portfolio(_fake_portfolio())
    payload = client.get("/api/brief").json()

    # The ledger block carries reconciled money only.
    assert payload["results"]["ledger"]["status"] == "partial"
    assert payload["results"]["ledger"]["totals"]["realized_by_currency"]["USDT"] == "5"
    estimated = payload["results"]["estimated"]
    assert estimated["spot_audit_realized_total"] == 5.0
    assert "net_by_currency" not in estimated
    assert estimated["futures_unrealized"] == 3.0


class TestBriefResultsProjection:
    @pytest.fixture(autouse=True)
    def _server_state(self):
        pass

    def test_retains_closure_evidence_summary(self, monkeypatch):
        from server import brief_results

        ledger = {
            "status": "partial",
            "positions": {
                "total": 3,
                "closed": 2,
                "closure_evidence": {
                    "status": "partial",
                    "verified_closed": 2,
                    "unverified_positions": 1,
                    "unverified_closed_timestamps": 1,
                    "reason": "one position lacks complete closure evidence",
                },
            },
            "coverage": [],
        }
        monkeypatch.setattr(brief_results.outcome_report, "summary", lambda: ledger)

        section = brief_results._results_section(None)

        assert section["ledger"]["positions"]["closure_evidence"] == {
            "status": "partial",
            "verified_closed": 2,
            "unverified_positions": 1,
            "unverified_closed_timestamps": 1,
            "reason": "one position lacks complete closure evidence",
        }


def test_brief_reports_unattributed_fills_per_coverage_row_not_summed(client):
    _seed_coverage(status="ok", warnings=[], manual=2, account="binance:demo:" + "b" * 64,
                   window_start="2026-11-10T00:00:00+00:00",
                   window_end="2026-11-10T01:00:00+00:00")
    _seed_coverage(status="ok", warnings=[], manual=3, account="binance:demo:" + "c" * 64,
                   window_start="2026-11-10T02:00:00+00:00",
                   window_end="2026-11-10T03:00:00+00:00")
    payload = client.get("/api/brief").json()
    rows = payload["results"]["unattributed_manual"]
    assert [row["count"] for row in rows] == [3, 2]
    # The brief never presents one summed headline over overlapping windows.
    assert "unattributed_total" not in payload["results"]
    assert "never be summed" in payload["results"]["coverage_note"]


def test_estimated_results_omits_unverified_futures_values():
    from server.brief_results import _estimated_results

    portfolio_body = _fake_portfolio()
    portfolio_body["futures"]["status"] = "unavailable"
    estimated = _estimated_results(portfolio_body)
    assert estimated["futures_unrealized"] is None
    assert estimated["futures_unrealized_currency"] is None
