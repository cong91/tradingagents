// Brief data layer: the wire shape of GET /api/brief (docs/ui-api-contract.md §16,
// plan §5.1). Types only — rendering lives in the components beside this file.
//
// Two rules from the contract show up directly in these types:
//   * a missing value is `null`, never `0` — the renderers must say "not enough
//     data" rather than print a zero the server never claimed;
//   * ledger decimals remain strings all the way to the renderer; converting
//     them to JavaScript numbers would round some stored monetary amounts.

import { fetchJson } from "@/components/overview/contract";
import type { PortfolioPayload } from "@/components/overview/contract";

export type CoverageStatus = "ok" | "partial" | "error" | "unsupported" | "unavailable";

export type NetRow = {
  currency: string | null;
  /** Decimal totals are exact strings of the stored numeric columns. */
  amount: string | null;
  funding_known: boolean;
  fee_known: boolean;
};

export type CoverageRow = {
  account_id: string | null;
  window_start: string | null;
  window_end: string | null;
  fills_found: number | string | null;
  status: CoverageStatus;
  warnings: string[] | null;
  realized_diff_ratio: number | string | null;
  unattributed_manual_detected: number | string | null;
  partial_entry_count: number | string | null;
};

export type LedgerTotals = {
  realized_by_currency?: Record<string, string | null>;
  fee_by_currency?: Record<string, string | null>;
  funding_by_currency?: Record<string, {
    received: string; paid: string; events: number; venue_semantics: string | null;
  }>;
  net_by_currency?: NetRow[];
} | null;

export type ClosureEvidence = {
  status: "complete" | "partial" | "unavailable";
  verified_closed: number | null;
  unverified_positions: number | null;
  unverified_closed_timestamps: number | null;
  reason: string | null;
};

export type BriefLedger = {
  status: "partial" | "unavailable" | "error";
  reason?: string;
  positions?: {
    total: number;
    closed: number;
    recorded_closed?: number | null;
    first_opened: string | null;
    last_closed: string | null;
    last_recorded_closed?: string | null;
    closure_evidence?: ClosureEvidence | null;
  } | null;
  fills?: number | null;
  totals?: LedgerTotals;
  funding_unknown_currencies?: (string | null)[];
  fee_unknown_currencies?: (string | null)[];
  funding_observations?: {
    status: "partial";
    events: {
      account_id: string;
      venue_key: string;
      account_type: string;
      symbol: string;
      venue_income_id: string;
      funding_at: string;
      amount: string;
      currency: string;
      venue_semantics: string;
    }[];
    truncated: boolean;
  };
  coverage?: CoverageRow[];
};

export type BriefPortfolio = PortfolioPayload;

export type AttentionItem = {
  id: string;
  kind: string;
  priority: 1 | 2 | 3;
  message_code: string;
  message_params: Record<string, number | string | null>;
  detail: string | null;
  link: string;
};

/**
 * The active Kronos forecast of the most recent recorded cycle. `state` names
 * the recorded cycle outcome: no record yet, read failure, elapsed window,
 * unavailable forecast, or a usable forecast.
 */
export type OhlcQualityStatus = "consistent" | "corrected" | "unknown";

export type OhlcQuality = {
  status: OhlcQualityStatus;
  algorithm: "ohlc_wick_bounds_v1" | null;
  version: 1 | null;
  corrected_bar_count: number | null;
  corrected_path_count: number | null;
  changed_field_counts: { highs: number; lows: number } | null;
  absolute_delta_by_field: {
    highs: { total: number; max: number };
    lows: { total: number; max: number };
  } | null;
  reason: string | null;
};

export type BriefActiveForecast = {
  state: "ok" | "empty" | "error" | "stale" | "unavailable";
  status: string;
  reason?: string | null;
  cycle_id?: string | null;
  symbol?: string | null;
  quote_currency?: string | null;
  policy?: string | null;
  defer_reason?: string | null;
  recorded_at?: string | null;
  note?: string;
  forecast?: {
    symbol?: string | null;
    status?: string | null;
    reason?: string | null;
    direction?: string | null;
    calibration_label?: string | null;
    distribution_kind?: string | null;
    direction_counts?: Record<string, number> | null;
    direction_ratio?: Record<string, number> | null;
    band?: Record<string, number | null> | null;
    band_spread_ratio?: number | null;
    signal_basis?: "close_only" | null;
    ohlc_quality?: OhlcQuality | null;
    reference_close?: number | null;
    data_open_time?: number | null;
    as_of?: string | null;
    horizon_hours?: number | null;
    timeframe?: string | null;
    paths?: number | null;
    per_path_samples?: number | null;
    generation_kind?: string | null;
    temperature?: number | null;
    model_revision?: string | null;
    latency_ms?: number | null;
    created_at?: string | null;
  } | null;
};

export type BriefPayload = {
  generated_at: string;
  stale_after: number;
  disclaimer: string;
  read_only: boolean;
  status: {
    exec_mode: string;
    auto_spot: boolean | null;
    auto_futures: boolean | null;
    any_auto_enabled: boolean | null;
    venue_profile: "demo" | "production" | null;
    account_scope: string | null;
    halted_today: boolean | null;
    halt_reason: string | null;
    pending_approvals: number | null;
    server_status: string | null;
    audit_log_readable: boolean | null;
    quote_currency: string | null;
  };
  funnel:
    | { status: "ok"; buckets: Record<string, number>; total: number }
    | { status: "unavailable"; reason: string };
  results: {
    ledger: BriefLedger;
    estimated: {
      spot_audit_realized_total: number | null;
      spot_audit_realized_today: number | null;
      spot_currency: string | null;
      spot_closed_trades: number | null;
      spot_unknown_basis_closes: number | null;
      futures_unrealized: number | null;
      futures_unrealized_currency: string | null;
    };
    unattributed_manual: {
      window_start: string | null;
      window_end: string | null;
      count: number | string;
      account_id: string | null;
    }[];
    coverage_note: string;
    ledger_note: string;
  };
  risk: {
    open_positions: { spot: number | null; futures: number | null };
    positions_read: {
      status: string;
      skipped: number | null;
      issues: { symbol: string | null; reason: string }[];
      observed_at: string | null;
    } | null;
    exposure: { value_pct: number | null; currency: string | null; reason: string | null };
    heat: { value_pct: number | null; reason: string };
    limits: Record<string, number | string | null>;
    protection: {
      scope: string;
      configured: number | null;
      missing: number | null;
      unknown: number | null;
      open_positions_verified: boolean;
      reason: string;
    };
    futures_status: string | null;
    futures_error: string | null;
    account_scope: string | null;
  };
  activity: {
    scope: string;
    window: string;
    cycles_today: number | null;
    analysed_today: number | null;
    entries_today: number | null;
    no_entry_top_reasons: {
      code: string;
      reason: string | null;
      count: number;
      sample_cycle_id: string | null;
    }[];
    pipeline_error_cycles: number | null;
    note: string;
    reason?: string;
  };
  active_forecast: BriefActiveForecast;
  experiments:
    | {
        status: "ok";
        total: number | null;
        latest: { run_id: string; hypothesis: string | null; created_at: string | null } | null;
        verdicts: Record<string, number>;
        note: string;
      }
    | { status: "unavailable"; reason: string };
  portfolio: BriefPortfolio | null;
  needs_attention: AttentionItem[];
};

export function fetchBrief(): Promise<BriefPayload> {
  return fetchJson<BriefPayload>("/api/brief");
}

/** Parse non-ledger numeric display values; unreadable values remain null. */
export function asNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function asCount(value: unknown): number | null {
  const parsed = asNumber(value);
  return parsed === null ? null : Math.trunc(parsed);
}

export type Scope = "all" | "spot" | "futures";

/** Which scope a block belongs to, so one filter drives every section. */
export type ScopeTag = "spot" | "futures" | "both";

export function inScope(scope: Scope, tag: ScopeTag): boolean {
  if (scope === "all") return true;
  if (tag === "both") return true;
  return scope === tag;
}
