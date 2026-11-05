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
import { getMessages } from "@/lib/i18n/messages";

import type { RunEvent, RunPayload, StartRunInput } from "./types";

const POLL_INTERVAL_MS = 2_000;
const STALE_AFTER_MS = 60_000;
const STALE_CHECK_MS = 5_000;

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);

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
  | { phase: "interrupted" }
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

function terminalPhase(status: RunPayload["status"]): StreamPhase {
  if (status === "failed") return { phase: "failed" };
  if (status === "interrupted") return { phase: "interrupted" };
  return { phase: "completed" };
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
              : getMessages().pipeline.errors.failedUnknown,
        },
        finished_at: event.ts,
      };
    }
    case "run_interrupted": {
      const raw = event.payload["error"];
      const err =
        raw && typeof raw === "object" ? (raw as { code?: unknown; message?: unknown }) : {};
      return {
        ...run,
        status: "interrupted",
        error: {
          code: typeof err.code === "string" ? err.code : "interrupted",
          message:
            typeof err.message === "string"
              ? err.message
              : getMessages().pipeline.errors.interruptedUnknown,
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
  const generationRef = useRef(0);
  const pollGenerationRef = useRef(0);
  const pollSequenceRef = useRef(0);
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
    pollGenerationRef.current += 1;
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const invalidate = useCallback(() => {
    generationRef.current += 1;
    closeStream();
    stopPolling();
    return generationRef.current;
  }, [closeStream, stopPolling]);

  /** Chốt ngay bằng event terminal; GET chỉ bổ sung nếu cùng trạng thái. */
  const finish = useCallback(
    async (
      runId: string,
      terminalStatus: RunPayload["status"],
      generation: number,
      terminalEvent: RunEvent,
    ) => {
      if (generation !== generationRef.current) return;
      const finishingGeneration = generationRef.current;
      closeStream();
      stopPolling();
      setState((s) => {
        if (finishingGeneration !== generationRef.current || s.run?.run_id !== runId) return s;
        return {
          ...s,
          run: applyEventToRun(s.run, terminalEvent),
          phase: terminalPhase(terminalStatus),
          sseConnected: false,
          stale: false,
        };
      });
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
        if (!res.ok || finishingGeneration !== generationRef.current) return;
        const run = (await res.json()) as RunPayload;
        if (finishingGeneration !== generationRef.current || run.run_id !== runId) return;
        setState((s) =>
          finishingGeneration === generationRef.current &&
          s.run?.run_id === runId &&
          isTerminalStatus(run.status) &&
          run.status === terminalStatus
            ? { ...s, run }
            : s,
        );
      } catch {
        // Backend không trả lời — giữ trạng thái terminal từ event.
      }
    },
    [closeStream, stopPolling],
  );

  /** Poll dự phòng khi SSE rớt (hợp đồng §3: mỗi 2s). */
  const pollRun = useCallback(
    async (runId: string, generation: number, pollGeneration: number) => {
      const sequence = ++pollSequenceRef.current;
      const eventSequence = lastSeqRef.current;
      const isCurrent = () =>
        generation === generationRef.current &&
        pollGeneration === pollGenerationRef.current &&
        sequence === pollSequenceRef.current &&
        eventSequence === lastSeqRef.current &&
        pollRef.current !== null;
      if (!isCurrent()) return;
      let res: Response;
      try {
        res = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
      } catch {
        return; // lỗi mạng tạm thời — poll lại chu kỳ sau.
      }
      if (!isCurrent()) return;
      if (res.status === 404) {
        const endedGeneration = invalidate();
        setState((s) =>
          endedGeneration === generationRef.current && s.run?.run_id === runId
            ? {
                ...s,
                phase: {
                  phase: "error",
                  message: getMessages().pipeline.errors.runNotFound(runId),
                },
                sseConnected: false,
                stale: false,
              }
            : s,
        );
        return;
      }
      if (!res.ok) return;
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        return;
      }
      if (!isCurrent() || run.run_id !== runId) return;
      if (isTerminalStatus(run.status)) {
        // SSE chết trước khi terminal kịp tới: poll cứu trạng thái rồi đóng.
        const endedGeneration = invalidate();
        setState((s) => endedGeneration === generationRef.current && s.run?.run_id === runId ? {
          ...s,
          run,
          phase: terminalPhase(run.status),
          sseConnected: false,
          stale: false,
        } : s);
      } else {
        setState((s) =>
          isCurrent() && s.run?.run_id === runId && !isTerminalStatus(s.run.status)
            ? { ...s, run }
            : s,
        );
      }
    },
    [invalidate],
  );

  const openStream = useCallback(
    (runId: string) => {
      const generation = invalidate();
      streamStartedAtRef.current = Date.now();
      const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
      sourceRef.current = source;
      const isCurrent = () =>
        generation === generationRef.current && sourceRef.current === source;
      source.onopen = () => {
        if (!isCurrent()) return;
        stopPolling();
        setState((s) =>
          generation === generationRef.current && s.run?.run_id === runId
            ? s.sseConnected
              ? s
              : { ...s, sseConnected: true }
            : s,
        );
      };
      const handleEvent = (message: MessageEvent<string>) => {
        if (!isCurrent()) return;
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
        setState((s) => {
          if (generation !== generationRef.current || s.run?.run_id !== runId) return s;
          const nextRun = applyEventToRun(s.run, event);
          return nextRun?.run_id === runId
            ? { ...s, run: nextRun, events: [...s.events, event], stale: false }
            : s;
        });
        if (event.type === "run_completed") {
          void finish(runId, "completed", generation, event);
        } else if (event.type === "run_failed") {
          void finish(runId, "failed", generation, event);
        } else if (event.type === "run_interrupted") {
          void finish(runId, "interrupted", generation, event);
        }
      };
      for (const eventType of [
        "run_started",
        "message",
        "tool_call",
        "analyst_status",
        "debate_update",
        "research_manager",
        "trader_update",
        "risk_update",
        "pm_decision",
        "run_completed",
        "run_failed",
        "run_interrupted",
      ]) {
        source.addEventListener(eventType, handleEvent);
      }
      source.onerror = () => {
        if (!isCurrent()) return;
        // EventSource tự reconnect (gửi lại Last-Event-ID) — bật poll dự phòng.
        setState((s) =>
          generation === generationRef.current && s.run?.run_id === runId
            ? s.sseConnected
              ? { ...s, sseConnected: false }
              : s
            : s,
        );
        if (pollRef.current === null) {
          const pollGeneration = ++pollGenerationRef.current;
          pollRef.current = window.setInterval(
            () => void pollRun(runId, generation, pollGeneration),
            POLL_INTERVAL_MS,
          );
        }
      };
    },
    [finish, invalidate, pollRun, stopPolling],
  );

  /** POST /api/runs và mở stream cho run mới. */
  const startRun = useCallback(
    async (input: StartRunInput) => {
      const generation = invalidate();
      const isCurrent = () => generation === generationRef.current;
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
        setState((s) => isCurrent() ? {
          ...s,
          phase: {
            phase: "error",
            message: getMessages().pipeline.errors.backendDown,
          },
        } : s);
        return;
      }
      if (generation !== generationRef.current) return;
      if (!res.ok) {
        const err = await readErrorEnvelope(res);
        const detailsRunId = err.details["run_id"];
        setState((s) => isCurrent() ? {
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
        } : s);
        return;
      }
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        setState((s) => isCurrent() ? {
          ...s,
          phase: { phase: "error", message: getMessages().pipeline.errors.postUnreadable },
        } : s);
        return;
      }
      if (generation !== generationRef.current) return;
      setState({
        phase: { phase: "streaming" },
        run,
        events: [],
        sseConnected: false,
        stale: false,
      });
      openStream(run.run_id);
    },
    [invalidate, openStream],
  );

  /** Bám theo một run đã tồn tại (409 từ tab khác, hoặc active_run_id từ /api/health). */
  const attachRun = useCallback(
    async (runId: string) => {
      const generation = invalidate();
      const isCurrent = () => generation === generationRef.current;
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
        setState((s) => isCurrent() ? {
          ...s,
          phase: { phase: "error", message: getMessages().pipeline.errors.loadNetwork },
        } : s);
        return;
      }
      if (generation !== generationRef.current) return;
      if (!res.ok) {
        const err = await readErrorEnvelope(res);
        if (generation !== generationRef.current) return;
        setState((s) => isCurrent() ? {
          ...s,
          phase: {
            phase: "error",
            message:
              err.status === 404
                ? getMessages().pipeline.errors.runGone(runId)
                : err.message,
          },
        } : s);
        return;
      }
      let run: RunPayload;
      try {
        run = (await res.json()) as RunPayload;
      } catch {
        setState((s) => isCurrent() ? {
          ...s,
          phase: {
            phase: "error",
            message: getMessages().pipeline.errors.getUnreadable,
          },
        } : s);
        return;
      }
      if (generation !== generationRef.current || run.run_id !== runId) return;
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
    [invalidate, openStream],
  );

  /** Về trạng thái trống (sau khi xem xong kết quả). */
  const reset = useCallback(() => {
    invalidate();
    lastSeqRef.current = 0;
    lastEventAtRef.current = null;
    setState(IDLE_STATE);
  }, [invalidate]);

  // Mở màn: nếu backend đang có run active (từ tab khác) thì bám theo nó.
  useEffect(() => {
    const generation = generationRef.current;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok || cancelled || generation !== generationRef.current) return;
        const health = (await res.json()) as { active_run_id?: unknown };
        const activeId = health["active_run_id"];
        if (
          !cancelled &&
          generation === generationRef.current &&
          typeof activeId === "string" &&
          activeId !== ""
        ) {
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
      invalidate();
    },
    [invalidate],
  );

  return { state, startRun, attachRun, reset };
}
