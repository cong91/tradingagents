// Shape dữ liệu màn Duyệt lệnh — khớp docs/ui-api-contract.md §4 và
// server/approvals.py (GET /api/approvals, POST .../approve, POST .../reject).

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "executed"
  | "execute_failed"
  | "expired";

export type ApprovalItem = {
  id: string;
  created_at: string;
  run_id: string | null;
  /** Job POST /api/daily đã sinh kế hoạch này (contract §4 rev 3) — optional
   * vì backend đang hiện thực song song; hiển thị khi có. */
  daily_job_id?: string | null;
  /** created_at + 24h; quá hạn trên item còn pending → badge suy ra (§4). */
  expires_at?: string | null;
  ticker: string | null;
  ccxt_symbol: string | null;
  signal: string | null;
  side: string | null;
  quantity: number | null;
  price_est: number | null;
  cost: number | null;
  market: string | null;
  leverage: number | null;
  mode_at_plan: string | null;
  plan_reason: string | null;
  /** Kế hoạch demo từ job mock — approve bị server từ chối (409). */
  mock?: boolean;
  status: ApprovalStatus;
  resolved_at: string | null;
  resolution: string | null;
  /** Điền khi approve resolve (contract §4): live → true, dry-fill → false. */
  executed?: boolean | null;
  mode?: string | null;
  order_id?: string | null;
};

export type ApprovalsResponse = {
  generated_at: string;
  items: ApprovalItem[];
  pending_count: number;
};

// Response của POST /api/approvals/{id}/approve (server/approvals.py:166-174;
// hợp đồng §4 — `warning` đánh dấu "order WAS placed but audit write failed").
export type ApproveResponse = {
  id: string;
  status: ApprovalStatus;
  executed: boolean;
  mode: string | null;
  order_id: string | null;
  risk?: { allowed: boolean; halted: boolean; reason: string | null };
  audit_action?: string;
  warning?: string;
};

// Envelope lỗi thống nhất của hợp đồng (§0.1).
export type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    details?: Record<string, unknown>;
  };
};

// GET /api/health (hợp đồng §8, server/health.py:44-56) — nguồn banner halt.
export type HealthResponse = {
  generated_at: string;
  exec_mode: string;
  halted_today: boolean;
  audit_log_readable: boolean;
  pending_approvals: number;
  status: string;
};
