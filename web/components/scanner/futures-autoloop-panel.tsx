"use client";

import { Activity, Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatTimestamp, formatUsd } from "./scanner-format";
import type { FuturesCycle, FuturesCycleCandidate } from "./scanner-types";
import { useFuturesCycles } from "./use-futures-cycles";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  claimed: "outline",
  running: "default",
  completed: "secondary",
  interrupted: "destructive",
  recovery_pending: "destructive",
  failed: "destructive",
};

function label(value: string | null | undefined): string {
  return value?.trim() || "Chưa có dữ liệu";
}

function numberLabel(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "Chưa có dữ liệu" : formatUsd(value);
}

function directionLabel(value: string | null | undefined): string {
  if (value === "bullish") return "Tăng";
  if (value === "bearish") return "Giảm";
  if (value === "doji") return "Doji";
  return label(value);
}

function valueLabel(value: unknown): string {
  if (value == null || value === "") return "Chưa có dữ liệu";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderCandidates(candidates: FuturesCycleCandidate[] | undefined) {
  if (!candidates || candidates.length === 0) return null;
  return (
    <div className="mt-3 border-t pt-3">
      <p className="text-xs font-medium">Ứng viên / skip</p>
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

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-4" aria-hidden />
          Futures autoloop
        </CardTitle>
        <CardDescription>
          {enabled ? "Đang bật" : "Đang tắt"} · {runningCycle ? "Đang chạy cycle" : "Đang chờ"}
          {intervalMinutes != null ? ` · nhịp ${intervalMinutes} phút` : ""}. Dữ liệu đọc trực tiếp từ server.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        {loading && cycles.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Đang tải trạng thái futures…
          </p>
        ) : null}
        {!loading && cycles.length === 0 ? (
          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
            Chưa có cycle futures nào được ghi nhận.
          </p>
        ) : null}
        {cycles.length > 0 ? (
          <div className="space-y-3">
            {cycles.map((cycle) => <FuturesCycleRow key={cycle.cycle_id} cycle={cycle} />)}
          </div>
        ) : null}
        <Button variant="outline" size="sm" className="min-touch" onClick={refresh}>
          <RefreshCw data-icon="inline-start" aria-hidden />
          Làm mới
        </Button>
      </CardContent>
    </Card>
  );
}

function FuturesCycleRow({ cycle }: { cycle: FuturesCycle }) {
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
            <span className="text-xs text-muted-foreground">yêu cầu: {payload.requested_ticker}</span>
          ) : null}
          <Badge variant={STATUS_VARIANT[cycle.status] ?? "outline"}>{label(cycle.status)}</Badge>
          <Badge variant="outline">{label(cycle.phase)}</Badge>
        </div>
        <span className="text-xs text-muted-foreground">{formatTimestamp(cycle.updated_at)}</span>
      </div>
      <dl className="mt-3 grid gap-x-4 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div><dt className="text-muted-foreground">Cycle</dt><dd className="font-mono break-all">{cycle.cycle_id}</dd></div>
        <div><dt className="text-muted-foreground">Unified market</dt><dd className="font-mono break-all">{unifiedSymbol ?? cycle.symbol}</dd></div>
        <div><dt className="text-muted-foreground">Candle 4h mở / đóng</dt><dd>{formatTimestamp(cycle.four_hour_open)} / {valueLabel(snapshot.four_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">Candle 1h mở / đóng</dt><dd>{formatTimestamp(cycle.one_hour_open)} / {valueLabel(snapshot.one_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">4h kế tiếp / gần nhất</dt><dd>{valueLabel(snapshot.next_four_hour_open)} / {valueLabel(snapshot.last_four_hour_close)}</dd></div>
        <div><dt className="text-muted-foreground">Hướng 1h</dt><dd>{directionLabel(snapshot.one_hour_direction)}</dd></div>
        <div><dt className="text-muted-foreground">Hướng 4h</dt><dd>{directionLabel(snapshot.four_hour_direction)}</dd></div>
        <div><dt className="text-muted-foreground">Confirmation 1h+4h</dt><dd>{confirmation == null ? label(payload.confirmation_reason) : confirmation ? "Đạt" : "Thiếu / từ chối"}</dd></div>
        <div><dt className="text-muted-foreground">Lý do confirmation</dt><dd>{label(payload.confirmation_reason)}</dd></div>
        <div><dt className="text-muted-foreground">Tín hiệu</dt><dd>{label(payload.signal)}</dd></div>
        <div><dt className="text-muted-foreground">Lý do tín hiệu</dt><dd>{label(payload.signal_reason)}</dd></div>
        <div><dt className="text-muted-foreground">Market bị loại</dt><dd>{label(payload.market_rejection_reason)}</dd></div>
        <div><dt className="text-muted-foreground">Lý do kế hoạch</dt><dd>{label(payload.plan_reason ?? plan.reason)}</dd></div>
        <div><dt className="text-muted-foreground">Execution</dt><dd>{label(payload.execution_status)}</dd></div>
        <div><dt className="text-muted-foreground">Entry / order</dt><dd>{payload.entry_order_id ?? payload.order_id ?? (payload.executed ? "Đã thực thi" : "Chưa có dữ liệu")}</dd></div>
        <div><dt className="text-muted-foreground">SL / TP</dt><dd>{numberLabel(plan.stop_loss)} / {numberLabel(plan.take_profit)}</dd></div>
        <div><dt className="text-muted-foreground">Claim</dt><dd>{payload.claims?.map((claim) => claim.status ?? "claimed").join(", ") || (cycle.status === "claimed" ? "claimed" : "Chưa có dữ liệu")}</dd></div>
        <div><dt className="text-muted-foreground">Bảo vệ</dt><dd>{label(payload.protection_state)}</dd></div>
        <div><dt className="text-muted-foreground">Position</dt><dd>{payload.position_state ? valueLabel(payload.position_state) : "Chưa có dữ liệu"}</dd></div>
        <div><dt className="text-muted-foreground">Recovery</dt><dd>{label(payload.recovery_state)}</dd></div>
      </dl>
      {renderCandidates(payload.candidates)}
      {payload.market_rejection_reason || payload.confirmation_reason || payload.plan_reason || payload.reason || payload.reason_code || payload.error || payload.skipped_reason ? (
        <p className="mt-3 border-t pt-2 text-xs text-muted-foreground">
          {payload.reason_code ? `${payload.reason_code}: ` : ""}
          {payload.error ?? payload.market_rejection_reason ?? payload.skipped_reason ?? payload.plan_reason ?? payload.reason}
        </p>
      ) : null}
    </article>
  );
}
