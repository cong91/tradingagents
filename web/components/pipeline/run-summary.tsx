"use client";

// Kết quả phiên: tín hiệu 5-tier + REVIEW (tradingagents/agents/rating.py:23-31)
// và tệp báo cáo. Màu tín hiệu luôn đi kèm nhãn chữ (không bao giờ là tín hiệu duy nhất).

import {
  CircleCheckIcon,
  CircleXIcon,
  FileTextIcon,
  RotateCcwIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "cn";
import { formatClock } from "./format";
import type { RunPayload } from "./types";
import { useT } from "@/lib/i18n";

type SignalTone = "bull" | "bear" | "hold" | "review";

function signalTone(signal: string | null, isReview: boolean): SignalTone {
  if (isReview || signal === "REVIEW") return "review";
  if (signal === "Buy" || signal === "Overweight") return "bull";
  if (signal === "Sell" || signal === "Underweight") return "bear";
  return "hold";
}

const TONE_BADGE_CLASS: Record<SignalTone, string> = {
  bull: "bg-profit/10 text-profit ring-1 ring-inset ring-profit/30",
  bear: "bg-loss/10 text-loss ring-1 ring-inset ring-loss/30",
  hold: "",
  review:
    "bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-500/40 dark:text-amber-400",
};

function ReportPaths({ run }: { run: RunPayload }) {
  const t = useT();
  const c = t.pipeline.summary;
  const complete = run.report_paths?.complete_report;
  const stateLog = run.report_paths?.state_log;
  if (!complete && !stateLog) {
    return (
      <p className="text-sm text-muted-foreground">
        {c.noReports}
      </p>
    );
  }
  return (
    <div>
      <p className="text-sm font-medium">{c.outputFiles}</p>
      <ul className="mt-1 space-y-1 text-sm">
        {complete && (
          <li className="flex items-start gap-2">
            <FileTextIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              {c.fullReport} <code className="break-all">{complete}</code>
            </span>
          </li>
        )}
        {stateLog && (
          <li className="flex items-start gap-2">
            <FileTextIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              {c.stateLog} <code className="break-all">{stateLog}</code>
            </span>
          </li>
        )}
      </ul>
    </div>
  );
}

export function RunSummary({
  run,
  onNewRun,
}: {
  run: RunPayload;
  onNewRun: () => void;
}) {
  const t = useT();
  const c = t.pipeline.summary;
  const startedAt = formatClock(run.created_at);
  const finishedAt = run.finished_at ? formatClock(run.finished_at) : "—";

  if (run.status === "failed") {
    return (
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
          <CircleXIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-loss" />
          <div className="min-w-0">
            <p className="font-medium text-loss">{c.failedTitle}</p>
            <p className="mt-0.5 break-words">
              {run.error?.message ?? c.failedUnknown}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {c.failedCode(run.error?.code ?? "internal_error")}
            </p>
          </div>
        </div>
        <Button variant="outline" className="min-touch" onClick={onNewRun}>
          <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
          {c.newRun}
        </Button>
      </div>
    );
  }

  if (run.status === "interrupted") {
    return (
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200">
          <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <div className="min-w-0">
            <p className="font-medium">{c.interruptedTitle}</p>
            <p className="mt-0.5 break-words">
              {run.error?.message ?? c.interruptedUnknown}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {c.interruptedCode(run.error?.code ?? "interrupted")}
            </p>
          </div>
        </div>
        <Button variant="outline" className="min-touch" onClick={onNewRun}>
          <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
          {c.newRun}
        </Button>
      </div>
    );
  }

  if (run.status === "cancelled") {
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">{c.cancelled}</p>
        <Button variant="outline" className="min-touch" onClick={onNewRun}>
          <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
          {c.newRun}
        </Button>
      </div>
    );
  }

  const tone = signalTone(run.signal, run.is_review);
  const signalLabel =
    run.signal !== null ? (c.signals[run.signal] ?? run.signal) : c.unknownSignal;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">{c.finalSignal}</span>
        <Badge variant="outline" className={cn("h-6 px-2 text-sm", TONE_BADGE_CLASS[tone])}>
          {tone === "review" ? (
            <TriangleAlertIcon aria-hidden="true" />
          ) : (
            <CircleCheckIcon aria-hidden="true" />
          )}
          {signalLabel}
        </Badge>
      </div>
      {run.is_review && (
        <p className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200">
          <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          {c.reviewNote}
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        {c.times(startedAt, finishedAt)}

      </p>
      <ReportPaths run={run} />
      <Button variant="outline" className="min-touch" onClick={onNewRun}>
        <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
        {c.newRun}
      </Button>
    </div>
  );
}
