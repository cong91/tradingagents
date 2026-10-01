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

const SIGNAL_LABELS: Record<string, string> = {
  Buy: "Buy (Mua)",
  Overweight: "Overweight (Thiên tăng)",
  Hold: "Hold (Giữ)",
  Underweight: "Underweight (Thiên giảm)",
  Sell: "Sell (Bán)",
  REVIEW: "REVIEW (Cần rà soát)",
};

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
  const complete = run.report_paths?.complete_report;
  const stateLog = run.report_paths?.state_log;
  if (!complete && !stateLog) {
    return (
      <p className="text-sm text-muted-foreground">
        Không có tệp báo cáo trên đĩa (chế độ Mock tổng hợp không ghi tệp).
      </p>
    );
  }
  return (
    <div>
      <p className="text-sm font-medium">Tệp kết quả</p>
      <ul className="mt-1 space-y-1 text-sm">
        {complete && (
          <li className="flex items-start gap-2">
            <FileTextIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              Báo cáo đầy đủ: <code className="break-all">{complete}</code>
            </span>
          </li>
        )}
        {stateLog && (
          <li className="flex items-start gap-2">
            <FileTextIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              State log: <code className="break-all">{stateLog}</code>
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
  const startedAt = formatClock(run.created_at);
  const finishedAt = run.finished_at ? formatClock(run.finished_at) : "—";

  if (run.status === "failed") {
    return (
      <div className="space-y-3">
        <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
          <CircleXIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-loss" />
          <div className="min-w-0">
            <p className="font-medium text-loss">Phiên thất bại</p>
            <p className="mt-0.5 break-words">
              {run.error?.message ?? "Lỗi không rõ nguyên nhân."}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Mã lỗi: {run.error?.code ?? "internal_error"} — kiểm tra log server rồi chạy phiên mới.
            </p>
          </div>
        </div>
        <Button variant="outline" className="min-touch" onClick={onNewRun}>
          <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
          Chạy phiên mới
        </Button>
      </div>
    );
  }

  if (run.status === "cancelled") {
    return (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">Phiên đã bị huỷ.</p>
        <Button variant="outline" className="min-touch" onClick={onNewRun}>
          <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
          Chạy phiên mới
        </Button>
      </div>
    );
  }

  const tone = signalTone(run.signal, run.is_review);
  const signalLabel =
    run.signal !== null ? (SIGNAL_LABELS[run.signal] ?? run.signal) : "Không xác định";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-muted-foreground">Tín hiệu cuối:</span>
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
          REVIEW là tín hiệu không giao dịch được: quyết định không đọc được rating —
          hãy rà soát thủ công hoặc chạy lại phiên.
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        Bắt đầu {startedAt} · Kết thúc {finishedAt}
      </p>
      <ReportPaths run={run} />
      <Button variant="outline" className="min-touch" onClick={onNewRun}>
        <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
        Chạy phiên mới
      </Button>
    </div>
  );
}
