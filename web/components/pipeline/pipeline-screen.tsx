"use client";

// Compose màn M2 Agent Pipeline: form chạy phiên + thẻ phiên (meta, tracker,
// kết quả) + thẻ luồng sự kiện. Mỗi trạng thái (loading/empty/error/stale)
// đều có hiển thị riêng theo hợp đồng §0.2 và §3.

import { LoaderCircleIcon, RadioTowerIcon, TriangleAlertIcon } from "lucide-react";
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
import { cn } from "cn";
import { EventFeed } from "./event-feed";
import { RunForm } from "./run-form";
import { RunSummary } from "./run-summary";
import { StageTracker } from "./stage-tracker";
import { useRunStream } from "./use-run-stream";
import type { RunPayload } from "./types";
import { useT } from "@/lib/i18n";

function RunMeta({ run }: { run: RunPayload }) {
  const t = useT();
  const p = t.pipeline;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <span>
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{run.run_id}</code>
      </span>
      <span className="font-medium">{run.ticker}</span>
      <span className="text-muted-foreground">{run.trade_date}</span>
      <span className="text-muted-foreground">
        {p.assets[run.asset_type] ?? run.asset_type}
      </span>
      <Badge variant={run.mode === "mock" ? "outline" : "secondary"}>
        {run.mode === "mock" ? p.modeMock : p.modeLive}
      </Badge>
          <Badge
        variant="outline"
        aria-live="polite"
        className={cn(
          run.status === "failed" && "border-loss/40 text-loss",
          run.status === "interrupted" && "border-amber-500/40 text-amber-700 dark:text-amber-400",
          run.status === "completed" && "border-profit/40 text-profit",
        )}
      >
        {p.status[run.status] ?? run.status}
      </Badge>
      {run.analysts.length > 0 && (
        <span className="text-muted-foreground">{p.analysts(run.analysts.join(", "))}</span>
      )}
    </div>
  );
}

function ErrorBox({
  message,
  activeRunId,
  onAttach,
}: {
  message: string;
  activeRunId?: string;
  onAttach: (runId: string) => void;
}) {
  const t = useT();
  return (
    <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
      <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-loss" />
      <div className="min-w-0">
        <p className="font-medium text-loss">{t.pipeline.startFailed}</p>
        <p className="mt-0.5 break-words">{message}</p>
        {activeRunId && (
          <Button
            variant="outline"
            size="sm"
            className="min-touch mt-2"
            onClick={() => onAttach(activeRunId)}
          >
            {t.pipeline.attach(activeRunId)}
          </Button>
        )}
      </div>
    </div>
  );
}

function StaleHint() {
  const t = useT();
  return (
    <p
      role="status"
      className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200"
    >
      <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span>{t.pipeline.staleHint}</span>
    </p>
  );
}

export function PipelineScreen() {
  const { state, startRun, attachRun, reset } = useRunStream();
  const t = useT();
  const p = t.pipeline;
  const { phase } = state;
  const busy = phase.phase === "starting" || phase.phase === "streaming";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{p.newRunTitle}</CardTitle>
          <CardDescription>{p.newRunDescription}</CardDescription>
          {busy && <CardAction><Badge variant="secondary">{p.running}</Badge></CardAction>}
        </CardHeader>
        <CardContent>
          <RunForm disabled={busy} onSubmit={startRun} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{p.runTitle}</CardTitle>
          <CardDescription>{p.runDescription}</CardDescription>
          {phase.phase === "streaming" && (
            <CardAction>
              {state.stale ? (
                <Badge className="bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-500/40 dark:text-amber-400">
                  <TriangleAlertIcon aria-hidden="true" />
                  {p.hanging}
                </Badge>
              ) : (
                <Badge
                  variant={state.sseConnected ? "default" : "secondary"}
                  className={state.sseConnected ? undefined : "animate-pulse"}
                >
                  <RadioTowerIcon aria-hidden="true" />
                  {state.sseConnected ? p.sseLive : p.reconnecting}
                </Badge>
              )}
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          {phase.phase === "idle" && (
            <div className="flex h-40 items-center justify-center rounded-lg border border-dashed px-4 text-center text-sm text-muted-foreground">
              {p.idle}
            </div>
          )}
          {phase.phase === "starting" && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
              {p.starting}
            </p>
          )}
          {(phase.phase === "streaming" ||
            phase.phase === "completed" ||
            phase.phase === "failed" ||
            phase.phase === "interrupted") &&
            state.run && (
              <>
                <RunMeta run={state.run} />
                {phase.phase === "streaming" && (
                  <>
                    {state.stale && <StaleHint />}
                    <StageTracker events={state.events} status={state.run.status} />
                  </>
                )}
                {(phase.phase === "completed" || phase.phase === "failed" || phase.phase === "interrupted") && (
                  <RunSummary run={state.run} onNewRun={reset} />
                )}
              </>
            )}
          {phase.phase === "error" && (
            <ErrorBox
              message={phase.message}
              activeRunId={phase.activeRunId}
              onAttach={attachRun}
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{p.feedTitle}</CardTitle>
          <CardDescription>{p.feedDescription}</CardDescription>
        </CardHeader>
        <CardContent>
          <EventFeed
            events={state.events}
            emptyText={
              phase.phase === "idle"
                ? p.feedEmptyIdle
                : p.feedEmptyWaiting

            }
          />
        </CardContent>
      </Card>
    </div>
  );
}
