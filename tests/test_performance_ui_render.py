"""Server-render the real performance UI without a browser or live APIs."""

import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.support.node_renderer import require_web_renderer

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"

NODE_RENDERER = r"""
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const webRoot = process.cwd();
const ts = require(require.resolve("typescript", { paths: [webRoot] }));
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

function compile(module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const result = ts.transpileModule(source, {
    fileName: filename,
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  });
  module._compile(result.outputText, filename);
}

Module._extensions[".ts"] = compile;
Module._extensions[".tsx"] = compile;
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
  const resolvedRequest = request.startsWith("@/")
    ? path.join(webRoot, request.slice(2))
    : request;
  return resolveFilename.call(this, resolvedRequest, parent, isMain, options);
};

// The control panel is locale-aware (#15): components read their copy through
// useT() (server snapshot = getServerLocale()) and their numbers/dates through
// getLocale(). The assertions below check the Vietnamese dictionary, which is
// the source shape every other locale is typed against, so pin both snapshots
// before rendering.
const requestedLocale = process.env.RENDER_LOCALE;
if (requestedLocale) {
  const i18nLocale = require(path.join(webRoot, "lib/i18n/locale.ts"));
  i18nLocale.getServerLocale = () => requestedLocale;
  i18nLocale.getLocale = () => requestedLocale;
}

const { PerformancePositions } = require(
  path.join(webRoot, "components/overview/performance-positions.tsx")
);
const { PerformanceHistory } = require(
  path.join(webRoot, "components/overview/performance-history.tsx")
);
const { PortfolioCard } = require(
  path.join(webRoot, "components/overview/portfolio-card.tsx")
);
const { PerformanceCard } = require(
  path.join(webRoot, "components/overview/performance-card.tsx")
);
const { ExecStatusChipView } = require(
  path.join(webRoot, "components/exec-status-chip.tsx")
);
const { AuditLogTable } = require(
  path.join(webRoot, "components/audit/audit-log.tsx")
);
const { FuturesCycleFunnel } = require(
  path.join(webRoot, "components/scanner/futures-cycle-funnel.tsx")
);
const { ResolvedList } = require(
  path.join(webRoot, "components/approvals/resolved-list.tsx")
);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const rendered = Object.fromEntries(Object.entries(cases).map(([name, item]) => {
  const component = item.component === "history" ? PerformanceHistory
    : item.component === "portfolio" ? PortfolioCard
    : item.component === "performance" ? PerformanceCard
    : item.component === "chip" ? ExecStatusChipView
    : item.component === "audit_table" ? AuditLogTable
    : item.component === "funnel" ? FuturesCycleFunnel
    : item.component === "resolved" ? ResolvedList
    : PerformancePositions;
  return [name, renderToStaticMarkup(React.createElement(component, item.props))];
}));
process.stdout.write(JSON.stringify(rendered));
"""


class RenderedHtml(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.regions = set()
        self.rows = {}
        self._elements = []
        self._row = None
        self.feed(markup)

    @property
    def text(self):
        return " ".join("".join(self.text_parts).split())

    def _active_region(self):
        return next((label for _, label in reversed(self._elements) if label), None)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        label = attributes.get("aria-label") if attributes.get("role") == "region" else None
        self._elements.append((tag, label))
        if label:
            self.regions.add(label)
        if tag == "tr":
            self._row = {"region": self._active_region(), "text": [], "cells": [], "cell": None}
        elif tag in {"td", "th"} and self._row is not None:
            self._row["cell"] = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        self.text_parts.append(data)
        if self._row is not None:
            self._row["text"].append(data)
            if self._row["cell"] is not None:
                self._row["cell"].append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._row is not None and self._row["cell"] is not None:
            self._row["cells"].append(" ".join("".join(self._row["cell"]).split()))
            self._row["cell"] = None
        if tag == "tr" and self._row is not None:
            region = self._row["region"]
            if region:
                text = " ".join("".join(self._row["text"]).split())
                self.rows.setdefault(region, []).append({"text": text, "cells": self._row["cells"]})
            self._row = None
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break


def _read_status(status="complete", *, skipped=0, observed_at="2026-11-02T12:00:00Z"):
    return {
        "status": status,
        "skipped": skipped,
        "issues": [],
        "observed_at": observed_at,
    }


def _futures(positions, *, read=None):
    return {
        "venue_profile": "demo",
        "status": "ok",
        "error": None,
        "positions": positions,
        "positions_read": read if read is not None else _read_status(),
    }


def _portfolio_props(
    *, source="venue", spot_positions=None, futures=None, stale=False, futures_stale=False,
):
    data = {
        "generated_at": "2026-11-02T12:00:00Z",
        "positions_source": source,
        "positions": spot_positions if spot_positions is not None else [],
        "warnings": [],
        "futures": futures,
    }
    return {
        "portfolioState": {"status": "ready", "data": data, "fetchedAt": 0},
        "stale": stale,
        "futuresStale": futures_stale,
        "onRetry": None,
    }


def _position(symbol, *, quantity=1.0):
    return {
        "symbol": symbol,
        "side": "long",
        "contracts": quantity,
        "quantity": quantity,
        "entry_price": 10.0,
        "mark_price": 11.0,
        "price_currency": "USDT",
        "unrealized_pnl": 1.0,
        "unrealized_pnl_currency": "USDT",
    }


def _card_data(futures):
    return {
        "generated_at": "2026-11-02T12:00:00Z",
        "currency": "USDT",
        "cash": 125.0,
        "equity_est": 200.0,
        "positions": [{"ticker": "BTC", "quantity": 0.25, "marked_price": 300}],
        "positions_source": "venue",
        "realized_pnl": {
            "total": 0, "today": 0, "closed_trades": 0,
            "unknown_basis_closes": 0, "consecutive_losses": 0,
        },
        "halted_today": False,
        "warnings": [],
        "futures": futures,
    }


def _card_case(
    component, futures, *, stale=False, futures_stale=False, poll_error=False, empty_spot=False,
):
    data = _card_data(futures)
    if empty_spot:
        data.update(cash=None, equity_est=None, positions=[])
    state = {"status": "ready", "data": data, "fetchedAt": 0}
    if poll_error:
        state.update(status="stale", code="network_error", message="offline")
    if component == "portfolio":
        props = {"state": state, "stale": stale, "futuresStale": futures_stale, "onRetry": None}
    else:
        props = {
            "state": {"status": "loading"}, "stale": False, "onRetry": None,
            "portfolioState": state, "portfolioStale": stale,
            "futuresStale": futures_stale, "onPortfolioRetry": None,
            "historyState": {"status": "loading"}, "historyStale": False, "onHistoryRetry": None,
        }
    return {"component": component, "props": props}


def _rollup(**overrides):
    """Rollup theo mã từ GET /api/autoloop/cycles (scanner-types.ts:234-263)."""
    base = {
        "symbol": "UNI/USDT:USDT",
        "total": 2, "entered": 0, "awaiting_approval": 0, "risk_rejected": 0,
        "not_executed": 0, "execution_failed": 0, "proposal_rejected": 1,
        "confirmation_rejected": 0, "market_rejected": 0, "in_flight": 1,
        "interrupted": 0, "other": 0,
        "last_updated": "2026-11-07T10:00:00Z",
        "last_status": "running", "last_phase": None, "last_execution_status": None,
        "last_executed": "", "last_execution_reason": None, "last_signal": None,
        "last_signal_reason": None, "last_market_rejection_reason": None,
        "last_confirmation_reason": None,
    }
    base.update(overrides)
    return base


def _funnel_summary(**overrides):
    base = {
        "market_rejected": 0, "confirmation_rejected": 0, "proposal_rejected": 1,
        "entered": 0, "awaiting_approval": 0, "risk_rejected": 0, "not_executed": 0,
        "execution_failed": 0, "in_flight": 1, "interrupted": 0, "other": 0, "total": 2,
    }
    base.update(overrides)
    return base


def _audit_event(**overrides):
    """Một dòng audit JSONL (audit/types.ts AuditEvent)."""
    base = {
        "timestamp": "2026-11-07T10:00:00Z", "ticker": "UNI/USDT:USDT",
        "action": "buy", "phase": "plan", "mode": "live",
        "qty": 1.5, "price_est": 8.5, "order_id": None, "reason": None,
        "_line_no": 1,
    }
    base.update(overrides)
    return base


def _resolved_item(**overrides):
    base = {
        "id": "ap-1", "created_at": "2026-11-07T09:00:00Z", "run_id": None,
        "ticker": "UNI/USDT:USDT", "ccxt_symbol": "UNI/USDT:USDT", "signal": "Buy",
        "side": "buy", "quantity": 1.5, "price_est": 8.5, "cost": 12.75,
        "market": "derivatives", "leverage": 2, "mode_at_plan": "live",
        "plan_reason": None, "status": "execute_failed",
        "resolved_at": "2026-11-07T09:05:00Z", "resolution": None,
        "mode": "live", "order_id": None,
    }
    base.update(overrides)
    return base


def _cases():
    venue_futures = _futures(
        [
            _position("HYPE/USDT:USDT", quantity=2.0),
            _position("SOL/USDT:USDT", quantity=-0.123456789123),
            _position("XPL/USDT:USDT", quantity=3.0),
        ]
    )
    history = {
        "status": "partial",
        "source": "venue_fills",
        "venue_profile": "demo",
        "start_at": "2026-10-30T00:00:00Z",
        "end_at": "2026-11-02T00:00:00Z",
        "observed_at": "2026-11-02T12:00:00Z",
        "coverage": "selected venue history",
        "warnings": [],
        "fills": [
            {
                "trade_id": "historic-uni-older",
                "order_id": "order-older",
                "symbol": "UNI/USDC:USDC",
                "side": "sell",
                "timestamp": "2026-10-31T10:00:00Z",
                "contracts": "1.000000000000000001",
                "price": "8.123456789012345678",
                "fee": None,
                "fee_currency": None,
                "realized_pnl": None,
                "realized_pnl_currency": None,
            },
            {
                "trade_id": "historic-uni-newer",
                "order_id": "order-newer",
                "symbol": "UNI/USDC:USDC",
                "side": "buy",
                "timestamp": "2026-11-01T10:00:00Z",
                "contracts": "2",
                "price": "8.50",
                "fee": None,
                "fee_currency": None,
                "realized_pnl": None,
                "realized_pnl_currency": None,
            },
        ],
    }
    cases = {
        "venue_snapshot": {
            "component": "positions",
            "props": _portfolio_props(
                spot_positions=[{"ticker": "BTC", "quantity": 0.25, "marked_price": 67000}],
                futures=venue_futures,
            ),
        },
        "audit_spot": {
            "component": "positions",
            "props": _portfolio_props(
                source="audit",
                spot_positions=[{"ticker": "UNI", "quantity": 10, "marked_price": 8.5}],
                futures=None,
            ),
        },
        "missing_futures_positions": {
            "component": "positions",
            "props": _portfolio_props(futures=_futures(None)),
        },
        "partial_futures_positions": {
            "component": "positions",
            "props": _portfolio_props(
                futures=_futures([_position("HYPE/USDT:USDT")], read=_read_status("partial")),
            ),
        },
        "empty_partial_futures": {
            "component": "positions",
            "props": _portfolio_props(
                futures=_futures([], read=_read_status("partial")),
            ),
        },
        "stale_empty_futures": {
            "component": "positions",
            "props": _portfolio_props(futures=_futures([]), stale=True),
        },
        "missing_observed_at": {
            "component": "positions",
            "props": _portfolio_props(
                futures=_futures([], read=_read_status(observed_at=None)),
            ),
        },
        "unknown_spot_audit": {
            "component": "portfolio",
            "props": {
                "state": {"status": "ready", "fetchedAt": 0, "data": {
                    "generated_at": "2026-11-02T12:00:00Z", "currency": "USDT",
                    "cash": None, "equity_est": None, "positions": [],
                    "positions_source": "unavailable", "realized_pnl": None,
                    "halted_today": None, "warnings": ["audit data unavailable"],
                    "futures": _futures([_position("HYPE/USDT:USDT")]),
                }},
                "stale": False, "futuresStale": False, "onRetry": None,
            },
        },
        "scope_only": {
            "component": "positions",
            "props": _portfolio_props(futures=_futures([], read={
                **_read_status("partial"),
                "issues": [{"symbol": None, "reason": "scope_unverified"}],
            })),
        },
        "monitor_position_missing": {
            "component": "positions",
            "props": _portfolio_props(futures=_futures(
                [_position("ZEC/USDT:USDT")], read={
                    **_read_status("partial"),
                    "issues": [
                        {"symbol": "ZEC/USDT:USDT", "reason": "monitor_position_missing"},
                    ],
                },
            )),
        },
        "history_fills": {"component": "history", "props": {"history": history}},
        "chip_live": {"component": "chip", "props": {"state": {"status": "ready", "live": True}}},
        "chip_dry": {"component": "chip", "props": {"state": {"status": "ready", "live": False}}},
        "chip_loading": {"component": "chip", "props": {"state": {"status": "loading"}}},
        "chip_error": {"component": "chip", "props": {"state": {"status": "error"}}},
        "funnel_running": {
            "component": "funnel",
            "props": {"summary": _funnel_summary(), "symbols": [_rollup()]},
        },
        "funnel_idle": {
            "component": "funnel",
            "props": {
                "summary": _funnel_summary(
                    in_flight=0, execution_failed=1, proposal_rejected=0, total=2,
                ),
                "symbols": [_rollup(
                    in_flight=0, execution_failed=1, proposal_rejected=0,
                    last_status="failed", last_phase="analysis",
                    last_execution_reason="llm relay timeout",
                )],
            },
        },
        "audit_grouped": {
            "component": "audit_table",
            "props": {"items": [
                # Dry plan không order_id — hàng đơn.
                _audit_event(
                    _line_no=1, action="no_order", phase="plan", mode="dry",
                    qty=None, price_est=None,
                ),
                # Live cluster điển hình: plan → execute → bracket (collapsed).
                _audit_event(_line_no=2, order_id="ord-1"),
                _audit_event(_line_no=3, order_id="ord-1", phase="execute", confirmed=True),
                _audit_event(_line_no=4, order_id="ord-1", action="bracket_placed",
                             phase="protection"),
                # Cluster bất thường: bracket_failed với reason chứa HTML của sàn
                # — badge "Cần kiểm tra" + steps mở sẵn.
                _audit_event(_line_no=5, ticker="INJ/USDT:USDT", order_id="ord-2"),
                _audit_event(
                    _line_no=6, ticker="INJ/USDT:USDT", order_id="ord-2",
                    action="bracket_failed", phase="protection",
                    reason=(
                        "live order on INJ/USDT:USDT failed: binance "
                        "502 Bad Gateway <html><body>gateway-error-internal</body></html>"
                    ),
                ),
                # Reason chỉ toàn thẻ → "không đọc được"; reason dài → cắt + "Xem đầy đủ".
                _audit_event(
                    _line_no=7, order_id=None, action="no_order", phase="plan",
                    mode="dry", qty=None, price_est=None, reason="<html></html>",
                ),
                _audit_event(
                    _line_no=8, order_id=None, action="no_order", phase="plan",
                    mode="dry", qty=None, price_est=None, reason="x" * 200,
                ),
                # Dòng điều khiển halt — không có action.
                _audit_event(
                    _line_no=9, event="halt", action=None, ticker=None, qty=None,
                    price_est=None, order_id=None, reason="daily loss cap",
                    timestamp="2026-11-07T11:00:00Z",
                ),
            ]},
        },
        "resolved_html_reason": {
            "component": "resolved",
            "props": {"items": [_resolved_item(
                resolution=(
                    "live order on UNI/USDT:USDT failed: binance "
                    "502 Bad Gateway <html><body>502-page</body></html>"
                ),
            )]},
        },
        "resolved_long_reason": {
            "component": "resolved",
            "props": {"items": [_resolved_item(resolution="y" * 200)]},
        },
        "resolved_unreadable_reason": {
            "component": "resolved",
            "props": {"items": [_resolved_item(resolution="<html><body></body></html>")]},
        },
    }
    for component in ("performance", "portfolio"):
        for name, futures, futures_stale in (
            ("unavailable", {"status": "unavailable"}, False),
            ("error", {"status": "error", "error": "offline"}, False),
            ("old", _futures([], read=_read_status(observed_at="2026-11-02T11:59:00Z")), True),
            ("missing_time", _futures([], read=_read_status(observed_at=None)), True),
            ("invalid_time", _futures([], read=_read_status(observed_at="invalid")), True),
        ):
            cases[f"{component}_{name}"] = _card_case(
                component, futures, futures_stale=futures_stale,
            )
        cases[f"{component}_missing_time_unflagged"] = _card_case(
            component, _futures([], read=_read_status(observed_at=None)),
        )
        cases[f"{component}_fresh"] = _card_case(component, _futures([]))
        cases[f"{component}_poll_error"] = _card_case(component, _futures([]), poll_error=True)
        cases[f"{component}_stale_snapshot"] = _card_case(component, _futures([]), stale=True)
        cases[f"{component}_empty_spot_old_futures"] = _card_case(
            component, _futures([]), futures_stale=True, empty_spot=True,
        )
        cases[f"{component}_old_positions"] = _card_case(
            component, _futures([_position("HYPE/USDT:USDT")]), futures_stale=True,
        )
    return cases


def _render(locale: str | None) -> dict[str, RenderedHtml]:
    """Render every case through node; ``locale`` overrides the server locale."""
    require_web_renderer()
    env = os.environ.copy()
    env.update({"TZ": "UTC", "NODE_ENV": "production"})
    if locale is not None:
        env["RENDER_LOCALE"] = locale
    result = subprocess.run(
        ["node", "-e", NODE_RENDERER],
        cwd=WEB_ROOT,
        input=json.dumps(_cases()),
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return {name: RenderedHtml(markup) for name, markup in json.loads(result.stdout).items()}


@pytest.fixture(scope="module")
def rendered():
    """The Vietnamese dictionary: the source shape every locale is typed against."""
    return _render("vi")


@pytest.fixture(scope="module")
def rendered_default_locale():
    """No override: the browser's first paint, which must be the default locale."""
    return _render(None)


def test_default_locale_renders_english_copy(rendered_default_locale):
    """The default locale must not leak the Vietnamese source strings."""
    page = rendered_default_locale["missing_futures_positions"]

    assert (
        "The Futures position list is unavailable; open positions cannot be confirmed."
        in page.text
    )
    assert "Danh sách vị thế Futures không khả dụng" not in page.text


def test_venue_snapshot_renders_reported_spot_and_futures_with_full_precision(rendered):
    page = rendered["venue_snapshot"]

    assert "Bảng vị thế Spot, có thể cuộn ngang" in page.regions
    assert "BTC" in page.rows["Bảng vị thế Spot, có thể cuộn ngang"][1]["text"]
    futures_rows = page.rows["Bảng vị thế Futures, có thể cuộn ngang"]
    assert all(
        symbol in " ".join(row["text"] for row in futures_rows)
        for symbol in ("HYPE/USDT:USDT", "SOL/USDT:USDT", "XPL/USDT:USDT")
    )
    assert "0,123456789123" in " ".join(row["text"] for row in futures_rows)


def test_audit_derived_spot_rows_are_not_rendered_as_current_positions(rendered):
    page = rendered["audit_spot"]

    assert "Vị thế Spot hiện tại không khả dụng vì không có snapshot được xác minh từ sàn." in page.text
    assert not any("UNI" in row for rows in page.rows.values() for row in rows)
    assert "Bảng vị thế Spot, có thể cuộn ngang" not in page.regions


def test_missing_futures_positions_are_reported_as_unavailable_not_empty(rendered):
    page = rendered["missing_futures_positions"]

    assert "Danh sách vị thế Futures không khả dụng; không thể xác nhận vị thế đang mở." in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text


def test_partial_futures_snapshot_warns_that_positions_may_be_missing(rendered):
    page = rendered["partial_futures_positions"]

    assert "Chưa xác minh được danh sách Futures đầy đủ; có thể còn vị thế chưa hiển thị." in page.text


def test_missing_monitored_position_names_the_symbol_and_reason(rendered):
    page = rendered["monitor_position_missing"]

    assert (
        "ZEC/USDT:USDT: Vị thế đang được hệ thống quản lý nhưng không có trong danh sách sàn"
        in page.text
    )
    assert "Chưa xác minh được danh sách Futures đầy đủ" in page.text


def test_empty_partial_futures_snapshot_is_not_claimed_to_be_flat(rendered):
    page = rendered["empty_partial_futures"]

    assert "Chưa thể xác nhận có vị thế Futures đang mở hay không." in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text


def test_stale_empty_futures_snapshot_is_not_claimed_to_be_flat(rendered):
    page = rendered["stale_empty_futures"]

    assert "Dữ liệu vị thế đã cũ, không phản ánh trạng thái hiện tại." in page.text
    assert "Chưa thể xác nhận có vị thế Futures đang mở hay không." in page.text


def test_missing_futures_observation_time_is_explicitly_unverified(rendered):
    page = rendered["missing_observed_at"]

    assert "Thời điểm đọc vị thế: không xác định." in page.text
    assert "Không xác định được thời điểm đọc vị thế Futures; không thể xác nhận dữ liệu hiện tại." in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
@pytest.mark.parametrize("condition", ["unavailable", "error"])
def test_futures_unavailable_does_not_mark_fresh_spot_stale(rendered, component, condition):
    page = rendered[f"{component}_{condition}"]

    assert "BTC" in page.text
    assert "Dữ liệu Spot đã cũ" not in page.text
    assert "trạng thái Spot và Futures hiện tại chưa được xác nhận" not in page.text
    if condition == "error":
        assert "Không đọc được" in page.text
        assert "offline" in page.text
    else:
        assert ("Snapshot Futures không khả dụng" in page.text
                or "Chưa đọc snapshot Futures" in page.text)
    assert "Sàn không báo vị thế Futures đang mở" not in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
@pytest.mark.parametrize("condition", ["old", "missing_time", "invalid_time"])
def test_unverified_futures_do_not_hide_fresh_spot(rendered, component, condition):
    page = rendered[f"{component}_{condition}"]

    assert "BTC" in page.text
    assert "Dữ liệu Spot đã cũ" not in page.text
    assert "trạng thái Spot và Futures hiện tại chưa được xác nhận" not in page.text
    assert "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc" in page.text
    assert "Chưa thể xác nhận có vị thế Futures đang mở hay không." in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text
    if condition != "old":
        assert "Không xác định được thời điểm đọc vị thế Futures" in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_missing_futures_timestamp_never_claims_empty_even_without_stale_prop(rendered, component):
    page = rendered[f"{component}_missing_time_unflagged"]

    assert "Không xác định được thời điểm đọc vị thế Futures" in page.text
    assert "Chưa thể xác nhận có vị thế Futures đang mở hay không." in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_fresh_futures_empty_snapshot_still_reports_no_open_positions(rendered, component):
    page = rendered[f"{component}_fresh"]

    assert "BTC" in page.text
    assert "Sàn không báo vị thế Futures đang mở" in page.text
    assert "Dữ liệu Futures đã cũ" not in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_last_poll_error_marks_spot_and_futures_stale(rendered, component):
    page = rendered[f"{component}_poll_error"]

    assert "offline" in page.text
    assert "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc" in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text
    if component == "performance":
        assert "Dữ liệu Spot đã cũ" in page.text
        assert "Bảng vị thế Spot, có thể cuộn ngang" not in page.regions
    else:
        assert "đang hiển thị dữ liệu cũ" in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_fresh_empty_spot_is_not_suppressed_by_stale_futures(rendered, component):
    page = rendered[f"{component}_empty_spot_old_futures"]

    if component == "performance":
        assert "Không có tài sản Spot được hệ thống nhận diện trong snapshot này" in page.text
    else:
        assert "Chưa có dữ liệu Spot" in page.text
    assert "Snapshot Spot đã cũ" not in page.text
    assert "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc" in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_stale_portfolio_marks_spot_and_futures_stale(rendered, component):
    page = rendered[f"{component}_stale_snapshot"]

    assert "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc" in page.text
    assert "Sàn không báo vị thế Futures đang mở" not in page.text
    if component == "performance":
        assert "Dữ liệu Spot đã cũ" in page.text
    else:
        assert "Snapshot đã cũ; trạng thái Spot và Futures hiện tại chưa được xác nhận." in page.text


@pytest.mark.parametrize("component", ["performance", "portfolio"])
def test_stale_nonempty_futures_are_explicitly_labelled(rendered, component):
    page = rendered[f"{component}_old_positions"]

    assert "BTC" in page.text
    assert "HYPE/USDT:USDT" in page.text
    assert "Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc" in page.text


def test_scope_only_warning_does_not_claim_partial_read(rendered):
    page = rendered["scope_only"]
    assert "Chưa xác minh đầy đủ phạm vi hoặc phân trang của sàn" in page.text
    assert "chỉ được đọc một phần" not in page.text


def test_unknown_spot_audit_does_not_hide_futures_or_render_zero(rendered):
    page = rendered["unknown_spot_audit"]
    assert "Không xác định được trạng thái dừng giao dịch của bot." in page.text
    assert "Không xác định được số dư và vị thế Spot." in page.text
    assert "HYPE/USDT:USDT" in page.text
    assert "—" in page.text


def test_venue_uni_fills_are_historical_newest_first_and_preserve_unknown_values(rendered):
    page = rendered["history_fills"]
    rows = page.rows["Bảng lịch sử khớp lệnh Futures, có thể cuộn ngang"]
    text = " ".join(row["text"] for row in rows)

    assert "UNI/USDC:USDC" in text
    assert text.index("historic-uni-newer") < text.index("historic-uni-older")
    assert "8,123456789012345678 USDC" in text
    assert "1,000000000000000001" in text
    assert all(row["cells"][5:7] == ["—", "—"] for row in rows[1:])
    assert "0,00 USDC" not in text
    assert "Lịch sử chỉ đầy đủ một phần; có thể còn khớp lệnh chưa được ghi nhận." in page.text


def test_exec_status_chip_renders_all_four_gate_states(rendered):
    # Chấm trạng thái là span aria-hidden (trang trí) — text không còn "●".
    assert "LIVE — lệnh thật" in rendered["chip_live"].text
    assert "An toàn — lệnh thử" in rendered["chip_dry"].text
    assert "Đang kiểm tra…" in rendered["chip_loading"].text
    assert "Không kiểm tra được" in rendered["chip_error"].text
    assert "Thử lại" in rendered["chip_error"].text


def test_running_symbol_cycle_shows_analyzing_and_dashed_symbol(rendered):
    page = rendered["funnel_running"]
    assert "Đang phân tích…" in page.text
    assert "UNI-USDT" in page.text

    # Không có chu kỳ claimed/running → không dán nhãn đang chạy vĩnh viễn.
    idle = rendered["funnel_idle"]
    assert "Đang phân tích…" not in idle.text
    assert "Phân tích lỗi — chưa vào lệnh (llm relay timeout)" in idle.text


def test_audit_table_clusters_same_order_id_and_flags_anomalies(rendered):
    page = rendered["audit_grouped"]
    text = page.text

    # Ký hiệu hiển thị một dạng gạch; nguyên văn chỉ còn trong title.
    assert "UNI-USDT" in text
    assert "INJ-USDT" in text
    assert "gộp chung một hàng" in text

    # Cluster bất thường: badge đỏ + steps mở sẵn với lý do đã strip HTML.
    assert "Cần kiểm tra" in text
    assert "Đặt cắt lỗ/chốt lời LỖI" in text
    assert "502 Bad Gateway" in text
    assert "gateway-error-internal" in text
    assert "<html>" not in text

    # Cluster bình thường thu gọn — bước con không render tĩnh.
    assert "Đã gửi lệnh" not in text
    assert "Đã đặt cắt lỗ + chốt lời" not in text

    # Dòng không order_id giữ hàng đơn; halt là dòng điều khiển riêng.
    assert "Dừng giao dịch trong ngày" in text

    # Reason dài cắt ~140 ký tự + nút mở rộng; reason chỉ toàn thẻ → không đọc được.
    assert "x" * 140 in text
    assert "x" * 200 not in text
    assert "Xem đầy đủ" in text
    assert "Lỗi thực thi — chi tiết kỹ thuật không đọc được" in text


def test_resolved_list_strips_html_from_resolution_and_offers_expansion(rendered):
    page = rendered["resolved_html_reason"]
    assert "Lý do:" in page.text
    assert "502 Bad Gateway" in page.text
    assert "<html>" not in page.text
    assert "UNI-USDT" in page.text

    long_page = rendered["resolved_long_reason"]
    assert "y" * 140 in long_page.text
    assert "y" * 200 not in long_page.text
    assert "Xem đầy đủ" in long_page.text

    unreadable = rendered["resolved_unreadable_reason"]
    assert "Lỗi thực thi — chi tiết kỹ thuật không đọc được" in unreadable.text
