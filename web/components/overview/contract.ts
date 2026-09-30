// M1 "Tổng quan lợi nhuận" — tầng dữ liệu của màn: types + fetch theo đúng
// docs/ui-api-contract.md. Màn này chỉ ĐỌC ba endpoint có trong hợp đồng:
//   GET /api/health  (§8)   — exec_mode, halted_today, pending_approvals
//   GET /api/history (§7)   — decision log: rating / pending / alpha (chuỗi "+1.2%")
//   GET /api/audit   (§6)   — nhật ký thực thi: lệnh buy/sell, qty, price_est
// Không tự chế endpoint ngoài hợp đồng; mọi response mang `generated_at`.
import { parseErrorResponse } from "@/lib/api";

/** Lỗi fetch: mạng đứt (backend chưa chạy) hoặc HTTP với error shape §0.1. */
export class FetchError extends Error {
  readonly kind: "network" | "http";
  readonly status: number | null;
  readonly code: string;

  constructor(kind: "network" | "http", message: string, status: number | null, code: string) {
    super(message);
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

async function fetchJson<T>(url: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch {
    throw new FetchError(
      "network",
      "Không kết nối được backend FastAPI.",
      null,
      "network_error"
    );
  }
  if (!res.ok) {
    // Parse envelope §0.1 ở lib/api.ts (chung cho cả 6 màn, xử lý cả {detail}).
    const body = await parseErrorResponse(res);
    throw new FetchError(
      "http",
      body.message,
      res.status,
      body.code ?? `http_${res.status}`
    );
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// GET /api/health — docs/ui-api-contract.md §8 (server/health.py:44-56)

export type HealthPayload = {
  generated_at: string;
  status: "ok" | "degraded";
  exec_mode: string;
  halted_today: boolean;
  active_run_id: string | null;
  pending_approvals: number;
};

// ---------------------------------------------------------------------------
// GET /api/history — docs/ui-api-contract.md §7 (server/history.py:57-100).
// Chỉ type hoá các field màn này dùng; payload thật có thêm field khác.

export type HistoryItem = {
  date: string;
  ticker: string;
  rating: string;
  pending: boolean;
  /** Chuỗi gốc của log, dạng "+1.2%" / "-0.4%" (decision_log.py:139). */
  alpha: string | null;
};

export type HistoryPayload = {
  generated_at: string;
  items: HistoryItem[];
  total: number;
  /** Server trả khi có block log không parse được (server/history.py:98-99). */
  warning?: string;
};

// ---------------------------------------------------------------------------
// GET /api/audit — docs/ui-api-contract.md §6 (server/audit.py:135-147).

export type AuditItem = {
  timestamp: string | null;
  ticker: string | null;
  action: string | null;
  qty: number | null;
  price_est: number | null;
  mode: string | null;
  order_id: string | null;
  phase: string | null;
};

export type AuditPayload = {
  generated_at: string;
  items: AuditItem[];
  total_matching: number;
  total_lines: number;
  halted_today: boolean;
  halt_reason: string | null;
  /** Dòng JSON hỏng bị bỏ qua — UI phải thấy được (hợp đồng §10.7). */
  skipped_lines: number;
};

// ---------------------------------------------------------------------------

export function fetchHealth(): Promise<HealthPayload> {
  return fetchJson<HealthPayload>("/api/health");
}

export function fetchHistory(limit: number): Promise<HistoryPayload> {
  return fetchJson<HistoryPayload>(`/api/history?limit=${limit}`);
}

export function fetchAudit(pageSize: number): Promise<AuditPayload> {
  return fetchJson<AuditPayload>(`/api/audit?page_size=${pageSize}`);
}

/**
 * "+1.2%" → 1.2 (điểm %), "-0.4%" → −0.4; null/"n/a" → null.
 * Log lưu alpha là chuỗi % một chữ số thập phân — engine cũng strip "%" rồi
 * parse (tradingagents/backtest.py:64-74).
 */
export function parseAlphaPercent(raw: string | null): number | null {
  if (!raw) return null;
  const value = Number.parseFloat(raw.trim().replace("%", ""));
  return Number.isFinite(value) ? value : null;
}
