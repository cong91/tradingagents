// Shape dữ liệu hợp đồng cho màn M3 (docs/ui-api-contract.md §2 watchlist, §5 daily pipeline).

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

export interface DailyPlanSummary {
  side: string;
  quantity: number;
  price_est: number;
  cost: number;
}

export interface DailyResultItem {
  ticker: string;
  signal: string;
  executed: boolean;
  order_id: string | null;
  reason: string;
  plan_summary: DailyPlanSummary | null;
  approval_id: string | null;
}

export type DailyJobStatus = "queued" | "running" | "completed" | "failed";

/** Response 202 của POST /api/daily (contract §5). */
export interface DailyJobStart {
  job_id: string;
  status: DailyJobStatus;
  tickers: string[];
  created_at: string;
  detail_url: string;
}

/** Response 200 của GET /api/daily/{job_id} (contract §5). */
export interface DailyJobStatusResponse {
  job_id: string;
  status: DailyJobStatus;
  created_at: string;
  finished_at: string | null;
  results: DailyResultItem[];
}

export type ApiErrorInfo = {
  status: number;
  code: string;
  message: string;
};
