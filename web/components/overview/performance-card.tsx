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
import { useT } from "@/lib/i18n";

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
  const t = useT();
  const c = t.overview.performance;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{c.title}</CardTitle>
        <CardDescription>{c.description}</CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" className="min-touch" onClick={() => {
            onPortfolioRetry();
            onHistoryRetry();
            onRetry();
          }}>
            <RefreshCwIcon aria-hidden="true" /> {t.common.refresh}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        <PerformancePositions portfolioState={portfolioState} stale={portfolioStale} futuresStale={futuresStale} onRetry={onPortfolioRetry} />

        <section className="space-y-2 border-t pt-4" aria-label={c.closedStatsTitle}>
          <h3 className="text-sm font-medium">{c.closedStatsTitle}</h3>
          <p className="text-sm text-muted-foreground">
            {c.closedStatsBody}
          </p>
        </section>

        <section className="border-t pt-4">
          {historyState.status === "loading" ? (
            <p role="status" className="text-sm text-muted-foreground">{c.historyLoading}</p>
          ) : historyState.status === "error" ? (
            <div role="alert" className="space-y-2 text-sm">
              <p className="text-destructive">{c.historyError(historyState.code, historyState.message)}</p>
              <Button variant="outline" size="sm" className="min-touch" onClick={onHistoryRetry}>{t.common.retry}</Button>
            </div>
          ) : (
            <>
              {historyState.status === "stale" || historyStale ? (
                <p role="alert" className="mb-3 flex items-center gap-2 text-sm text-destructive">
                  <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
                  {c.historyStale(formatDateTime(historyState.data.observed_at))}
                </p>
              ) : null}
              <PerformanceHistory history={historyState.data} />
            </>
          )}
        </section>

        <section className="border-t pt-4">
          {state.status === "loading" ? (
            <p role="status" className="text-sm text-muted-foreground">{c.eventsLoading}</p>
          ) : state.status === "error" ? (
            <div role="alert" className="space-y-2 text-sm">
              <p className="text-destructive">{c.eventsError(state.code, state.message)}</p>
              <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>{t.common.retry}</Button>
            </div>
          ) : (
            <>
              {state.status === "stale" || stale ? <Badge variant="outline">{c.eventsStale(formatDateTime(state.data.generated_at))}</Badge> : null}
              <PerformanceEvents events={state.data.recent_orders} skippedLines={state.data.skipped_lines ?? null} />
            </>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
