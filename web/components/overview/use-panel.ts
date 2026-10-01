"use client";

// Tầng poll dữ liệu dùng chung các panel của màn M1 (hợp đồng §0.2):
//   loading — chưa có gì; ready — có dữ liệu tươi; stale — có dữ liệu cũ kèm
//   lỗi poll gần nhất (không xoá UI khi poll lỗi); error — chưa từng có dữ liệu.
// Tách khỏi dashboard.tsx để từng card (hiệu suất, audit, danh mục) chia sẻ
// đúng một định nghĩa trạng thái — cùng pattern use-section-data.ts của audit/.

import { useCallback, useEffect, useState } from "react";

import { FetchError } from "./contract";

export type PanelState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T; fetchedAt: number }
  | { status: "stale"; data: T; fetchedAt: number; message: string; code: string }
  | { status: "error"; message: string; code: string };

/** Panel đã có dữ liệu (ready hoặc stale) — đầu vào của các section render. */
export type ReadyPanel<T> = Extract<PanelState<T>, { status: "ready" | "stale" }>;

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  staleAfterMs: number,
  clock: number
): { state: PanelState<T>; refresh: () => void; stale: boolean } {
  const [state, setState] = useState<PanelState<T>>({ status: "loading" });

  const load = useCallback(() => {
    fetcher().then(
      (data) => setState({ status: "ready", data, fetchedAt: Date.now() }),
      (err: unknown) => {
        const message = err instanceof Error ? err.message : "Lỗi không xác định";
        const code = err instanceof FetchError ? err.code : "unknown_error";
        setState((prev) => {
          if (prev.status === "ready" || prev.status === "stale") {
            return {
              status: "stale",
              data: prev.data,
              fetchedAt: prev.fetchedAt,
              message,
              code,
            };
          }
          return { status: "error", message, code };
        });
      }
    );
  }, [fetcher]);

  useEffect(() => {
    load();
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
  }, [load, intervalMs]);

  const stale =
    (state.status === "ready" || state.status === "stale") &&
    clock - state.fetchedAt > staleAfterMs;

  return { state, refresh: load, stale };
}
