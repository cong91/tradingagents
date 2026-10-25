"use client";

import { ActivityIcon, RefreshCwIcon, TriangleAlertIcon } from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatDateTime } from "@/lib/format";
import type { AutoloopTelemetry } from "@/components/settings/autoloop-types";
import { useOverviewActivity } from "./use-overview-activity";

function statusLabel(status: string): string {
  return ({
    running: "Đang chạy",
    queued: "Đang chờ",
    completed: "Hoàn tất",
    failed: "Thất bại",
    cancelled: "Đã dừng",
    interrupted: "Bị gián đoạn",
    timeout: "Hết thời gian",
  } as Record<string, string>)[status] ?? status;
}

function elapsedLabel(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const seconds = Math.floor(value / 1000);
  return seconds < 60 ? `${seconds} giây` : `${Math.floor(seconds / 60)} phút ${seconds % 60} giây`;
}

function ActivityRow({
  title,
  status,
  href,
  detail,
  error,
}: {
  title: string;
  status: string;
  href: string;
  detail: string[];
  error?: string | null;
}) {
  return (
    <article className="min-w-0 border-t py-3 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">{title}</h3>
        <Badge variant={status === "failed" || status === "interrupted" || status === "timeout" ? "destructive" : "outline"}>
          {statusLabel(status)}
        </Badge>
      </div>
      {detail.length > 0 ? <p className="mt-1 break-words text-sm text-muted-foreground">{detail.join(" · ")}</p> : null}
      {error ? <p role="alert" className="mt-1 break-words text-xs text-destructive">{error}</p> : null}
      <Link href={href} className="mt-2 inline-flex min-touch items-center text-xs underline underline-offset-4">
        Chi tiết
      </Link>
    </article>
  );
}

function telemetryDetails(telemetry: AutoloopTelemetry | undefined): string[] {
  if (!telemetry) return [];
  const ticker = telemetry.active_ticker ?? telemetry.current_ticker;
  const stage = telemetry.active_stage ?? telemetry.last_stage;
  return [
    ticker ? `Mã ${ticker}` : null,
    telemetry.active_node ? `Agent ${telemetry.active_node}` : null,
    stage ? `Bước ${stage}` : null,
    elapsedLabel(telemetry.active_elapsed_ms),
  ].filter((value): value is string => value !== null);
}

export function OverviewActivityCard() {
  const { state, refresh } = useOverviewActivity();
  const data = state.status === "ready" || state.status === "stale" ? state.data : null;
  const job = data?.dailyJob;
  const run = data?.activeRun;
  const lastCycle = data?.autoloop?.last_cycle;
  const cycleStatus = data?.autoloop?.running_cycle ? "running" : lastCycle?.status ?? "idle";
  const cycleDetails = telemetryDetails(lastCycle?.telemetry);
  if (lastCycle?.results.length) {
    const executed = lastCycle.results.filter((item) => item.executed).length;
    cycleDetails.push(`${executed}/${lastCycle.results.length} lệnh đã thực thi`);
  }

  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><ActivityIcon className="size-4" aria-hidden="true" />Hoạt động hiện tại</CardTitle>
        <CardDescription>Theo dõi riêng vòng quét, job hằng ngày và phiên phân tích; không thay đổi trạng thái giao dịch.</CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" className="min-touch" onClick={() => void refresh()}>
            <RefreshCwIcon aria-hidden="true" />Làm mới
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-3">
        {state.status === "loading" ? <p className="text-sm text-muted-foreground" role="status">Đang tải hoạt động…</p> : null}
        {state.status === "error" ? <p className="text-sm text-destructive" role="alert">Không đọc được trạng thái hoạt động: {state.message}</p> : null}
        {state.status === "stale" ? (
          <p role="alert" className="flex items-center gap-2 text-xs text-destructive">
            <TriangleAlertIcon className="size-3 shrink-0" aria-hidden="true" />
            Poll gần nhất lỗi; đang giữ dữ liệu{state.fetchedAt ? ` từ ${formatDateTime(new Date(state.fetchedAt).toISOString())}` : " chưa xác nhận thời điểm cập nhật"}: {state.message}
          </p>
        ) : null}
        {data ? (
          <div className="grid gap-3 md:grid-cols-3">
            <ActivityRow
              title="Autoloop"
              status={cycleStatus}
              href="/settings"
              detail={cycleDetails}
              error={data.errors.autoloop ?? lastCycle?.error}
            />
            <ActivityRow
              title="Quét hằng ngày"
              status={job?.status ?? (data.daily?.active_job_id ? "running" : "idle")}
              href="/scanner"
              detail={[
                job?.current_ticker ? `Mã ${job.current_ticker}` : null,
                job ? `${job.results.length}/${job.tickers.length} kết quả` : null,
                job?.screening ? `Đã sàng ${job.screening.universe_count} mã` : null,
              ].filter((value): value is string => value !== null)}
              error={data.errors.daily ?? job?.error}
            />
            <ActivityRow
              title="Phiên phân tích"
              status={run?.status ?? "idle"}
              href="/pipeline"
              detail={run ? [run.ticker, run.trade_date, run.run_id] : []}
              error={data.errors.run ?? run?.error?.message}
            />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
