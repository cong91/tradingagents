// Fetch cho thẻ "Override vận hành". Mọi thay đổi cổng phải kèm confirm=true và
// reason (server/operator_config.py) — lớp này là chỗ duy nhất dựng body đó.
import { networkHint, parseErrorResponse } from "@/lib/api";

import type { OperatorConfigPayload } from "./operator-config-types";

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

export class OperatorConfigApiError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "OperatorConfigApiError";
    this.status = status;
    this.code = code;
  }
}

export function toOperatorError(err: unknown): OperatorConfigApiError {
  if (err instanceof OperatorConfigApiError) return err;
  return new OperatorConfigApiError(0, "network", networkHint());
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw toOperatorError(err);
  }
  if (!res.ok) {
    const body = await parseErrorResponse(res);
    throw new OperatorConfigApiError(res.status, body.code, body.message);
  }
  return (await res.json()) as T;
}

export const operatorConfigApi = {
  get(): Promise<OperatorConfigPayload> {
    return requestJson<OperatorConfigPayload>("/api/operator-config", { cache: "no-store" });
  },

  /**
   * Ghi override. `confirm`/`reason` bắt buộc khi payload chứa key cổng — server
   * trả 412 gate_confirmation_required hoặc 422 reason_required nếu thiếu.
   */
  update(
    settings: Record<string, string | boolean>,
    options?: { confirm?: boolean; reason?: string; actor?: string }
  ): Promise<OperatorConfigPayload> {
    return requestJson<OperatorConfigPayload>("/api/operator-config", {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({
        settings,
        confirm: options?.confirm === true,
        reason: options?.reason,
        actor: options?.actor,
      }),
    });
  },

  /** Xoá override để quay về default của engine. */
  reset(key: string): Promise<OperatorConfigPayload> {
    return requestJson<OperatorConfigPayload>(
      `/api/operator-config/${encodeURIComponent(key)}`,
      { method: "DELETE", headers: JSON_HEADERS, body: "{}" }
    );
  },
};
