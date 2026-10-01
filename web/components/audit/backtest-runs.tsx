"use client";

// Section "Backtest" — GET /api/backtest (hợp đồng §7, server/history.py:103-150).
// Danh sách lần sweep + tổng kết theo rating; bung chi tiết nạp entry thật
// của run đó (?run_id=…). Chi tiết nạp lười — chỉ khi người dùng bấm.

import { useState } from "react";
import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
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
import {
  DataMeta,
  EmptyState,
  ErrorState,
  SectionSkeleton,
} from "./section-states";
import {
  formatFractionPercent,
  formatHitRate,
  signedTextTone,
} from "./display";
import { HistoryEntryRow } from "./history-entry";
import { useSectionData } from "./use-section-data";
import type { BacktestDetailPayload, BacktestListPayload, BacktestRun } from "./types";

const STALE_SECONDS = 60;

function ByRatingTable({ run }: { run: BacktestRun }) {
  const ratings = Object.entries(run.by_rating);
  if (ratings.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        Không có quyết định nào được chấm điểm theo rating.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <caption className="sr-only">
          Tổng kết backtest theo rating — run {run.run_id}
        </caption>
        <thead>
          <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
            <th scope="col" className="px-3 py-2 font-medium">Rating</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Số quyết định</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Tỉ lệ trúng</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Alpha trung bình</th>
          </tr>
        </thead>
        <tbody>
          {ratings.map(([rating, score]) => {
            const alpha = formatFractionPercent(score.mean_alpha);
            return (
              <tr key={rating} className="border-b last:border-b-0">
                <td className="px-3 py-2 font-medium">{rating}</td>
                <td className="px-3 py-2 text-right tabular-nums">{score.count}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {formatHitRate(score.hit_rate)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {alpha && (
                    <span className={signedTextTone(alpha)}>{alpha}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const { state, refresh } = useSectionData<BacktestDetailPayload>(
    `/api/backtest?run_id=${encodeURIComponent(runId)}`
  );
  if (state.status === "loading") return <SectionSkeleton rows={2} />;
  if (state.status === "error") {
    return <ErrorState message={state.error.message} onRetry={refresh} />;
  }
  const { data } = state;
  return (
    <div className="space-y-2 rounded-lg bg-muted/40 p-3">
      {data.warning && (
        <p className="text-xs text-amber-900 dark:text-amber-100">
          Cảnh báo: {data.warning}
        </p>
      )}
      {data.entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Run này không ghi nhật ký quyết định nào.
        </p>
      ) : (
        <ul className="space-y-2">
          {data.entries.map((item) => (
            <HistoryEntryRow key={`${item.date}-${item.ticker}`} item={item} />
          ))}
        </ul>
      )}
    </div>
  );
}

function RunRow({
  run,
  expanded,
  onToggle,
}: {
  run: BacktestRun;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <li className="rounded-xl border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold">{run.run_id}</span>
        <Badge variant="secondary">{run.entries} quyết định</Badge>
        <Badge variant="outline">{run.resolved} đã chấm điểm</Badge>
        {run.pending > 0 && <Badge variant="outline">{run.pending} chưa settle</Badge>}
        {run.unscored > 0 && <Badge variant="outline">{run.unscored} không chấm được</Badge>}
        <Button
          variant="outline"
          size="sm"
          className="ml-auto min-touch"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? (
            <>
              Đóng chi tiết
              <ChevronUpIcon aria-hidden="true" />
            </>
          ) : (
            <>
              Xem chi tiết
              <ChevronDownIcon aria-hidden="true" />
            </>
          )}
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {run.holding ? `Thời gian giữ trung bình: ${run.holding}. ` : ""}
        Log: <span className="font-mono">{run.log_path}</span>
      </p>
      {run.warning && (
        <p className="mt-1 text-xs text-amber-900 dark:text-amber-100">
          Cảnh báo: {run.warning}
        </p>
      )}
      <div className="mt-3">
        <ByRatingTable run={run} />
      </div>
      {expanded && <div className="mt-3"><RunDetail runId={run.run_id} /></div>}
    </li>
  );
}

export function BacktestRunsCard() {
  const { state, refresh, refreshing } = useSectionData<BacktestListPayload>(
    "/api/backtest"
  );
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const data = state.status === "ready" ? state.data : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backtest</CardTitle>
        <CardDescription>
          {
            "GET /api/backtest — tổng kết các lần sweep backtest: quyết định quá khứ được chấm điểm theo kết quả giá sau đó (hit rate và alpha trung bình)."
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
      <CardContent className="space-y-3">
        {state.status === "loading" && <SectionSkeleton rows={3} />}
        {state.status === "error" && (
          <ErrorState message={state.error.message} onRetry={refresh} />
        )}
        {state.status === "ready" && state.data.runs.length === 0 && (
          <EmptyState
            title="Chưa có lần chạy backtest nào"
            hint="Chưa có sweep nào ghi kết quả vào results_dir/backtest. Chạy sweep từ CLI, ví dụ: tradingagents backtest NVDA,AAPL --start 2026-07-01 --end 2026-09-30."
          />
        )}
        {state.status === "ready" && state.data.runs.length > 0 && (
          <ul className="space-y-2">
            {state.data.runs.map((run) => (
              <RunRow
                key={run.run_id}
                run={run}
                expanded={openRunId === run.run_id}
                onToggle={() =>
                  setOpenRunId((current) =>
                    current === run.run_id ? null : run.run_id
                  )
                }
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
