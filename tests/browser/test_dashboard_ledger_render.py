"""Render the actual outcome-store summary through the dashboard React component."""

import json
import subprocess
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from tests.server_api_fixtures import _no_network as _no_network  # noqa: F401
from tests.support.node_renderer import require_web_renderer
from tradingagents.outcomes.netting import REALIZED_PNL_EXCLUDES_FEE

pytestmark = pytest.mark.unit

_RENDER = r"""
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const web = process.cwd();
const ts = require("typescript");
const React = require("react");
const {renderToStaticMarkup} = require("react-dom/server");
function compile(module, filename) {
  let source = fs.readFileSync(filename, "utf8");
  if (filename.endsWith(path.join("brief", "results-section.tsx"))) {
    source = source.replace("function VerifiedBlock(", "export function VerifiedBlock(")
      .replace("function FundingObservations(", "export function FundingObservations(");
  }
  const result = ts.transpileModule(source, {fileName: filename, compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022, esModuleInterop: true,
  }});
  module._compile(result.outputText, filename);
}
Module._extensions[".ts"] = compile;
Module._extensions[".tsx"] = compile;
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, isMain, options) {
  return resolve.call(this, request.startsWith("@/") ? path.join(web, request.slice(2)) : request,
    parent, isMain, options);
};
const locale = require(path.join(web, "lib/i18n/locale.ts"));
locale.getServerLocale = () => "en";
locale.getLocale = () => "en";
const {VerifiedBlock, ClosureEvidenceBlock, FundingObservations} = require(path.join(web, "components/overview/brief/results-section.tsx"));
const ledger = JSON.parse(fs.readFileSync(0, "utf8"));
const data = {results: {ledger}};
process.stdout.write(renderToStaticMarkup(React.createElement(React.Fragment, null,
  React.createElement(VerifiedBlock, {data}), React.createElement(ClosureEvidenceBlock, {ledger}), React.createElement(FundingObservations, {data}))));
"""


class _Rows(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.rows = {}
        self.current = None
        self.cells = None
        self.cell = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and "data-net-currency" in attrs:
            self.current = attrs["data-net-currency"]
            self.cells = []
        elif tag == "td" and self.cells is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.cells.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            self.rows[self.current] = self.cells
            self.current = self.cells = None



@pytest.mark.parametrize(
    ("status", "recorded_closed", "verified_closed", "expected_status", "verified_timestamp"),
    [
        pytest.param("complete", 1, 1, "complete", True, id="complete-status"),
        pytest.param("partial", 0, 0, "partial", False, id="partial-zero"),
        pytest.param(None, 1, None, "unavailable", False, id="unavailable-proof"),
        pytest.param("partial", 1, 1, "partial", True, id="coherent-verified-count"),
        pytest.param("partial", 1, 2, "partial", False, id="incoherent-verified-count"),
    ],
)
def test_closure_evidence_renders_recorded_and_proven_counts_without_postgres(
    status, recorded_closed, verified_closed, expected_status, verified_timestamp,
):
    ledger = {
        "status": "partial",
        "positions": {
            "total": 2,
            "closed": recorded_closed,
            "recorded_closed": recorded_closed,
            "first_opened": "2026-11-09T23:55:00+00:00",
            "last_closed": "2026-11-10T01:00:00+00:00" if recorded_closed else None,
            "last_recorded_closed": "2026-11-10T01:00:00+00:00" if recorded_closed else None,
            "closure_evidence": None if status is None else {
                "status": status,
                "verified_closed": verified_closed,
                "unverified_positions": 1 if status != "complete" else 0,
                "unverified_closed_timestamps": max(
                    0, recorded_closed - (verified_closed or 0)
                ) if status != "complete" else 0,
                "reason": None if status == "complete" else "Partial stored-fill proof.",
            },
        },
        "fills": 2,
        "totals": {
            "realized_by_currency": {"USDT": "12345678901234567890.123456789"},
            "fee_by_currency": {}, "funding_by_currency": {},
            "net_by_currency": [{"currency": "USDT", "amount": None,
                                  "funding_known": False, "fee_known": False}],
        },
        "funding_unknown_currencies": ["USDT"],
        "fee_unknown_currencies": [],
        "funding_observations": {"status": "partial", "events": [], "truncated": False},
        "coverage": [],
    }
    web = require_web_renderer()
    result = subprocess.run(
        ["node", "-e", _RENDER], cwd=web, input=json.dumps(ledger),
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "12345678901234567890.123456789" in result.stdout
    assert f'data-closure-evidence-status="{expected_status}"' in result.stdout
    assert "Recorded positions" in result.stdout
    if status == "complete":
        assert "Closures verified against stored fills" in result.stdout
        assert "Recorded positions with closure timestamps" in result.stdout
    elif status == "partial":
        assert "Recorded positions with closure timestamps (unverified)" in result.stdout
        assert "does not prove that no closures occurred" in result.stdout
        assert "Closures verified against stored fills" in result.stdout
    else:
        assert "Recorded positions with closure timestamps (unverified)" in result.stdout
        assert "does not prove that no closures occurred" in result.stdout
        assert "Closures verified against stored fills" not in result.stdout

    if verified_timestamp:
        assert "Last closure timestamp verified against stored fills" in result.stdout
        assert "Latest recorded closure timestamp (may be unverified)" not in result.stdout
    elif recorded_closed:
        assert "Last closure timestamp verified against stored fills" not in result.stdout
        assert "Latest recorded closure timestamp (may be unverified)" in result.stdout
    else:
        assert "Last closure timestamp verified against stored fills" not in result.stdout
        assert "Latest recorded closure timestamp (may be unverified)" not in result.stdout


@pytest.mark.parametrize("route", ["/api/outcomes", "/api/brief"])
def test_actual_summary_renders_exact_components_without_claiming_unknown_net(
    postgres_schema, monkeypatch, route,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import operating_brief, outcome_store, outcomes_api

    outcome_store.record_outcome(
        account_id="binance:demo:renderer",
        position={
            "cycle_id": "renderer-cycle", "client_order_id": "renderer-order",
            "symbol": "BTC/USDT:USDT", "market_type": "perpetual", "side": "long",
            "filled_quantity": Decimal("1"), "attribution": "managed",
        },
        fills=[{
            "venue_trade_id": "renderer-fill", "symbol": "BTC/USDT:USDT", "side": "sell",
            "role": "exit", "quantity": Decimal("1"), "price": Decimal("100"),
            "occurred_at": "2026-11-10T01:00:00+00:00",
            "realized_pnl": Decimal("12345678901234567890.123456789"),
            "realized_pnl_currency": "USDT", "realized_pnl_semantics": REALIZED_PNL_EXCLUDES_FEE,
            "fee": Decimal("0.000000019"), "fee_currency": "BNB",
        }, {
            "venue_trade_id": "renderer-fill-unknown-fee", "symbol": "BTC/USDT:USDT",
            "side": "buy", "role": "entry", "quantity": Decimal("1"),
            "price": Decimal("100"), "occurred_at": "2026-11-10T00:00:00+00:00",
            "fee": Decimal("0.123456789"), "fee_currency": None,
        }],
        exits=[],
        funding=[{
            "funding_at": "2026-11-10T02:00:00+00:00", "amount": Decimal("0.000000003"),
            "currency": "USDT", "venue_semantics": "binance: income positive = received",
        }],
    )
    outcome_store.record_funding_observations(
        "binance:demo:renderer", "binance:demo", "futures", "BTC/USDT:USDT", [{
            "id": "observed-funding", "funding_at": "2026-11-10T03:00:00+00:00",
            "amount": Decimal("0.000000000123456789"), "currency": "BNB",
            "venue_semantics": "binance: income positive = received",
        }],
    )
    app = FastAPI()
    app.include_router(outcomes_api.router)
    monkeypatch.setattr(operating_brief, "_read_portfolio", lambda: None)
    app.include_router(operating_brief.router)

    with TestClient(app) as client:
        response = client.get(route)
    assert response.status_code == 200
    payload = response.json()
    ledger = payload if route == "/api/outcomes" else payload["results"]["ledger"]
    web = require_web_renderer()
    result = subprocess.run(
        ["node", "-e", _RENDER], cwd=web, input=json.dumps(ledger),
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    rows = _Rows(result.stdout).rows
    assert ledger["totals"]["realized_by_currency"]["USDT"] in rows["USDT"][1]
    assert ledger["totals"]["funding_by_currency"]["USDT"]["received"] in rows["USDT"][3]
    assert "Not enough data" in rows["USDT"][4]
    assert ledger["totals"]["fee_by_currency"]["BNB"] in rows["BNB"][2]
    assert "Not enough data" in rows["BNB"][4]
    assert ledger["totals"]["fee_by_currency"]["null"] in rows["null"][2]
    assert "Unknown currency" in result.stdout
    assert ledger["funding_observations"]["events"][0]["amount"] in result.stdout
    assert "not attributed to positions" in result.stdout
    assert "not included in net" in result.stdout
    assert "binance:demo:renderer" in result.stdout
    assert "BTC/USDT:USDT" in result.stdout
    assert "Research framework" in payload["disclaimer"]
