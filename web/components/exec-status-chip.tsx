"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircleIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "cn";
import { useT } from "@/lib/i18n";

export type ModeState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; enabled: boolean };

type SettingsPayload = {
  gates_readonly?: {
    auto_spot?: unknown;
    auto_futures?: unknown;
  } | null;
};

type ChipTone = "enabled" | "disabled" | "loading" | "error";

const TONE_CLASS: Record<ChipTone, string> = {
  enabled: "border-amber-500/60 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
  disabled: "border-border bg-muted text-foreground",
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
    state.status === "ready" ? (state.enabled ? "enabled" : "disabled") : state.status
  ];
  const label = (() => {
    const c = t.common.execStatus;
    switch (state.status) {
      case "ready":
        return state.enabled ? c.chipEnabled : c.chipDisabled;
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
        href="/settings#operator-config"
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
          tone
        )}
      >
        {state.status === "loading" && (
          <LoaderCircleIcon className="size-3 shrink-0 animate-spin" aria-hidden="true" />
        )}
        {/* Chấm trạng thái là trang trí: aria-hidden để screen reader không đọc "bullet",
            chữ nhãn tự là tên truy cập của link. */}
        <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" />
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

// The chip reflects automation modes, not the configured venue or account profile.
// Settings events trigger a refetch; visibility and network events are fallbacks.
export function ExecStatusChip() {
  const [state, setState] = useState<ModeState>({ status: "loading" });
  const lastFetchAt = useRef(0);
  const fetchSequence = useRef(0);

  // Thuần: fetch rồi trả trạng thái; setState chỉ chạy trong callback promise.
  const fetchMode = useCallback(async (): Promise<ModeState> => {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as SettingsPayload | null;
      const gates = data?.gates_readonly;
      if (gates?.auto_spot === true || gates?.auto_futures === true) {
        return { status: "ready", enabled: true };
      }
      if (gates?.auto_spot === false && gates?.auto_futures === false) {
        return { status: "ready", enabled: false };
      }
      return { status: "error" };
    } catch {
      return { status: "error" };
    }
  }, []);

  const refresh = useCallback(() => {
    lastFetchAt.current = Date.now();
    const sequence = ++fetchSequence.current;
    void fetchMode().then((next) => {
      if (sequence === fetchSequence.current) setState(next);
    });
  }, [fetchMode]);

  // Kết nối SSE luôn phát "reset" lúc mở — bỏ qua nếu vừa fetch để mỗi lần
  // load trang chỉ t đúng một request.
  const refreshIfQuiet = useCallback(() => {
    if (Date.now() - lastFetchAt.current >= 2_000) refresh();
  }, [refresh]);

  useEffect(() => {
    refresh();
    if (typeof EventSource === "undefined") return;
    const source = new EventSource("/api/settings/events");
    source.addEventListener("reset", refreshIfQuiet);
    source.addEventListener("changed", refresh);
    return () => source.close();
  }, [refresh, refreshIfQuiet]);

  useEffect(() => {
    const onWake = () => {
      if (document.visibilityState === "visible") refreshIfQuiet();
    };
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("online", onWake);
    return () => {
      document.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("online", onWake);
    };
  }, [refreshIfQuiet]);

  return <ExecStatusChipView state={state} onRetry={refresh} />;
}
