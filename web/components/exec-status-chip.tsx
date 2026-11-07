"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { LoaderCircleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "cn";
import { useT } from "@/lib/i18n";

export type ModeState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; live: boolean };

type SettingsPayload = {
  effective_exec_mode?: unknown;
};

// Settings stale sau 30s — chip tự refresh cùng chu kỳ (cơ chế cũ của ModeBar).
const REFRESH_INTERVAL_MS = 30_000;

type ChipTone = "live" | "dry" | "loading" | "error";

const TONE_CLASS: Record<ChipTone, string> = {
  live: "border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100",
  dry: "border-blue-300 bg-blue-50 text-blue-900 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-100",
  loading: "border-border bg-muted text-foreground",
  error:
    "border-amber-500/60 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
};

// Vùng hiển thị thuần (state vào — chip ra); tách khỏi container để test
// render được cả 4 trạng thái mà không cần fetch.
export function ExecStatusChipView({
  state,
  onRetry,
}: {
  state: ModeState;
  onRetry?: () => void;
}) {
  const t = useT();
  const tone = TONE_CLASS[
    state.status === "ready" ? (state.live ? "live" : "dry") : state.status
  ];
  const label = (() => {
    const c = t.common.execStatus;
    switch (state.status) {
      case "ready":
        return state.live ? c.chipLive : c.chipSafe;
      case "loading":
        return c.chipChecking;
      case "error":
        return c.chipUnknown;
    }
  })();

  return (
    // role="status" trên vùng bọc (không trên Link) để giữ vai trò link và
    // vẫn thông báo thay đổi trạng thái; chữ mô tả trạng thái nên màu không
    // bao giờ là tín hiệu duy nhất (WCAG 1.4.1).
    <div role="status" aria-live="polite" className="flex flex-wrap items-center gap-1.5">
      <Link
        href="/settings#exec-gates"
        aria-label={label}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
          tone
        )}
      >
        {state.status === "loading" && (
          <LoaderCircleIcon className="size-3 shrink-0 animate-spin" aria-hidden="true" />
        )}
        <span className="truncate">{label}</span>
      </Link>
      {state.status === "error" && (
        <Button variant="ghost" size="sm" className="min-touch px-2 text-xs" onClick={onRetry}>
          {t.common.retry}
        </Button>
      )}
    </div>
  );
}

// Chip trạng thái cổng thực thi, luôn hiển thị trong sidebar (thay banner
// full-width). Nhãn chỉ đo cổng «Lệnh thật»: effective_exec_mode là "live" khi
// CẢ HAI nửa mở (exec_live + TRADINGAGENTS_EXEC_LIVE) — không đo exec_auto_confirm.
export function ExecStatusChip() {
  const [state, setState] = useState<ModeState>({ status: "loading" });

  // Thuần: fetch rồi trả trạng thái; setState chỉ chạy trong callback promise.
  const fetchMode = useCallback(async (): Promise<ModeState> => {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as SettingsPayload;
      return { status: "ready", live: data.effective_exec_mode === "live" };
    } catch {
      return { status: "error" };
    }
  }, []);

  const refresh = useCallback(() => {
    void fetchMode().then(setState);
  }, [fetchMode]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return <ExecStatusChipView state={state} onRetry={refresh} />;
}
