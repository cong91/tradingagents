"use client";

// Các trạng thái hạng nhất dùng chung cho các section của màn M6:
// skeleton (loading), empty-state (nêu việc cần làm tiếp), error (kèm retry),
// và chỉ báo stale theo `generated_at` (§0.2 hợp đồng).

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  ClockIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "cn";
import { formatDateTime } from "./display";

export function SectionSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Đang tải dữ liệu" className="space-y-2">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="h-10 animate-pulse rounded-lg bg-muted"
          aria-hidden="true"
        />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  actionHref,
  actionLabel,
}: {
  title: string;
  hint: string;
  actionHref?: string;
  actionLabel?: string;
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      <p className="max-w-md text-sm text-muted-foreground">{hint}</p>
      {actionHref && actionLabel && (
        <Link
          href={actionHref}
          className={cn(buttonVariants({ variant: "outline" }), "mt-2 min-touch")}
        >
          {actionLabel}
        </Link>
      )}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-6"
    >
      <div className="flex items-start gap-2">
        <TriangleAlertIcon
          className="mt-0.5 size-4 shrink-0 text-destructive"
          aria-hidden="true"
        />
        <div>
          <p className="text-sm font-medium">Không tải được dữ liệu</p>
          <p className="mt-1 text-sm text-muted-foreground">{message}</p>
        </div>
      </div>
      <Button variant="outline" className="min-touch" onClick={onRetry}>
        <RefreshCwIcon aria-hidden="true" />
        Thử lại
      </Button>
    </div>
  );
}

function useTickingNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Dòng meta của section: badge "cũ" khi quá hạn + thời điểm đọc + nút làm mới. */
export function DataMeta({
  generatedAt,
  staleAfterSeconds,
  onRefresh,
  refreshing,
}: {
  generatedAt: string | null;
  staleAfterSeconds: number;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const now = useTickingNow(5_000);
  const generatedMs = generatedAt ? Date.parse(generatedAt) : Number.NaN;
  const stale =
    Number.isFinite(generatedMs) && now - generatedMs > staleAfterSeconds * 1000;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {stale && (
        <span className="inline-flex items-center gap-1 rounded-4xl border border-amber-500/60 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          <ClockIcon className="size-3" aria-hidden="true" />
          Dữ liệu cũ — hãy làm mới
        </span>
      )}
      <span className="text-xs text-muted-foreground">
        Đọc lúc {generatedAt ? formatDateTime(generatedAt) : "—"}
      </span>
      <Button
        variant="ghost"
        size="sm"
        className="min-touch"
        onClick={onRefresh}
        disabled={refreshing}
      >
        <RefreshCwIcon
          className={cn(refreshing && "animate-spin")}
          aria-hidden="true"
        />
        Làm mới
      </Button>
    </div>
  );
}
