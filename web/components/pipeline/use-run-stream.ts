"use client";

// Toàn bộ tương tác API của màn M2 (hợp đồng docs/ui-api-contract.md §3):
// - POST /api/runs → 201 payload; 409 conflict khi đã có run active (worker duy nhất).
// - GET /api/runs/{id}/events (SSE): EventSource tự reconnect và gửi lại
//   Last-Event-ID; server phát lại buffer từ seq+1 (server/runs.py:453-469).
// - SSE rớt → poll GET /api/runs/{id} mỗi 2s (hợp đồng §3 quy định).
// - Không nhận event nào > 60s trong khi run vẫn queued/running → stale=true
//   (badge "đang treo"; v1 không có endpoint huỷ — §10.11).
// Mọi trạng thái loading/empty/error/stale đều là hạng nhất của hook này.

import { useCallback, useEffect, useRef, useState } from "react";

import { parseErrorResponse } from "@/lib/api";

import type { RunEvent, RunPayload, StartRunInput } from "./types";

const POLL_INTERVAL_MS = 2_000;
const STALE_AFTER_MS = 60_000;
const STALE_CHECK_MS = 5_000;

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

/** Lỗi có shape thống nhất của hợp đồng §0.1: { error: { code, message, details } }. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function readErrorEnvelope(res: Response): Promise<ApiError> {
  // Parse envelope §0.1/{detail} ở lib/api.ts — chung cho cả 6 màn.
  const body = await parseErrorResponse(res);
  return new ApiError(res.status, body.code ?? "internal_error", body.message, body.details);
}

// Hợp đồng §0: mọi POST đổi trạng thái bắt buộc Content-Type: application/json
// (guard CSRF phía server trả 415 nếu sai — server/main.py:48-65).
async function postJson(url: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type StreamPhase =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "streaming" }
  | { phase: "completed" }
  | { phase: "failed" }
  | { phase: "error"; message: string; activeRunId?: string };

export interface RunStreamState {
  phase: StreamPhase;
  run: RunPayload | null;
  events: RunEvent[];
  /** SSE đang mở (false = đang tự reconnect, poll dự phòng đang bật). */
  sseConnected: boolean;
  /** Không có event mới > 60s trong khi run vẫn chạy (hợp đồng §3). */
  stale: boolean;
}

const IDLE_STATE: RunStreamState = {
  phase: { phase: "idle" },
  run: null,
  events: [],
  sseConnected: false,
  stale: false,
};

function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** Cập nhật run từ event terminal để UI không phải đợi GET cuối cùng. */
function applyEventToRun(run: RunPayload | null, event: RunEvent): RunPayload | null {
  if (!run) return run;
  switch (event.type) {
    case "run_started": {
      const analysts = event.payload["analysts"];
      return {
        ...run,
        status: "running",
        analysts: Array.isArray(analysts)
          ? analysts.filter((a): a is string => typeof a === "string")
          : run.analysts,
      };
    }
    case "run_completed": {
      const signal = event.payload["signal"];
      const paths = event.payload["report_paths"];
      return {
        ...run,
        status: "completed",
        signal: typeof signal === "string" ? signal : run.signal,
        is_review: event.payload["is_review"] === true,
        report_paths:
          paths && typeof paths === "object"
            ? (paths as RunPayload["report_paths"])
            : run.report_paths,
        finished_at: event.ts,
      };
    }
    case "run_failed": {
      const raw = event.payload["error"];
      const err =
        raw && typeof raw === "object" ? (raw as { code?: unknown; message?: unknown }) : {};
      return {
        ...run,
        status: "failed",
        error: {
          code: typeof err.code === "string" ? err.code : "internal_error",
          message:
            typeof err.message === "string"
              ? err.message
              : "Phiên thất bại không rõ nguyên nhân.",
        },
        finished_at: event.ts,
      };
    }
    default:
      return run;
  }
}

export function useRunStream() {
  const [state, setState] = useState<RunStreamState>(IDLE_STATE);

  const sourceRef = useRef<EventSource | null>(null);
  const pollRef = useRef<number | null>(null);
  const lastSeqRef = useRef(0);
  const lastEventAtRef = useRef<number | null>(null);
  // Gán trong openStream (không khởi tạo bằng Date.now() trong render — impure).
  const streamStartedAtRef = useRef<number | null>(null);

  const closeStream = useCallback(() => {
    if (sourceRef.current !== null) {
      sourceRef.current.close();
      sourceRef.current = null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  /** Chốt phiên khi đã nhận event terminal: lấy trạng thái chuẩn từ GET. */
  const finish = useCallback(
    async (runId: string) => {
      closeStream();
      stopPolling();
      let settled = false;
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
        if (res.ok) {
          const run = (await res.json()) as RunPayload;
          setState((s) => ({
            ...s,
            run,
            phase:
              run.status === "failed" ? { phase: "failed" } : { phase: "completed" },
            sseConnected: false,
            stale: false,
          }));
          settled = true;
        }
      } catch {
        // Backend không trả lời — chốt bằng trạng thái suy ra từ event bên dưới.
      }
      if (!settled) {
        setState((s) => ({
          ...s,
          phase:
            s.run?.status === "failed" ? { phase: "failed" } : { phase: "completed" },
          sseConnected: false,
          stale: false,
        }));
      }
    },
    [closeStream, stopPolling],
  );

  /** Poll dự phòng khi SSE rớt (hợp đồng §3: mỗi 2s). */
  const pollRun = useCallback(
    async (runId: string) => {
      let res: Response;
      try {
        res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
      } catch {
        return; // lỗi mạng tạm thời — poll lại chu kỳ sau.
      }
      if (res.status === 404) {
        stopPolling();
        closeStream();
        setState((s) => ({
          ...s,
          phase: {
            phase: "error",
            message: `Không tìm thấy phiên ${runId} trên backend (server có thể đã khởi động lại).`,
          },
          sseConnected: false,
          stale: false,
        }));
        return;
      }
      if (!res.ok) return;
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        return;
      }
      if (isTerminalStatus(run.status)) {
        // SSE chết trước khi terminal kịp tới: poll cứu trạng thái rồi đóng.
        closeStream();
        stopPolling();
        setState((s) => ({
          ...s,
          run,
          phase:
            run.status === "failed" ? { phase: "failed" } : { phase: "completed" },
          sseConnected: false,
          stale: false,
        }));
      } else {
        setState((s) => ({ ...s, run }));
      }
    },
    [closeStream, stopPolling],
  );

  const openStream = useCallback(
    (runId: string) => {
      closeStream();
      stopPolling();
      streamStartedAtRef.current = Date.now();
      const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
      sourceRef.current = source;
      source.onopen = () =>
        setState((s) => (s.sseConnected ? s : { ...s, sseConnected: true }));
      source.onmessage = (message) => {
        let event: RunEvent;
        try {
          event = JSON.parse(message.data) as RunEvent;
        } catch {
          return; // frame hỏng — bỏ qua, server gửi tiếp event sau.
        }
        // Reconnect phát lại buffer: chỉ nhận seq mới hơn mốc đã thấy.
        if (typeof event.seq !== "number" || event.seq <= lastSeqRef.current) return;
        lastSeqRef.current = event.seq;
        lastEventAtRef.current = Date.now();
        setState((s) => ({
          ...s,
          run: applyEventToRun(s.run, event),
          events: [...s.events, event],
          stale: false,
        }));
        if (event.type === "run_completed" || event.type === "run_failed") {
          void finish(runId);
        }
      };
      source.onerror = () => {
        // EventSource tự reconnect (gửi lại Last-Event-ID) — bật poll dự phòng.
        setState((s) => (s.sseConnected ? { ...s, sseConnected: false } : s));
        if (pollRef.current === null) {
          pollRef.current = window.setInterval(
            () => void pollRun(runId),
            POLL_INTERVAL_MS,
          );
        }
      };
    },
    [closeStream, finish, pollRun, stopPolling],
  );

  /** POST /api/runs và mở stream cho run mới. */
  const startRun = useCallback(
    async (input: StartRunInput) => {
      closeStream();
      stopPolling();
      lastSeqRef.current = 0;
      lastEventAtRef.current = null;
      setState({
        phase: { phase: "starting" },
        run: null,
        events: [],
        sseConnected: false,
        stale: false,
      });
      let res: Response;
      try {
        res = await postJson("/api/runs", input);
      } catch {
        setState((s) => ({
          ...s,
          phase: {
            phase: "error",
            message:
              "Không kết nối được backend. Hãy khởi động server FastAPI (uvicorn server.main:app --port 8000) rồi thử lại.",
          },
        }));
        return;
      }
      if (!res.ok) {
        const err = await readErrorEnvelope(res);
        const detailsRunId = err.details["run_id"];
        setState((s) => ({
          ...s,
          phase: {
            phase: "error",
            message: err.message,
            // 409 conflict kèm run_id của phiên đang chạy (server/runs.py:429).
            activeRunId:
              err.status === 409 && typeof detailsRunId === "string"
                ? detailsRunId
                : undefined,
          },
        }));
        return;
      }
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        setState((s) => ({
          ...s,
          phase: { phase: "error", message: "Phản hồi POST /api/runs không đọc được." },
        }));
        return;
      }
      setState({
        phase: { phase: "streaming" },
        run,
        events: [],
        sseConnected: false,
        stale: false,
      });
      openStream(run.run_id);
    },
    [closeStream, openStream, stopPolling],
  );

  /** Bám theo một run đã tồn tại (409 từ tab khác, hoặc active_run_id từ /api/health). */
  const attachRun = useCallback(
    async (runId: string) => {
      closeStream();
      stopPolling();
      lastSeqRef.current = 0;
      lastEventAtRef.current = null;
      setState({
        phase: { phase: "starting" },
        run: null,
        events: [],
        sseConnected: false,
        stale: false,
      });
      let res: Response;
      try {
        res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
      } catch {
        setState((s) => ({
          ...s,
          phase: { phase: "error", message: "Không kết nối được backend khi tải phiên." },
        }));
        return;
      }
      if (!res.ok) {
        const err = await readErrorEnvelope(res);
        setState((s) => ({
          ...s,
          phase: {
            phase: "error",
            message:
              err.status === 404
                ? `Phiên ${runId} không còn trên backend (server có thể đã khởi động lại — run chỉ lưu trong bộ nhớ).`
                : err.message,
          },
        }));
        return;
      }
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        setState((s) => ({
          ...s,
          phase: {
            phase: "error",
            message: "Phản hồi GET /api/runs/{id} không đọc được.",
          },
        }));
        return;
      }
      // Luôn mở stream: với run đã kết thúc, server phát lại toàn bộ buffer rồi
      // đóng (hợp đồng §3) — event terminal phát lại sẽ chốt completed/failed.
      setState({
        phase: { phase: "streaming" },
        run,
        events: [],
        sseConnected: false,
        stale: false,
      });
      openStream(run.run_id);
    },
    [closeStream, openStream, stopPolling],
  );

  /** Về trạng thái trống (sau khi xem xong kết quả). */
  const reset = useCallback(() => {
    closeStream();
    stopPolling();
    lastSeqRef.current = 0;
    lastEventAtRef.current = null;
    setState(IDLE_STATE);
  }, [closeStream, stopPolling]);

  // Mở màn: nếu backend đang có run active (từ tab khác) thì bám theo nó.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok || cancelled) return;
        const health = (await res.json()) as { active_run_id?: unknown };
        const activeId = health["active_run_id"];
        if (!cancelled && typeof activeId === "string" && activeId !== "") {
          await attachRun(activeId);
        }
      } catch {
        // Backend chưa chạy — màn ở trạng thái trống, form vẫn dùng được.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [attachRun]);

  // Định kỳ đánh dấu stale: không event mới > 60s trong khi run vẫn chạy.
  useEffect(() => {
    const id = window.setInterval(() => {
      setState((s) => {
        if (s.phase.phase !== "streaming") return s;
        const status = s.run?.status;
        if (status !== "queued" && status !== "running") return s;
        const reference =
          lastEventAtRef.current ?? streamStartedAtRef.current ?? Date.now();
        const isStale = Date.now() - reference > STALE_AFTER_MS;
        return isStale === s.stale ? s : { ...s, stale: isStale };
      });
    }, STALE_CHECK_MS);
    return () => window.clearInterval(id);
  }, []);

  // Dọn stream/poll khi unmount.
  useEffect(
    () => () => {
      closeStream();
      stopPolling();
    },
    [closeStream, stopPolling],
  );

  return { state, startRun, attachRun, reset };
}
