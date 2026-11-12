"""Deterministic dashboard data and isolated production fixture construction."""

import json
from collections.abc import Mapping
from pathlib import Path

from tests.browser import fixture_server

FAKE_API = r"""
(() => {
  const manualOpen = !Object.prototype.hasOwnProperty.call(window, "__FIXTURE_STATE__");
  const state = window.__FIXTURE_STATE__ || {};
  if (manualOpen) {
    document.addEventListener("DOMContentLoaded", () => {
      const badge = document.createElement("div");
      badge.dataset.fixtureManual = "true";
      badge.textContent = "Offline test fixture";
      badge.style.cssText = "position:fixed;right:8px;bottom:8px;z-index:9999;background:#166534;color:white;padding:6px 10px;font:12px sans-serif";
      document.body.appendChild(badge);
    });
  }
  const ok = body => Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: {"Content-Type": "application/json"}}));
  const fail = status => Promise.resolve(new Response(
    JSON.stringify({error: {code: "http_" + status, message: "fixture failure", details: {}}}),
    {status, headers: {"Content-Type": "application/json"}}));

  const netRow = () => ({currency: "USDT", realized: "5", fees: "0.25", funding: null,
    net: "4.75", funding_known: false, fee_known: true});
  const coverage = status => [{
    account_id: "binance:demo:" + "a".repeat(8), window_start: "2026-11-10T00:00:00Z",
    window_end: "2026-11-10T01:00:00Z", fills_found: 2, status,
    warnings: status === "error" ? ["venue returned 502 while listing fills"] : [],
    realized_diff_ratio: status === "partial" ? 0.0125 : null,
    unattributed_manual_detected: 3, partial_entry_count: 0}];

  const portfolio = {
    generated_at: "2026-11-10T09:00:00Z", currency: "USDT", cash: 100, equity_est: 110,
    positions: [{ticker: "BTC-USD", quantity: 0.01, average_price: 60000,
      basis_source: "fifo", marked_price: 61000, market_value: 610, unrealized_pnl: 10}],
    positions_source: "venue",
    realized_pnl: {total: 5, today: 1, by_ticker: {"BTC-USD": 5}, closed_trades: 2,
      unknown_basis_closes: 0, consecutive_losses: 0},
    futures: {equity: 1000, wallet_balance: 900, available_balance: 800,
      unrealized_pnl: 12.5, venue_profile: state.venueProfile || "demo",
      equity_currency: "USDT", wallet_balance_currency: "USDT",
      available_balance_currency: "USDT", unrealized_pnl_currency: "USDT",
      account_scope: "derivatives",
      positions: [{symbol: "BTC/USDT:USDT", side: "long", contracts: 0.1, quantity: 0.1,
        entry_price: 60000, mark_price: 61000, liquidation_price: null,
        unrealized_pnl: 12.5, notional: 184, margin: 61, leverage: 3,
        margin_mode: "isolated", price_currency: "USDT", notional_currency: "USDT",
        margin_currency: "USDT", unrealized_pnl_currency: "USDT",
        opened_at: null, held_hours: null, funding: null, funding_currency: null,
        round_trip_fee: 0.27, round_trip_fee_currency: "USDT"}],
      positions_read: {status: "complete", skipped: 0, issues: [],
        observed_at: "2026-11-10T09:25:50Z"},
      status: "ok", error: null},
    halted_today: state.halted === true,
    snapshot: {context_id: "ctx", epoch: "e1", generation: 1, freshness: "unknown",
      dirty: false, pending: false, cache: "hit", error: null,
      request_started_at: null, response_received_at: null},
    warnings: [], stale_after: 30,
  };

  function brief() {
    if (state.brief === 500) return null;
    const hidden = state.coverageOnly === true;
    return {
      generated_at: "2026-11-10T09:25:00Z", stale_after: 30, read_only: true,
      disclaimer: "Research framework — not financial advice.",
      status: {exec_mode: "dry", auto_spot: true, auto_futures: false,
        any_auto_enabled: true, venue_profile: state.venueProfile || "demo",
        account_scope: "derivatives", halted_today: state.halted === true,
        halt_reason: state.halted ? "daily loss limit" : null,
        pending_approvals: state.approvals || 0, server_status: "ok",
        audit_log_readable: true, quote_currency: "USDT"},
      funnel: {status: "ok", buckets: {entered: 2}, total: 2},
      results: {
        ledger: hidden
          ? {status: "partial", positions: {total: 1, closed: 1}, fills: 2,
             totals: {realized_by_currency: {USDT: "5"}, fee_by_currency: {},
               funding_by_currency: {}, net_by_currency: []},
             funding_unknown_currencies: [], fee_unknown_currencies: [],
             coverage: coverage("error")}
          : {status: "partial", positions: {total: 1, closed: 1}, fills: 2,
             totals: {realized_by_currency: {USDT: "5"}, fee_by_currency: {USDT: "0.25"},
               funding_by_currency: {}, net_by_currency: [netRow()]},
             funding_unknown_currencies: ["USDT"], fee_unknown_currencies: [],
             coverage: coverage("partial")},
        estimated: {spot_audit_realized_total: 5, spot_audit_realized_today: 1,
          spot_currency: "USDT", spot_closed_trades: 2, spot_unknown_basis_closes: 0,
          futures_unrealized: 12.5, futures_unrealized_currency: "USDT"},
        unattributed_manual: [{window_start: "2026-11-10T00:00:00Z",
          window_end: "2026-11-10T01:00:00Z", count: 3, account_id: "acct"}],
        coverage_note: "Each coverage row reconciles one cycle; never summed.",
        ledger_note: "Totals are per currency; no FX conversion."},
      risk: {
        open_positions: {spot: 1, futures: 1},
        positions_read: {status: "complete", skipped: 0, issues: [],
          observed_at: "2026-11-10T09:25:50Z"},
        exposure: {value_pct: 18.4, currency: "USDT", reason: null},
        heat: {value_pct: null, reason: "risk-at-stop is not in the snapshot"},
        limits: {total_exposure_pct: 80, derivatives_exposure_soft_pct: 0,
          derivatives_exposure_hard_pct: 0, open_heat_pct: 6,
          note: "Configured limits, displayed read-only."},
        protection: {scope: "today_executed_cycles", configured: 2, missing: 1,
          unknown: 0, open_positions_verified: false,
          reason: "Venue positions carry no durable binding to their entry order."},
        futures_status: "ok", futures_error: null, account_scope: "derivatives"},
      activity: {scope: "futures", window: "today_utc", cycles_today: 4,
        analysed_today: 4, entries_today: 2, pipeline_error_cycles: 1,
        no_entry_top_reasons: [{code: "market_rejected", reason: "no_tradeable_signal",
          count: 1, sample_cycle_id: "futures:BTC/USDT:USDT:1"}],
        note: "No-signal and pipeline failure are counted separately."},
      active_forecast: activeForecast(state),
      experiments: {status: "ok", total: 1,
        latest: {run_id: "exp_1", hypothesis: "memory on/off", created_at: null},
        verdicts: {better: 4, worse: 6}, note: "Simulated, paired. Never a promotion."},
      portfolio: portfolio,
      needs_attention: state.attention === false ? [] : [
        {id: "risk_halted", kind: "risk_halted", priority: 1,
          message_code: "brief.attention.riskHalted", message_params: {},
          detail: "daily loss limit", link: "/audit"},
        {id: "protection_unconfirmed", kind: "protection_unconfirmed", priority: 1,
          message_code: "brief.attention.protectionMissing", message_params: {count: 1},
          detail: null, link: "/cycles"}],
    };
  }

  const health = () => ({generated_at: "2026-11-10T09:25:00Z",
    status: "ok", exec_mode: "dry", halted_today: state.halted === true,
    active_run_id: null, pending_approvals: state.approvals || 0});

  // The ACTIVE forecast block: the forecast the most recent recorded cycle
  // actually saw, with each state rendered as its own thing.
  function activeForecast(s) {
    const want = s.forecastState || "ok";
    const base = {cycle_id: "futures:BTC/USDT:USDT:1", symbol: "BTC/USDT:USDT",
      quote_currency: "USDT", policy: "use", defer_reason: null,
      recorded_at: "2026-11-10T09:25:00Z",
      note: "Observation labels only; nothing here is a profit claim."};
    if (want === "empty") return {state: "empty", status: "unavailable",
      reason: "no recorded cycle has a Kronos forecast yet; nothing is claimed",
      ...base, forecast: null};
    if (want === "error") return {state: "error", status: "error",
      reason: "the recorded forecasts could not be read: OperationalError",
      ...base, forecast: null};
    const forecast = {symbol: "BTC/USDT:USDT", status: "ok", reason: null,
      direction: "up", calibration_label: "sample_ratio", distribution_kind: "model_samples",
      signal_basis: "close_only",
      ohlc_quality: s.forecastQuality === "corrected"
        ? {status: "corrected", algorithm: "ohlc_wick_bounds_v1", version: 1,
           corrected_bar_count: 3, corrected_path_count: 2,
           changed_field_counts: {highs: 3, lows: 1},
           absolute_delta_by_field: {highs: {total: 12.5, max: 2.5}, lows: {total: 4, max: 1.5}},
           reason: "Model wick bounds were applied before path summary."}
        : s.forecastQuality === "unknown"
          ? {status: "unknown", algorithm: null, version: null,
             corrected_bar_count: null, corrected_path_count: null,
             changed_field_counts: null, absolute_delta_by_field: null,
             reason: "Legacy forecast did not include OHLC quality metadata."}
          : {status: "consistent", algorithm: null, version: null,
             corrected_bar_count: 0, corrected_path_count: 0,
             changed_field_counts: {highs: 0, lows: 0},
             absolute_delta_by_field: {highs: {total: 0, max: 0}, lows: {total: 0, max: 0}},
             reason: null},
      direction_counts: {up: 9, down: 5, flat: 2},
      direction_ratio: {up: 0.5625, down: 0.3125, flat: 0.125},
      band: {low: 58000, p50: 60100, high: 62500}, band_spread_ratio: 0.0765,
      reference_close: 60000, data_open_time: 1794268800000,
      as_of: "2026-11-10T08:00:00Z", horizon_hours: 24, timeframe: "4h",
      paths: 16, per_path_samples: 1, generation_kind: "forward", temperature: 0.5,
      model_revision: "kronos-fixture", artifact_hash: "abc123", latency_ms: 2512.3,
      created_at: "2026-11-10T08:05:00Z"};
    if (want === "unavailable") return {state: "unavailable", status: "unavailable",
      reason: "the isolated Kronos interpreter is absent (fixture)",
      ...base, forecast: {symbol: "BTC/USDT:USDT", status: "unavailable",
        reason: "the isolated Kronos interpreter is absent (fixture)"}};
    if (want === "stale") return {state: "stale", status: "stale", reason: null,
      ...base, forecast: {...forecast, as_of: "2026-10-01T00:00:00Z"}};
    return {state: "ok", status: "ok", reason: null, ...base, forecast};
  }

  window.__apiRequests = [];
  const original = window.fetch.bind(window);
  window.fetch = (url, init = {}) => {
    const target = typeof url === "string" ? url : url.url;
    const parsed = new URL(target, location.href);
    if (!parsed.pathname.startsWith("/api/")) return original(url, init);
    const method = (init.method || "GET").toUpperCase();
    window.__apiRequests.push({path: parsed.pathname, method});
    if (method !== "GET") return Promise.resolve(new Response("{}", {status: 404}));
    switch (parsed.pathname) {
      case "/api/brief": {
        const body = brief();
        return body === null ? fail(500) : ok(body);
      }
      case "/api/health": return ok(health());
      case "/api/portfolio": return Promise.resolve(new Response("{}", {status: 404}));
      case "/api/history": return ok({generated_at: "2026-11-10T09:25:00Z",
        items: [{date: "2026-11-01", ticker: "BTC-USD", rating: "Buy", pending: false,
          alpha: "+1.2%"}], total: 1});
      case "/api/audit": return ok({generated_at: "2026-11-10T09:25:00Z",
        items: Array.from({length: 14}, (_, i) => ({timestamp: "2026-11-10T0" + (8 - i % 8) + ":00:00Z",
          ticker: "BTC-USD", ccxt_symbol: "BTC/USDT", action: "buy", qty: 0.01,
          price_est: 60000, market: "spot", leverage: null, cost: null, mode: "dry",
          order_id: "o-" + i, phase: "execute"})),
        total_matching: 14, total_lines: 14, halted_today: false, halt_reason: null,
        skipped_lines: 0});
      case "/api/performance": return ok({generated_at: "2026-11-10T09:25:00Z",
        summary: null, open_positions: null, brackets: {placed: 1, failed: 0},
        recent_orders: [], skipped_lines: 0});
      case "/api/performance/fills": return ok({generated_at: "2026-11-10T09:25:00Z",
        status: "unavailable", source: "unavailable", venue_profile: null,
        start_at: null, end_at: null, observed_at: null, coverage: "none",
        warnings: [], fills: []});
      case "/api/autoloop": return ok({generated_at: "2026-11-10T09:25:00Z", enabled: true,
        interval_minutes: 60, running_cycle: false,
        modes: {auto_spot: true, auto_futures: false, any_auto_enabled: true},
        last_cycle: null});
      case "/api/daily": return ok({generated_at: "2026-11-10T09:25:00Z", items: [],
        active_job_id: null, total: 0});
      case "/api/settings": return ok({gates_readonly: {auto_spot: true, auto_futures: false}});
      default: return original(url, init);
    }
  };
  window.EventSource = class { addEventListener() {} removeEventListener() {} close() {} };
})();
"""


def build_dashboard_fixture(web: Path, root: Path, *, env_overrides: Mapping[str,str] | None = None) -> None:
    """Build the real dashboard using fixture-only API data and no rewrites."""
    layout = (
        'import "./globals.css";\n'
        'export default function Layout({children}: {children: React.ReactNode}) {\n'
        '  return <html lang="en"><head><script '
        'dangerouslySetInnerHTML={{__html: ' + json.dumps(FAKE_API) + '}} /></head>'
        '<body className="flex min-h-full flex-col"><main className="min-w-0 flex-1 p-4 md:p-6">'
        '{children}</main></body></html>;\n}'
    )
    page = (
        'import {PageHeader} from "@/components/page-header";\n'
        'import {OverviewDashboard} from "@/components/overview/dashboard";\n'
        'export default function Page() {return <div><PageHeader page="overview" />'
        '<OverviewDashboard/></div>}\n'
    )
    fixture_server.build_fixture_root(
        web, root, layout_source=layout, pages={"page.tsx": page}, env_overrides=env_overrides,
    )
