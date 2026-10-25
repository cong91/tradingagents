"use client";

import { Activity, Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatTimestamp, formatUsd } from "./scanner-format";
import type { FuturesCycle, FuturesCycleCandidate } from "./scanner-types";
import { useFuturesCycles } from "./use-futures-cycles";
import { useT, type Messages } from "@/lib/i18n";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  claimed: "outline",
  running: "default",
  completed: "secondary",
  interrupted: "destructive",
  recovery_pending: "destructive",
  failed: "destructive",
};

// Giá trị backend (status/phase/reason…) hiển thị nguyên văn; chỉ placeholder
// "chưa có dữ liệu" và hướng nến được dịch.
function label(t: Messages, value: string | null | undefined): string {
  return value?.trim() || t.scanner.futures.noData;
}

function numberLabel(t: Messages, value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? t.scanner.futures.noData : formatUsd(value);
}

function directionLabel(t: Messages, value: string | null | undefined): string {
  if (value === "bullish") return t.scanner.futures.up;
  if (value === "bearish") return t.scanner.futures.down;
  if (value === "doji") return "Doji";
  return label(t, value);
}

function valueLabel(t: Messages, value: unknown): string {
  if (value == null || value === "") return t.scanner.futures.noData;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderCandidates(t: Messages, candidates: FuturesCycleCandidate[] | undefined) {
  if (!candidates || candidates.length === 0) return null;
  return (
    <div className="mt-3 border-t pt-3">
      <p className="text-xs font-medium">{t.scanner.futures.candidates}</p>
      <ul className="mt-1 space-y-1 text-xs text-muted-foreground">
        {candidates.map((candidate, index) => (
          <li key={`${candidate.symbol ?? candidate.ticker ?? "candidate"}-${index}`}>
            <span className="font-mono">{candidate.symbol ?? candidate.ticker ?? "?"}</span>
            {candidate.signal ? ` · ${candidate.signal}` : ""}
            {candidate.reason_code ? ` · ${candidate.reason_code}` : ""}
            {candidate.reason ?? candidate.skipped_reason ? ` · ${candidate.reason ?? candidate.skipped_reason}` : ""}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function FuturesAutoloopPanel() {
  const { cycles, runningCycle, enabled, intervalMinutes, loading, error, refresh } = useFuturesCycles();
  const t = useT();
  const c = t.scanner.futures;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-4" aria-hidden />
          Futures autoloop
        </CardTitle>
        <CardDescription>
          {enabled ? c.enabled : c.disabled} · {runningCycle ? c.runningCycle : c.waiting}
          {intervalMinutes != null ? c.interval(intervalMinutes) : ""}{c.liveData}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        {loading && cycles.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {c.loading}
          </p>
        ) : null}
        {!loading && cycles.length === 0 ? (
          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
            {c.empty}
          </p>
        ) : null}
        {cycles.length > 0 ? (
          <div className="space-y-3">
            {cycles.map((cycle) => <FuturesCycleRow key={cycle.cycle_id} cycle={cycle} />)}
          </div>
        ) : null}
        <Button variant="outline" size="sm" className="min-touch" onClick={refresh}>
          <RefreshCw data-icon="inline-start" aria-hidden />
          {t.common.refresh}
        </Button>
      </CardContent>
    </Card>
  );
}

function FuturesCycleRow({ cycle }: { cycle: FuturesCycle }) {
  const t = useT();
  const c = t.scanner.futures;
  const payload = cycle.payload ?? {};
  const snapshot = payload.snapshot ?? {};
  const plan = payload.plan ?? {};
  const unifiedSymbol = payload.unified_symbol ?? null;
  // rev 6 đọc payload.confirmation_result; snapshot.confirmation là fallback tương thích
  // cho cycle row cũ chỉ ghi kết quả xác nhận trong snapshot.
  const confirmation =
    payload.confirmation_result ?? (typeof snapshot.confirmation === "boolean" ? snapshot.confirmation : null);
  return (
    <article className="rounded-lg border p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono font-medium">{cycle.symbol}</span>
          {payload.requested_ticker && payload.requested_ticker !== cycle.symbol ? (
            <span className="text-xs text-muted-foreground">{c.requested(payload.requested_ticker)}</span>
          ) : null}
          <Badge variant={STATUS_VARIANT[cycle.status] ?? "outline"}>{label(t, cycle.status)}</Badge>
          <Badge variant="outline">{label(t, cycle.phase)}</Badge>
        </div>
        <span className="text-xs text-muted-foreground">{formatTimestamp(cycle.updated_at)}</span>
      </div>
      <dl className="mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div><dt className="text-muted-foreground">Cycle</dt><dd className="font-mono break-all">{cycle.cycle_id}</dd></div>
        <div><dt className="text-muted-foreground">Unified market</dt><dd className="font-mono break-all">{unifiedSymbol ?? cycle.symbol}</dd></div>
        <div><dt className="text-muted-foreground">{c.candle4h}</dt><dd>{formatTimestamp(cycle.four_hour_open)} / {valueLabel(t, snapshot.four_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">{c.candle1h}</dt><dd>{formatTimestamp(cycle.one_hour_open)} / {valueLabel(t, snapshot.one_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">{c.next4h}</dt><dd>{valueLabel(t, snapshot.next_four_hour_open)} / {valueLabel(t, snapshot.last_four_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">{c.dir1h}</dt><dd>{directionLabel(t, snapshot.one_hour_direction)}</dd></div>
        <div><dt className="text-muted-foreground">{c.dir4h}</dt><dd>{directionLabel(t, snapshot.four_hour_direction)}</dd></div>
        <div><dt className="text-muted-foreground">{c.confirmation}</dt><dd>{confirmation == null ? label(t, payload.confirmation_reason) : confirmation ? c.passed : c.failedConfirm}</dd></div>
        <div><dt className="text-muted-foreground">{c.confirmationReason}</dt><dd>{label(t, payload.confirmation_reason)}</dd></div>
        <div><dt className="text-muted-foreground">{c.signal}</dt><dd>{label(t, payload.signal)}</dd></div>
        <div><dt className="text-muted-foreground">{c.signalReason}</dt><dd>{label(t, payload.signal_reason)}</dd></div>
        <div><dt className="text-muted-foreground">{c.marketRejected}</dt><dd>{label(t, payload.market_rejection_reason)}</dd></div>
        <div><dt className="text-muted-foreground">{c.planReason}</dt><dd>{label(t, payload.plan_reason ?? plan.reason)}</dd></div>
        <div><dt className="text-muted-foreground">Execution</dt><dd>{label(t, payload.execution_status)}</dd></div>
        <div><dt className="text-muted-foreground">Entry / order</dt><dd>{payload.entry_order_id ?? payload.order_id ?? (payload.executed ? c.executed : c.noData)}</dd></div>
        <div><dt className="text-muted-foreground">SL / TP</dt><dd>{numberLabel(t, plan.stop_loss)} / {numberLabel(t, plan.take_profit)}</dd></div>
        <div><dt className="text-muted-foreground">Claim</dt><dd>{payload.claims?.map((claim) => claim.status ?? "claimed").join(", ") || (cycle.status === "claimed" ? "claimed" : c.noData)}</dd></div>
        <div><dt className="text-muted-foreground">{c.protection}</dt><dd>{label(t, payload.protection_state)}</dd></div>
        <div><dt className="text-muted-foreground">Position</dt><dd>{payload.position_state ? valueLabel(t, payload.position_state) : c.noData}</dd></div>
        <div><dt className="text-muted-foreground">Recovery</dt><dd>{label(t, payload.recovery_state)}</dd></div>
      </dl>
      {renderCandidates(t, payload.candidates)}

      {payload.market_rejection_reason || payload.confirmation_reason || payload.plan_reason || payload.reason || payload.reason_code || payload.error || payload.skipped_reason ? (
        <p className="mt-3 border-t pt-2 text-xs text-muted-foreground">
          {payload.reason_code ? `${payload.reason_code}: ` : ""}
          {payload.error ?? payload.market_rejection_reason ?? payload.skipped_reason ?? payload.plan_reason ?? payload.reason}
        </p>
      ) : null}
    </article>
  );
}
