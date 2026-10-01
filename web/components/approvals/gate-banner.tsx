"use client";

// Banner cổng duyệt: đọc GET /api/health mỗi 15s (hợp đồng §8 — stale 15s +
// auto-poll). Hợp đồng §6 bắt buộc màn approval gate hiển thị banner đỏ khi
// halted_today=true; v1 không có endpoint reset halt (risk §10.8) nên banner
// không kèm nút reset — đúng chủ đích thiết kế.

import { useCallback, useEffect, useState } from "react";
import { ShieldAlertIcon, TriangleAlertIcon } from "lucide-react";

import type { HealthResponse } from "./types";

const POLL_INTERVAL_MS = 15_000;

type GateState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; haltedToday: boolean; execMode: "live" | "dry" | null };

export function useGateHealth() {
  const [state, setState] = useState<GateState>({ status: "loading" });

  const fetchHealth = useCallback(async (): Promise<GateState> => {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as HealthResponse;
      if (typeof data.halted_today !== "boolean") {
        throw new Error("Phản hồi /api/health thiếu halted_today");
      }
      return {
        status: "ready",
        haltedToday: data.halted_today,
        execMode:
          data.exec_mode === "live"
            ? "live"
            : data.exec_mode === "dry"
              ? "dry"
              : null,
      };
    } catch {
      return { status: "error" };
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const run = () => {
      void fetchHealth().then((next) => {
        if (!cancelled) setState(next);
      });
    };
    run();
    const id = setInterval(run, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [fetchHealth]);

  return state;
}

export function GateBanner({ gate }: { gate: GateState }) {
  if (gate.status === "ready" && !gate.haltedToday) return null;

  if (gate.status === "ready" && gate.haltedToday) {
    return (
      <div
        role="alert"
        className="mb-4 flex items-start gap-3 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
      >
        <ShieldAlertIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        <div>
          <p className="font-semibold">RiskGuard đã dừng giao dịch hôm nay</p>
          <p>
            Mọi phê duyệt sẽ bị server từ chối (403 risk_halted). Việc mở lại
            halt chỉ do operator thực hiện phía server — màn này cố ý không có
            nút reset.
          </p>
        </div>
      </div>
    );
  }

  if (gate.status === "error") {
    return (
      <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
        <TriangleAlertIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        <p>
          Không đọc được GET /api/health — chưa kiểm tra được trạng thái halt
          của RiskGuard.
        </p>
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="mb-4 h-[3.25rem] animate-pulse rounded-lg bg-muted"
    />
  );
}
