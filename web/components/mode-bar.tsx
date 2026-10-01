"use client";

import { useCallback, useEffect, useState } from "react";
import {
  LoaderCircleIcon,
  ShieldCheckIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "cn";

type ModeState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; live: boolean };

type SettingsPayload = {
  effective_exec_mode?: unknown;
};

// Contract §1: settings stale sau 30s — mode bar tự refresh cùng chu kỳ.
const REFRESH_INTERVAL_MS = 30_000;

export function ModeBar() {
  // Trạng thái ban đầu là "loading"; setState chỉ chạy trong callback của
  // promise (sau khi fetch trả về), không chạy đồng bộ trong effect.
  const [state, setState] = useState<ModeState>({ status: "loading" });

  // Hàm thuần: chỉ fetch và trả về trạng thái mới, không đụng setState.
  const fetchMode = useCallback(async (): Promise<ModeState> => {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as SettingsPayload;
      // `effective_exec_mode` chỉ là "live" khi CẢ HAI cổng FR5 đang mở
      // (exec_live + TRADINGAGENTS_EXEC_LIVE — docs/ui-api-contract.md §1).
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

  const tone = (() => {
    switch (state.status) {
      case "ready":
        return state.live
          ? {
              bar: "border-b-red-600 bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-100",
              icon: (
                <TriangleAlertIcon
                  className="size-5 shrink-0"
                  aria-hidden="true"
                />
              ),
              title: "LIVE — LỆNH THẬT",
              subtitle:
                "Cả hai cổng thực thi đang mở: lệnh được duyệt sẽ gửi thẳng lên sàn.",
            }
          : {
              bar: "border-b-blue-600 bg-blue-50 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
              icon: (
                <ShieldCheckIcon
                  className="size-5 shrink-0"
                  aria-hidden="true"
                />
              ),
              title: "SANDBOX",
              subtitle: "Chế độ mô phỏng — không có lệnh thật nào được gửi.",
            };
      case "loading":
        return {
          bar: "border-b-muted-foreground/40 bg-muted text-foreground",
          icon: (
            <LoaderCircleIcon
              className="size-5 shrink-0 animate-spin"
              aria-hidden="true"
            />
          ),
          title: "ĐANG TẢI CHẾ ĐỘ…",
          subtitle: "Đang đọc GET /api/settings.",
        };
      case "error":
        return {
          bar: "border-b-amber-500 bg-amber-50 text-amber-900 dark:bg-amber-950 dark:text-amber-100",
          icon: (
            <TriangleAlertIcon className="size-5 shrink-0" aria-hidden="true" />
          ),
          title: "KHÔNG KẾT NỐI",
          subtitle: "Không đọc được GET /api/settings — kiểm tra backend.",
        };
    }
  })();

  return (
    <div
      className={cn(
        "sticky top-0 z-40 flex min-h-11 items-center gap-3 border-b-4 px-4 py-2",
        tone.bar
      )}
    >
      {tone.icon}
      <div role="status" aria-live="polite" className="min-w-0">
        <p className="text-sm font-bold tracking-wide">{tone.title}</p>
        <p className="truncate text-xs">{tone.subtitle}</p>
      </div>
      {state.status === "error" && (
        <Button
          variant="outline"
          size="sm"
          className="min-touch ml-auto"
          onClick={refresh}
        >
          Thử lại
        </Button>
      )}
    </div>
  );
}
