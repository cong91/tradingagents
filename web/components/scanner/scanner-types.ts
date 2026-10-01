// Shape dữ liệu hợp đồng cho màn M3 (docs/ui-api-contract.md §2 watchlist, §5 daily pipeline rev 3).
// §5 rev 3: worker per-coin, enqueue ngay khi coin xong, job KHÔNG BAO GIỜ tự
// thực thi — kết quả chỉ mang kế hoạch (plan) + approval_id, không có
// executed/order_id (bất biến §5: mọi lệnh chỉ đi qua màn Duyệt lệnh).

export interface WatchlistSymbol {
  symbol: string;
  added_at: string;
}

export interface WatchlistResponse {
  generated_at: string;
  symbols: WatchlistSymbol[];
  /** Server khôi phục từ config default khi file state hỏng (contract §2). */
  warning?: string;
}

export type DailyJobMode = "live" | "mock";

/** Chi tiết kế hoạch của một coin (đủ cho màn Duyệt lệnh — contract §5). */
export interface DailyPlan {
  side: string;
  quantity: number;
  price_est: number;
  cost: number;
  ccxt_symbol: string;
  market: string;
  leverage: number;
  mode_at_plan: string;
  plan_reason: string | null;
}

export interface DailyResultItem {
  ticker: string;
  /** 5-tier hoặc "REVIEW"; coin fail giữa pipeline → null. */
  signal: string | null;
  /** Lý do không trade / "awaiting approval" / "<ExceptionType>: <message>". */
  reason: string | null;
  plan: DailyPlan | null;
  /** Tham chiếu GET /api/approvals — null khi kế hoạch không trade được. */
  approval_id: string | null;
  /** Lỗi hệ thống (khác reason nghiệp vụ). */
  error: string | null;
  /** Chỉ job mode:"mock" — "replay:<path>" hoặc "synthetic". */
  mock_source?: string;
}

export type DailyJobStatus = "queued" | "running" | "completed" | "failed";

/** Response 202 của POST /api/daily (contract §5). */
export interface DailyJobStart {
  job_id: string;
  status: DailyJobStatus;
  mode: DailyJobMode;
  tickers: string[];
  created_at: string;
  detail_url: string;
}

/** Response 200 của GET /api/daily/{job_id} (contract §5 rev 3). */
export interface DailyJobStatusResponse {
  generated_at: string;
  job_id: string;
  status: DailyJobStatus;
  mode: DailyJobMode;
  created_at: string;
  finished_at: string | null;
  tickers: string[];
  results: DailyResultItem[];
}

export type ApiErrorInfo = {
  status: number;
  code: string;
  message: string;
};
