"use client";

// Phễu kết quả mọi chu kỳ autoloop + bảng theo mã. Click một mã để mở rộng xem
// từng chu kỳ của mã đó và lý do bị loại / vào lệnh theo ngôn ngữ đang chọn
// (GET /api/autoloop/cycles?symbol=…).

import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatSymbol } from "@/lib/format";
import { useT, type Messages } from "@/lib/i18n";
import { scannerApi } from "./scanner-api";
import { formatTimestamp } from "./scanner-format";
import type {
  FuturesCycle,
  FuturesCyclePayload,
  FuturesCycleSymbolRollup,
  FuturesCycleSummary,
} from "./scanner-types";

const FUNNEL_KEYS = [
  "market_rejected",
  "confirmation_rejected",
  "forecast_deferred",
  "proposal_rejected",
  "entered",
  "awaiting_approval",
  "risk_rejected",
  "not_executed",
  "execution_failed",
] as const;

// Badge status chu kỳ (statusLabels + variant theo status); giá trị lạ →
// nguyên văn. Panel chính và danh sách mở rộng dùng chung.
export const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  claimed: "outline",
  running: "default",
  completed: "secondary",
  interrupted: "destructive",
  recovery_pending: "destructive",
  failed: "destructive",
};

export function statusLabel(c: Messages["cycles"], status: string | null | undefined): string {
  return (status && c.statusLabels[status]) || status || "";
}

/** Chu kỳ mới nhất của mã đang claimed/running — payload job_store map vào
 *  bucket in_flight; dùng last_status (semantics "kết quả gần nhất"), không
 *  dùng row.in_flight (đếm cả chu kỳ kẹt cũ, không treo nhãn vĩnh viễn). */
function isInFlight(row: FuturesCycleSymbolRollup): boolean {
  return row.last_status === "claimed" || row.last_status === "running";
}

type CyclesCopy = Messages["cycles"];

function signalLabel(c: CyclesCopy, signal: string | null | undefined): string {
  return (signal ? c.signals[signal] : undefined) ?? (signal?.trim() || c.noData);
}

function marketRejectionLabel(c: CyclesCopy, reason: string | null | undefined): string {
  if (!reason) return c.noData;
  if (/no active USDT-settled futures contract/i.test(reason)) return c.noUsdtContract;
  return reason;
}

function confirmationLabel(c: CyclesCopy, reason: string | null | undefined): string {
  switch (reason) {
    case "doji_candle":
      return c.confirmation.doji_candle;
    case "direction_mismatch":
      return c.confirmation.direction_mismatch;
    case "confirmation_missing":
    case null:
    case undefined:
    case "":
      return c.confirmation.missing;
    default:
      return reason;
  }
}

function proposalLabel(c: CyclesCopy, reason: string | null | undefined): string {
  if (!reason) return c.noData;
  if (reason.startsWith("agent_proposal_missing")) return c.proposal.missing;
  if (reason.startsWith("agent_proposal_rejected")) {
    const detail = reason.slice("agent_proposal_rejected:".length).trim();
    return c.proposal.rejected(detail || c.proposal.rejectedDefault);
  }
  switch (reason) {
    case "agent_abstained":
      return c.proposal.agent_abstained;
    case "position_management_only":
      return c.proposal.position_management_only;
    case "structured_terms_unavailable":
      return c.proposal.structured_terms_unavailable;
    default:
      return reason;
  }
}

function phaseLabel(c: CyclesCopy, phase: string): string {
  return c.phases[phase] ?? phase;
}

/** Label for each terminal execution outcome — "entered" only when the payload
 *  confirms the order was sent (execution_status=executed); every other
 *  outcome states explicitly that no position was entered. */
function executionStatusLabel(c: CyclesCopy, payload: FuturesCyclePayload): string {
  const o = c.outcome;
  switch (payload.execution_status) {
    case "awaiting_approval":
      return o.awaitingApprovalReason(payload.execution_reason ?? o.gateClosed);
    case "risk_rejected":
      return o.riskRejectedReason(payload.execution_reason ?? payload.plan_reason ?? o.overRiskCap);
    case "not_executed":
      return o.notExecutedReason(payload.plan_reason ?? payload.execution_reason ?? o.noOrderFromPlan);
    case "failed":
      return o.failedReason(payload.failure_reason ?? payload.error ?? o.unknownReason);
    default:
      return "";
  }
}

/** Failure label by phase: a context/analysis failure (relay, market data) is
 *  not an order-send failure — naming the phase keeps the funnel from reading
 *  as if an order had reached the venue. */
function failureLabel(c: CyclesCopy, phase: string | null | undefined, reason: string | null | undefined): string {
  const o = c.outcome;
  if (phase === "execution" || phase === "entry" || phase === "protection") {
    return reason ? o.failedReason(reason) : o.failed;
  }
  if (phase === "analysis" || phase === "context") {
    return reason ? o.analysisFailedReason(reason) : o.analysisFailed;
  }
  return reason ? o.cycleFailedReason(reason) : o.cycleFailed;
}

function forecastLabel(c: CyclesCopy, payload: FuturesCyclePayload): string | null {
  const forecast = payload.kronos_forecast;
  if (!forecast) return null;
  const status = forecast.status?.trim() || payload.kronos_policy?.trim() || "unknown";
  const details = [
    forecast.direction?.trim(),
    forecast.paths == null ? null : `paths=${forecast.paths}`,
    forecast.band_spread_ratio == null ? null : `spread=${forecast.band_spread_ratio}`,
  ].filter((part): part is string => Boolean(part)).join(", ");
  if (status === "ok") return c.outcome.forecast(status, details);
  return forecast.reason ? c.outcome.forecast(status, forecast.reason) : c.outcome.forecast(status, details);
}

function cycleOutcome(c: CyclesCopy, cycle: FuturesCycle): string {
  const payload: FuturesCyclePayload = cycle.payload ?? {};
  if (cycle.status === "completed" && cycle.phase === "entry") {
    // entered only when execution_status=executed and the payload does not
    // contradict it (executed=false) — matches job_store.futures_cycle_summary.
    if (payload.execution_status === "executed" && payload.executed !== false) {
      return c.outcome.entered;
    }
    // Legacy cycles wrote completed/entry before execution_status existed:
    // map the recorded outcome, never infer "entered".
    return executionStatusLabel(c, payload) || c.outcome.legacyCompleted;
  }
  if (cycle.status === "rejected" && cycle.phase === "forecast"
      && payload.kronos_policy === "defer" && payload.execution_status !== "failed") {
    return payload.kronos_defer_reason
      ? c.outcome.forecastDeferredReason(payload.kronos_defer_reason)
      : c.outcome.forecastDeferred;
  }
  if (payload.execution_status) {
    const outcome = executionStatusLabel(c, payload);
    if (outcome) return outcome;
  }
  // Outer-layer failure (context/analysis): failed row with execution_status
  // not_started but a recorded failure_reason — show it under its real phase
  // instead of a generic reason.
  if (cycle.status === "failed" && payload.failure_reason) {
    return failureLabel(c, cycle.phase, payload.failure_reason);
  }
  if (cycle.phase === "market") return marketRejectionLabel(c, payload.market_rejection_reason);
  if (cycle.phase === "confirmation") return confirmationLabel(c, payload.confirmation_reason);
  if (cycle.phase === "proposal") {
    if (payload.signal_reason === "tradeable_signal") return c.outcome.tradeableUnconfirmed;
    return proposalLabel(c, payload.signal_reason);
  }
  if (payload.plan_reason) return payload.plan_reason;
  return payload.signal_reason ?? payload.reason ?? c.noData;
}

/** Latest outcome of a symbol, derived from status/phase/execution_status of
 *  its newest cycle — signal_reason is never taken as proof of entry.
 *  entered only when the payload confirms execution AND executed=false does
 *  not contradict it; legacy cycles missing `executed` ('') still count. */
function latestOutcome(c: CyclesCopy, row: FuturesCycleSymbolRollup): string {
  const o = c.outcome;
  const reason = row.last_execution_reason;
  // Nhánh in-flight đứng TRƯỚC mọi nhánh last_*: chu kỳ claimed/running chưa
  // có kết quả — không được suy ra "analyzed"/proposal/noData.
  if (isInFlight(row)) return o.inFlight;
  if (row.last_status === "completed" && row.last_phase === "entry"
      && row.last_execution_status === "executed"
      && row.last_executed !== "false") {
    return o.entered;
  }
  if (row.last_status === "rejected" && row.last_phase === "forecast"
      && row.last_kronos_policy === "defer" && row.last_execution_status !== "failed") {
    return row.last_kronos_defer_reason
      ? o.forecastDeferredReason(row.last_kronos_defer_reason)
      : o.forecastDeferred;
  }
  switch (row.last_execution_status) {
    case "awaiting_approval":
      return o.awaitingApproval;
    case "risk_rejected":
      return reason ? o.riskRejectedReason(reason) : o.riskRejected;
    case "not_executed":
      return reason ? o.notExecutedReason(reason) : o.notExecuted;
    case "failed":
      return reason ? o.failedReason(reason) : o.failed;
  }
  if (row.last_status === "failed") {
    return failureLabel(c, row.last_phase, row.last_execution_reason);
  }
  if (row.last_phase === "market") return marketRejectionLabel(c, row.last_market_rejection_reason);
  if (row.last_phase === "confirmation") return confirmationLabel(c, row.last_confirmation_reason);
  if (row.last_phase === "proposal") return proposalLabel(c, row.last_signal_reason);
  if (row.last_signal_reason === "tradeable_signal") return o.analyzed;
  if (row.last_signal_reason) return proposalLabel(c, row.last_signal_reason);
  return c.noData;
}

const BREAKDOWN_KEYS = [
  "entered",
  "awaiting_approval",
  "risk_rejected",
  "not_executed",
  "execution_failed",
  "proposal_rejected",
  "confirmation_rejected",
  "market_rejected",
  "forecast_deferred",
  "in_flight",
  "interrupted",
  "other",
] as const;

function breakdownParts(c: CyclesCopy, row: FuturesCycleSymbolRollup): string[] {
  return BREAKDOWN_KEYS.filter((key) => row[key]).map((key) => c.breakdown[key](row[key]));
}

const SUMMARY_EXTRA_KEYS = [
  "in_flight",
  "awaiting_approval",
  "risk_rejected",
  "not_executed",
  "execution_failed",
  "interrupted",
  "other",
] as const;

/** Khối trích nguyên văn quyết định của Quản lý danh mục cho một chu kỳ:
 * Rating, luận điểm và Futures Execution Intent — lý do thật đằng sau
 *  việc từ chối / vào lệnh, đúng từng chữ agent đã viết. */
export function AgentDecisionNote({ note }: { note: string | null | undefined }) {
  const t = useT();
  if (!note?.trim()) return null;
  return (
    <div className="mt-1.5 rounded-md border-l-2 border-muted-foreground/40 bg-muted/40 px-3 py-2">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        {t.cycles.agentNoteTitle}
      </p>
      <p className="mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap break-words text-xs leading-relaxed">
        {note}
      </p>
    </div>
  );
}

function SymbolRow({ row }: { row: FuturesCycleSymbolRollup }) {
  const t = useT();
  const c = t.cycles;
  const [open, setOpen] = useState(false);
  const [cycles, setCycles] = useState<FuturesCycle[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && cycles === null && !loading) {
      setLoading(true);
      scannerApi.futuresCycles(100, row.symbol).then(
        (body) => {
          setCycles(body.items);
          setError(null);
        },
        (err: unknown) => {
          setError(err instanceof Error ? err.message : c.loadSymbolFailed);
        },
      ).finally(() => setLoading(false));
    }
  };

  const Chevron = open ? ChevronDown : ChevronRight;
  const breakdown = breakdownParts(c, row);
  const inFlight = isInFlight(row);

  return (
    <>
      <tr className="align-top">
        <td className="py-2 pr-3">
          <button
            type="button"
            onClick={toggle}
            aria-expanded={open}
            className="flex min-touch items-center gap-1.5 rounded-md px-1.5 py-1 text-left font-mono text-sm hover:bg-sidebar-accent/60"
          >
            <Chevron className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            {formatSymbol(row.symbol)}
          </button>
        </td>
        <td className="py-2 pr-3 tabular-nums">{row.total}</td>
        <td className="py-2 pr-3 text-xs">
          {breakdown.length > 0 ? (
            <span className="text-muted-foreground">{breakdown.join(" · ")}</span>
          ) : (
            <span className="text-muted-foreground">{c.noData}</span>
          )}
        </td>
        <td className="py-2 pr-3">
          {inFlight && !row.last_signal ? c.outcome.inFlight : signalLabel(c, row.last_signal)}
        </td>
        <td className="py-2 pr-3 text-xs">{latestOutcome(c, row)}</td>
        <td className="py-2 whitespace-nowrap text-xs text-muted-foreground">
          {formatTimestamp(row.last_updated)}
        </td>
      </tr>
      {open ? (
        <tr>
          <td colSpan={6} className="pb-3">
            {loading ? (
              <p className="flex items-center gap-2 px-2 text-xs text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                {c.loadingSymbol(formatSymbol(row.symbol))}
              </p>
            ) : null}
            {error ? <p role="alert" className="px-2 text-xs text-destructive">{error}</p> : null}
            {cycles !== null && cycles.length === 0 && !error ? (
              <p className="px-2 text-xs text-muted-foreground">{c.noCycles}</p>
            ) : null}
            {cycles !== null && cycles.length > 0 ? (
              <ul className="space-y-1.5 px-2">
                {cycles.map((cycle) => (
                  <li key={cycle.cycle_id} className="rounded-md border p-2 text-xs">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="whitespace-nowrap text-muted-foreground">
                        {formatTimestamp(cycle.created_at)}
                      </span>
                      <Badge variant={STATUS_VARIANT[cycle.status] ?? "outline"}>
                        {statusLabel(c, cycle.status)}
                      </Badge>
                      <Badge variant="outline">{phaseLabel(c, cycle.phase)}</Badge>
                      <span className="font-medium">{signalLabel(c, cycle.payload?.signal)}</span>
                      <span className="text-muted-foreground">— {cycleOutcome(c, cycle)}</span>
                    </div>
                    {cycle.payload?.kronos_forecast ? (
                      <p className="mt-1 text-[11px] text-muted-foreground">
                        {forecastLabel(c, cycle.payload)}
                      </p>
                    ) : null}
                    <AgentDecisionNote note={cycle.payload?.agent_decision_note} />
                  </li>
                ))}
              </ul>
            ) : null}
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function FuturesCycleFunnel({
  summary,
  symbols,
}: {
  summary: FuturesCycleSummary | null;
  symbols: FuturesCycleSymbolRollup[];
}) {
  const t = useT();
  const c = t.cycles;
  if (!summary) return null;
  const extraParts = SUMMARY_EXTRA_KEYS.filter((key) => summary[key]).map((key) =>
    c.breakdown[key](summary[key]),
  );

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-9">
        {FUNNEL_KEYS.map((key) => (
          <div key={key} className="rounded-lg border p-3">
            <p className="text-lg font-semibold tabular-nums">{summary[key]}</p>
            <p className="text-xs font-medium">{c.funnel[key].label}</p>
            <p className="mt-1 text-[11px] leading-snug text-muted-foreground">{c.funnel[key].hint}</p>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {c.totalLine(summary.total)}
        {extraParts.length > 0 ? ` · ${extraParts.join(" · ")}` : ""}
        {c.clickHint}
      </p>
      {symbols.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{c.caption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">{c.colSymbol}</th>
                <th scope="col" className="py-2 pr-3 font-medium">{c.colCycles}</th>
                <th scope="col" className="py-2 pr-3 font-medium">{c.colBreakdown}</th>
                <th scope="col" className="py-2 pr-3 font-medium">{c.colLastSignal}</th>
                <th scope="col" className="py-2 pr-3 font-medium">{c.colLastOutcome}</th>
                <th scope="col" className="py-2 font-medium">{c.colUpdated}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {symbols.map((row) => (
                <SymbolRow key={row.symbol} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}
