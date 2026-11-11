// Brief data layer: the wire shape of GET /api/brief (docs/ui-api-contract.md §16,
// plan §5.1). Types only — rendering lives in the components beside this file.
//
// Two rules from the contract show up directly in these types:
//   * a missing value is `null`, never `0` — the renderers must say "not enough
//     data" rather than print a zero the server never claimed;
//   * decimals travel as strings (the ledger's own convention) so precision is
//     not lost in float conversion. `asNumber` is the only place they are parsed,
//     and it returns null for anything it cannot read exactly.

import { fetchJson } from "@/components/overview/contract";
import type { PortfolioPayload } from "@/components/overview/contract";

export type CoverageStatus = "ok" | "partial" | "error" | "unsupported" | "unavailable";

export type NetRow = {
  currency: string | null;
  realized: string | null;
  fees: string | null;
  funding: string | null;
  net: string | null;
  funding_known?: boolean;
  fee_known?: boolean;
  /** Decimal totals are exact strings of the stored numeric columns. */
  amount?: string | null;
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
  funding_by_currency?: Record<string, { received: string; paid: string; events: number }>;
  net_by_currency?: NetRow[];
} | null;

export type BriefLedger = {
  status: "partial" | "unavailable" | "error";
  reason?: string;
  positions?: { total: number; closed: number } | null;
  fills?: number | null;
  totals?: LedgerTotals;
  funding_unknown_currencies?: string[];
  fee_unknown_currencies?: string[];
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

export type BriefShadowProvider = {
  provider: string;
  distribution_kind: "model_samples" | "deterministic_rule";
  attempts: { rows: number; ok: number; unavailable: number; error: number; timeout: number };
  verdicts: { up: number; down: number; flat: number };
  evaluations: {
    rows: number;
    direction_scored: number;
    direction_hits: number;
    band_scored: number;
    band_inside: number;
    net_r_rows: number;
    stop_first_rate_mean: number | null;
  };
};

/**
 * The ACTIVE Kronos forecast of the most recent recorded cycle — its own block,
 * never a shadow row. `state` names which situation this is and they are not
 * interchangeable: `off` (switch off), `empty` (on, nothing recorded), `error`
 * (read failed), `stale` (the recorded window fully elapsed), `unavailable`
 * (the recorded block itself failed) and `ok`. A failed block carries NO
 * numbers, so the renderer must never print ratios from one.
 */
export type BriefActiveForecast = {
  state: "ok" | "off" | "empty" | "error" | "stale" | "unavailable";
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

/** The brief's shadow slot: what the observation layer recorded, or why nothing. */
export type BriefShadow = {
  /**
   * Which kind of "nothing to show" this is. The three are NOT interchangeable:
   * `disabled` means the operator switched the worker off, `empty` means it is
   * on but has recorded nothing yet, `error` means the read failed. Rendering
   * one sentence for all three (as the section first did) told an operator with
   * the worker enabled that it was "not enabled".
   */
  state?: "ok" | "disabled" | "empty" | "error";
  status: string;
  reason?: string | null;
  providers?: string[];
  kronos?: { available: boolean; reason: string | null } | null;
  totals?: {
    rows: number;
    ok: number;
    unavailable: number;
    errors: number;
    timeouts: number;
    retrospective: number;
    first_data_open_time: string | null;
    last_data_open_time: string | null;
  } | null;
  providers_detail?: BriefShadowProvider[];
  observations?: {
    symbol: string;
    timeframe: string;
    horizon_hours: number;
    provider: string;
    direction: string | null;
    status: string;
    generation_kind: string;
    outcome_group: string;
    note?: string | null;
  }[];
  note?: string;
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
    shadow: BriefShadow;
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

/** Parse a wire decimal exactly; anything unreadable is null, never 0. */
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
