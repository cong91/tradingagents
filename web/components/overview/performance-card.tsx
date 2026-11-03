"use client";

import { RefreshCwIcon, TriangleAlertIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatDateTime } from "@/lib/format";

import type { PerformanceHistoryPayload, PerformancePayload, PortfolioPayload } from "./contract";
import { PerformanceEvents } from "./performance-events";
import { PerformanceHistory } from "./performance-history";
import { PerformancePositions } from "./performance-positions";
import type { PanelState } from "./use-panel";

export function PerformanceCard({
  state,
  stale,
  onRetry,
  portfolioState,
  portfolioStale,
  futuresStale,
  onPortfolioRetry,
  historyState,
  historyStale,
  onHistoryRetry,
}: {
  state: PanelState<PerformancePayload>;
  stale: boolean;
  onRetry: () => void;
  portfolioState: PanelState<PortfolioPayload>;
  portfolioStale: boolean;
  futuresStale: boolean;
  onPortfolioRetry: () => void;
  historyState: PanelState<PerformanceHistoryPayload>;
  historyStale: boolean;
  onHistoryRetry: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Hiệu suất giao dịch</CardTitle>
        <CardDescription>Vị thế từ sàn, khớp lệnh Futures và nhật ký sự kiện.</CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" className="min-touch" onClick={() => {
            onPortfolioRetry();
            onHistoryRetry();
            onRetry();
          }}>
            <RefreshCwIcon aria-hidden="true" /> Làm mới
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        <PerformancePositions portfolioState={portfolioState} stale={portfolioStale} futuresStale={futuresStale} onRetry={onPortfolioRetry} />

        <section className="space-y-2 border-t pt-4" aria-label="Thống kê giao dịch đã đóng">
          <h3 className="text-sm font-medium">Thống kê giao dịch đã đóng</h3>
          <p className="text-sm text-muted-foreground">
            — Chưa đủ dữ liệu đối soát để xác nhận số lệnh đóng, tỷ lệ thắng và kỳ vọng.
          </p>
        </section>

        <section className="border-t pt-4">
          {historyState.status === "loading" ? (
            <p role="status" className="text-sm text-muted-foreground">Đang tải lịch sử khớp lệnh…</p>
          ) : historyState.status === "error" ? (
            <div role="alert" className="space-y-2 text-sm">
              <p className="text-destructive">Không đọc được lịch sử khớp lệnh ({historyState.code}): {historyState.message}</p>
              <Button variant="outline" size="sm" className="min-touch" onClick={onHistoryRetry}>Thử lại</Button>
            </div>
          ) : (
            <>
              {historyState.status === "stale" || historyStale ? (
                <p role="alert" className="mb-3 flex items-center gap-2 text-sm text-destructive">
                  <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
                  Lịch sử cũ; lần đọc gần nhất lúc {formatDateTime(historyState.data.observed_at)}.
                </p>
              ) : null}
              <PerformanceHistory history={historyState.data} />
            </>
          )}
        </section>

        <section className="border-t pt-4">
          {state.status === "loading" ? (
            <p role="status" className="text-sm text-muted-foreground">Đang tải nhật ký…</p>
          ) : state.status === "error" ? (
            <div role="alert" className="space-y-2 text-sm">
              <p className="text-destructive">Không đọc được nhật ký ({state.code}): {state.message}</p>
              <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>Thử lại</Button>
            </div>
          ) : (
            <>
              {state.status === "stale" || stale ? <Badge variant="outline">Nhật ký cũ · {formatDateTime(state.data.generated_at)}</Badge> : null}
              <PerformanceEvents events={state.data.recent_orders} skippedLines={state.data.skipped_lines ?? null} />
            </>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
