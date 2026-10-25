"use client";

// Tầng dữ liệu lịch sử job cho màn Scanner (contract §5 rev 7). Tách khỏi
// component để panel lịch sử và panel quét dùng chung đúng một nguồn: nhờ đó
// màn hình biết ngay "đang có job chạy" khi vừa mở, thay vì chỉ biết khi bấm
// chạy và bị từ chối 409.

import { useCallback, useEffect, useState } from "react";
import { getMessages } from "@/lib/i18n/messages";

import { scannerApi } from "./scanner-api";
import type { DailyJobSummary } from "./scanner-types";

const HISTORY_POLL_MS = 10_000;

export interface JobHistoryState {
  jobs: DailyJobSummary[];
  /** Job đang chiếm worker theo backend; null khi rảnh. */
  activeJobId: string | null;
  loading: boolean;
  /** Lỗi lần tải gần nhất; dữ liệu cũ vẫn được giữ để không nhấp nháy UI. */
  error: string | null;
  refresh: () => void;
}

export function useJobHistory(): JobHistoryState {
  const [jobs, setJobs] = useState<DailyJobSummary[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Shaped like overview/use-panel.ts's loader: the promise callbacks hold the
  // setState calls, so nothing writes state synchronously inside the effect.
  const load = useCallback(() => {
    scannerApi
      .dailyJobs()
      .then(
        (body) => {
          setJobs(body.items);
          setActiveJobId(body.active_job_id);
          setError(null);
        },
        (err: unknown) => {
          setError(err instanceof Error ? err.message : getMessages().scanner.history.loadFailed);
        }
      )
      .finally(() => setLoading(false));
  }, []);

  // Stable identity on purpose: callers put this in effect dependency arrays,
  // and a fresh closure per render would restart their polls every render.
  const refresh = useCallback(() => load(), [load]);

  useEffect(() => {
    load();
    const id = setInterval(load, HISTORY_POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return { jobs, activeJobId, loading, error, refresh };
}
