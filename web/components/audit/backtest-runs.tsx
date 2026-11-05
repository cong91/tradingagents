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
import { useT } from "@/lib/i18n";

const STALE_SECONDS = 60;

function ByRatingTable({ run }: { run: BacktestRun }) {
  const t = useT();
  const c = t.audit.backtest;
  const ratings = Object.entries(run.by_rating);
  if (ratings.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        {c.noRatings}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <caption className="sr-only">
          {c.caption(run.run_id)}
        </caption>
        <thead>
          <tr className="border-b bg-muted/50 text-left text-xs text-muted-foreground">
            <th scope="col" className="px-3 py-2 font-medium">{c.colRating}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{c.colCount}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{c.colHitRate}</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">{c.colAlpha}</th>
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
  const t = useT();
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
          {t.common.warning(data.warning)}
        </p>
      )}
      {data.entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t.audit.backtest.noEntries}
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
  const t = useT();
  const c = t.audit.backtest;
  return (
    <li className="rounded-xl border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold">{run.run_id}</span>
        <Badge variant="secondary">{c.entries(run.entries)}</Badge>
        <Badge variant="outline">{c.resolved(run.resolved)}</Badge>
        {run.pending > 0 && <Badge variant="outline">{c.pending(run.pending)}</Badge>}
        {run.unscored > 0 && <Badge variant="outline">{c.unscored(run.unscored)}</Badge>}
        <Button
          variant="outline"
          size="sm"
          className="ml-auto min-touch"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? (
            <>
              {c.hideDetails}
              <ChevronUpIcon aria-hidden="true" />
            </>
          ) : (
            <>
              {c.showDetails}
              <ChevronDownIcon aria-hidden="true" />
            </>
          )}
        </Button>
      </div>
      <p className="mt-1 text-xs text-muted-foreground">
        {run.holding ? c.avgHolding(run.holding) : ""}
        Log: <span className="font-mono">{run.log_path}</span>
      </p>
      {run.warning && (
        <p className="mt-1 text-xs text-amber-900 dark:text-amber-100">
          {t.common.warning(run.warning)}
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
  const t = useT();
  const c = t.audit.backtest;
  const data = state.status === "ready" ? state.data : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{c.title}</CardTitle>
        <CardDescription>{c.description}</CardDescription>
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
            title={c.emptyTitle}
            hint={c.emptyHint}

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
