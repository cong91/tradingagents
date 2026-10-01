"use client";

// Nguồn dữ liệu màn Duyệt lệnh: GET /api/approvals (auto-poll 15s theo hợp đồng
// §4 — "đây là màn approval gate — phải auto-poll") + hai hành động
// POST /api/approvals/{id}/approve và /reject (CSRF §0: Content-Type json).
// Khi một dialog hành động đang mở, caller set paused=true để poll không ghi
// đè UI khi người dùng đang tương tác (quy tắc stale §0.2).
//
// Lưu ý cấu trúc: mọi setState trong luồng poll nằm trong callback của promise
// (.then/.catch/.finally) — effect chỉ lên lịch, không chạy setState đồng bộ
// (react-hooks/set-state-in-effect), cùng pattern với components/mode-bar.tsx.

import { useCallback, useEffect, useRef, useState } from "react";

import { parseErrorResponse } from "@/lib/api";

import type {
  ApprovalItem,
  ApprovalsResponse,
  ApproveResponse,
} from "./types";

const POLL_INTERVAL_MS = 15_000;
const LIMIT = 50;

export type QueueSnapshot = {
  pending: ApprovalItem[];
  resolved: ApprovalItem[]; // đã sort: mới giải quyết nhất trước
  generated_at: string;
  fetchedAt: number;
};

export type ActionResult<T> =
  | { ok: true; payload: T }
  | { ok: false; code: string; message: string; details: Record<string, unknown> };

export type LastAction =
  | {
      kind: "approved";
      id: string;
      mode: string | null;
      orderId: string | null;
      warning: string | null;
    }
  | { kind: "rejected"; id: string };

async function readApiError(
  res: Response
): Promise<{ code: string; message: string; details: Record<string, unknown> }> {
  // Parse envelope §0.1/{detail} ở lib/api.ts — chung cho cả 6 màn.
  const body = await parseErrorResponse(res);
  return {
    code: body.code ?? `http_${res.status}`,
    message: body.message,
    details: body.details,
  };
}

function byResolvedDesc(a: ApprovalItem, b: ApprovalItem): number {
  const av = a.resolved_at ?? a.created_at;
  const bv = b.resolved_at ?? b.created_at;
  return bv.localeCompare(av);
}

async function fetchQueue(): Promise<QueueSnapshot> {
  const res = await fetch(`/api/approvals?limit=${LIMIT}`, { cache: "no-store" });
  if (!res.ok) {
    const err = await readApiError(res);
    throw new Error(err.message || `HTTP ${res.status}`);
  }
  const data = (await res.json()) as ApprovalsResponse;
  if (!data || !Array.isArray(data.items)) {
    throw new Error("Phản hồi không đúng định dạng hợp đồng (thiếu items).");
  }
  const items = data.items.filter(
    (item): item is ApprovalItem => Boolean(item) && typeof item.id === "string"
  );
  return {
    pending: items.filter((item) => item.status === "pending"),
    resolved: items.filter((item) => item.status !== "pending").sort(byResolvedDesc),
    generated_at: typeof data.generated_at === "string" ? data.generated_at : "",
    fetchedAt: Date.now(),
  };
}

export function useApprovals({ paused }: { paused: boolean }) {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [lastAction, setLastAction] = useState<LastAction | null>(null);

  const inFlight = useRef(false);
  const hasData = useRef(false);

  const runRefresh = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    void fetchQueue()
      .then((next) => {
        hasData.current = true;
        setSnapshot(next);
        setLoadError(null);
        setStale(false);
      })
      .catch((exc: unknown) => {
        const message = exc instanceof Error ? exc.message : "Lỗi không xác định";
        // Đã có dữ liệu → giữ nguyên UI và đánh dấu "cũ" (quy tắc stale §0.2);
        // chưa có dữ liệu → hiển thị error-state với nút thử lại.
        if (hasData.current) setStale(true);
        else setLoadError(message);
      })
      .finally(() => {
        inFlight.current = false;
        setRefreshing(false);
        setInitialLoading(false);
      });
  }, []);

  // Pause đóng interval; khi mở lại (hoặc lần đầu mount), fetch ngay một lần.
  useEffect(() => {
    runRefresh();
    if (paused) return;
    const id = setInterval(runRefresh, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [paused, runRefresh]);

  // Làm mới từ UI: spinner bật ngay trong event handler (setState sync trong
  // event được phép; effect không bao giờ đi qua đây).
  const manualRefresh = useCallback(() => {
    setRefreshing(true);
    runRefresh();
  }, [runRefresh]);

  const postAction = useCallback(
    <T,>(
      path: string,
      body: Record<string, unknown>,
      onSuccess: (payload: T) => void
    ): Promise<ActionResult<T>> => {
      return new Promise((resolve) => {
        void fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" }, // quy tắc CSRF §0
          body: JSON.stringify(body),
        })
          .then(async (res) => {
            if (!res.ok) {
              const err = await readApiError(res);
              // Item đã không còn pending nơi khác → làm mới để UI phản ánh đúng.
              if (err.code === "conflict" || err.code === "expired") runRefresh();
              resolve({ ok: false, code: err.code, message: err.message, details: err.details });
              return;
            }
            const payload = (await res.json()) as T;
            onSuccess(payload);
            runRefresh();
            resolve({ ok: true, payload });
          })
          .catch(() => {
            resolve({
              ok: false,
              code: "network",
              message: "Không kết nối được backend — kiểm tra server FastAPI đang chạy.",
              details: {},
            });
          });
      });
    },
    [runRefresh]
  );

  const approve = useCallback(
    (id: string): Promise<ActionResult<ApproveResponse>> =>
      postAction<ApproveResponse>(
        `/api/approvals/${encodeURIComponent(id)}/approve`,
        {},
        (payload) =>
          setLastAction({
            kind: "approved",
            id,
            mode: payload.mode ?? null,
            orderId: payload.order_id ?? null,
            warning: typeof payload.warning === "string" ? payload.warning : null,
          })
      ),
    [postAction]
  );

  const reject = useCallback(
    (id: string, reason: string | null): Promise<ActionResult<ApprovalItem>> =>
      postAction<ApprovalItem>(
        `/api/approvals/${encodeURIComponent(id)}/reject`,
        { reason },
        () => setLastAction({ kind: "rejected", id })
      ),
    [postAction]
  );

  return {
    snapshot,
    initialLoading,
    loadError,
    stale,
    refreshing,
    refresh: manualRefresh,
    approve,
    reject,
    lastAction,
    clearLastAction: () => setLastAction(null),
  };
}
