"use client";

// Tầng poll dữ liệu dùng chung các panel của màn M1 (hợp đồng §0.2):
//   loading — chưa có gì; ready — có dữ liệu tươi; stale — có dữ liệu cũ kèm
//   lỗi poll gần nhất (không xoá UI khi poll lỗi); error — chưa từng có dữ liệu.
// Tách khỏi dashboard.tsx để từng card (hiệu suất, audit, danh mục) chia sẻ
// đúng một định nghĩa trạng thái — cùng pattern use-section-data.ts của audit/.

import { useCallback, useEffect, useRef, useState } from "react";

import { FetchError } from "./contract";

const MAX_TIMESTAMP_SKEW_MS = 5_000;

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
  clock: number,
  suppressOverlap = false,
  getDataTimestamp?: (data: T) => string | number | null | undefined
): { state: PanelState<T>; refresh: () => void; stale: boolean } {
  const [state, setState] = useState<PanelState<T>>({ status: "loading" });
  const requestSequence = useRef(0);
  const inFlightRequest = useRef<object | null>(null);
  const refreshQueued = useRef(false);
  const active = useRef(false);

  const load = useCallback(function load(): void {
    if (!active.current) return;
    if (suppressOverlap && inFlightRequest.current !== null) {
      refreshQueued.current = true;
      return;
    }

    const sequence = ++requestSequence.current;
    const request = {};
    if (suppressOverlap) inFlightRequest.current = request;
    const clearInFlight = () => {
      if (inFlightRequest.current === request) inFlightRequest.current = null;
    };

    let pending: Promise<T>;
    try {
      pending = fetcher();
    } catch (err) {
      clearInFlight();
      throw err;
    }

    pending.then(
      (data) => {
        if (!active.current || sequence !== requestSequence.current) return;
        setState({ status: "ready", data, fetchedAt: Date.now() });
      },
      (err: unknown) => {
        if (!active.current || sequence !== requestSequence.current) return;
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
    ).finally(() => {
      clearInFlight();
      if (active.current && refreshQueued.current) {
        refreshQueued.current = false;
        load();
      }
    });
  }, [fetcher, suppressOverlap]);

  useEffect(() => {
    active.current = true;
    load();
    const id = setInterval(load, intervalMs);
    return () => {
      active.current = false;
      requestSequence.current += 1;
      inFlightRequest.current = null;
      refreshQueued.current = false;
      clearInterval(id);
    };
  }, [load, intervalMs]);

  const stale =
    (state.status === "ready" || state.status === "stale") &&
    (getDataTimestamp
      ? isTimestampStale(getDataTimestamp(state.data), clock, staleAfterMs)
      : clock - state.fetchedAt > staleAfterMs);

  return { state, refresh: load, stale };
}

function isTimestampStale(
  timestamp: string | number | null | undefined,
  clock: number,
  staleAfterMs: number
): boolean {
  if (timestamp == null) return true;
  const timestampMs = typeof timestamp === "number" ? timestamp : Date.parse(timestamp);
  const ageMs = clock - timestampMs;
  return (
    !Number.isFinite(timestampMs) ||
    ageMs < -MAX_TIMESTAMP_SKEW_MS ||
    ageMs > staleAfterMs
  );
}
