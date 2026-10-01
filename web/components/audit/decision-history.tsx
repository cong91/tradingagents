"use client";

// Section "Lịch sử quyết định" — GET /api/history (hợp đồng §7, server/history.py:76-100).
// Decision log + run tree, đánh dấu quyết định chưa settle; stale sau 60s.

import { useEffect, useMemo, useState } from "react";
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
import { DataMeta, EmptyState, ErrorState, SectionSkeleton } from "./section-states";
import { HistoryEntryRow } from "./history-entry";
import { useSectionData } from "./use-section-data";
import type { HistoryPayload } from "./types";

const PAGE_STEP = 25;
const STALE_SECONDS = 60;

export function DecisionHistoryCard() {
  const [tickerInput, setTickerInput] = useState("");
  const [ticker, setTicker] = useState("");
  const [pendingOnly, setPendingOnly] = useState(false);
  const [limit, setLimit] = useState(PAGE_STEP);

  // Gõ ticker → debounce 300ms; filter mới → nạp lại từ đầu (25 entry đầu).
  useEffect(() => {
    const id = setTimeout(() => {
      setTicker(tickerInput.trim().toUpperCase());
      setLimit(PAGE_STEP);
    }, 300);
    return () => clearTimeout(id);
  }, [tickerInput]);

  const url = useMemo(() => {
    const query = new URLSearchParams();
    if (ticker) query.set("ticker", ticker);
    query.set("pending_only", String(pendingOnly));
    query.set("limit", String(limit));
    query.set("offset", "0");
    return `/api/history?${query.toString()}`;
  }, [ticker, pendingOnly, limit]);

  const { state, refresh, refreshing } = useSectionData<HistoryPayload>(url);
  const data = state.status === "ready" ? state.data : null;
  const hasMore = data != null && data.items.length < data.total;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Lịch sử quyết định</CardTitle>
        <CardDescription>
          {
            "GET /api/history — quyết định đã lưu trong decision log, ghép với tệp run trên đĩa. Quyết định \"Chưa settle\" chưa thể chấm điểm."
          }
        </CardDescription>
        <CardAction>
          <DataMeta
            generatedAt={data?.generated_at ?? null}
            staleAfterSeconds={STALE_SECONDS}
            onRefresh={refresh}
            refreshing={refreshing}
          />
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_auto] md:items-end">
          <div>
            <label htmlFor="history-ticker" className="text-xs text-muted-foreground">
              Ticker
            </label>
            <input
              id="history-ticker"
              value={tickerInput}
              onChange={(event) => setTickerInput(event.target.value)}
              placeholder="Tất cả các ticker"
              className="mt-1 h-9 w-full rounded-lg border border-input bg-background px-3 text-sm md:max-w-64"
            />
          </div>
          <div className="flex items-center gap-2 pb-1.5">
            <input
              id="history-pending-only"
              type="checkbox"
              checked={pendingOnly}
              onChange={(event) => {
                setPendingOnly(event.target.checked);
                setLimit(PAGE_STEP);
              }}
              className="size-4 accent-[var(--primary)]"
            />
            <label htmlFor="history-pending-only" className="text-sm">
              Chỉ quyết định chưa settle
            </label>
          </div>
        </div>

        {state.status === "loading" && <SectionSkeleton rows={4} />}
        {state.status === "error" && (
          <ErrorState message={state.error.message} onRetry={refresh} />
        )}
        {state.status === "ready" && state.data.items.length === 0 && (
          <EmptyState
            title="Chưa có quyết định nào được ghi nhận"
            hint={
              data != null && data.total === 0 && ticker === "" && !pendingOnly
                ? "Decision log trống hoặc chưa có phiên phân tích nào. Chạy một phiên ở M2 để tạo quyết định đầu tiên, kết quả sẽ tự xuất hiện ở đây."
                : `Không có quyết định nào khớp bộ lọc hiện tại (tổng cộng ${data?.total ?? 0} quyết định). Thử xóa bộ lọc ticker hoặc bỏ tùy chọn \"chưa settle\".`
            }
            actionHref="/pipeline"
            actionLabel="Chạy phiên phân tích (M2)"
          />
        )}
        {state.status === "ready" && state.data.items.length > 0 && (
          <>
            {data?.warning && (
              <p className="rounded-lg border border-amber-500/60 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100">
                Cảnh báo: {data.warning}
              </p>
            )}
            {data && Object.keys(data.by_ticker).length > 1 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-muted-foreground">Theo ticker:</span>
                {Object.entries(data.by_ticker).map(([symbol, count]) => (
                  <Badge key={symbol} variant="outline">
                    {symbol} · {count}
                  </Badge>
                ))}
              </div>
            )}
            <ul className="space-y-2">
              {state.data.items.map((item) => (
                <HistoryEntryRow
                  key={`${item.date}-${item.ticker}`}
                  item={item}
                />
              ))}
            </ul>
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">
                Hiển thị {state.data.items.length}/{state.data.total} quyết định
              </p>
              {hasMore && (
                <Button
                  variant="outline"
                  size="sm"
                  className="min-touch"
                  onClick={() => setLimit((current) => current + PAGE_STEP)}
                >
                  Tải thêm {PAGE_STEP}
                </Button>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
