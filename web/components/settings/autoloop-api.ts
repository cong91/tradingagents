// Lớp fetch cho card "Chế độ tự động" ở màn Cấu hình. Mọi POST gửi kèm
// Content-Type: application/json kể cả body rỗng — quy tắc CSRF bắt buộc của
// backend (contract §0, server/main.py:48-65). Shape lỗi parse qua lib/api.ts
// (envelope §0.1 / {detail} của FastAPI) — chung cho cả 6 màn.
import { NETWORK_HINT, parseErrorResponse } from "@/lib/api";

import type {
  AutoloopHaltResponse,
  AutoloopPayload,
  AutoloopRunNowResponse,
} from "./autoloop-types";

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

/** Lỗi HTTP/network chuẩn hoá để UI hiển thị thông điệp + nút retry. */
export class AutoloopApiError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "AutoloopApiError";
    this.status = status;
    this.code = code;
  }
}

/** Chuẩn hoá mọi throw của fetch thành AutoloopApiError. */
export function toAutoloopError(err: unknown): AutoloopApiError {
  if (err instanceof AutoloopApiError) return err;
  // fetch throw TypeError khi network đứt hoặc rewrite không tới được backend.
  return new AutoloopApiError(0, "network", NETWORK_HINT);
}

async function parseHttpError(res: Response): Promise<AutoloopApiError> {
  const body = await parseErrorResponse(res);
  return new AutoloopApiError(res.status, body.code, body.message);
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw toAutoloopError(err);
  }
  if (!res.ok) throw await parseHttpError(res);
  return (await res.json()) as T;
}

export const autoloopApi = {
  get(): Promise<AutoloopPayload> {
    return requestJson<AutoloopPayload>("/api/autoloop", { cache: "no-store" });
  },
  /** POST /api/autoloop — server trả 409 gate_closed khi cổng chưa đủ (fail-closed). */
  set(enabled: boolean, intervalMinutes: number): Promise<AutoloopPayload> {
    return requestJson<AutoloopPayload>("/api/autoloop", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ enabled, interval_minutes: intervalMinutes }),
    });
  },
  runNow(): Promise<AutoloopRunNowResponse> {
    return requestJson<AutoloopRunNowResponse>("/api/autoloop/run-now", {
      method: "POST",
      headers: JSON_HEADERS,
      body: "{}",
    });
  },
  halt(): Promise<AutoloopHaltResponse> {
    return requestJson<AutoloopHaltResponse>("/api/halt", {
      method: "POST",
      headers: JSON_HEADERS,
      body: "{}",
    });
  },
  resetHalt(): Promise<AutoloopHaltResponse> {
    return requestJson<AutoloopHaltResponse>("/api/halt/reset", {
      method: "POST",
      headers: JSON_HEADERS,
      body: "{}",
    });
  },
};
