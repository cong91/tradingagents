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
  /** Giá cắt lỗ/chốt lời đính kèm (ATR-derived, rev 5) — null khi ATR thiếu. */
  stop_loss?: number | null;
  take_profit?: number | null;
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
  /** "crypto" | "stock" — stock chỉ advisory, không sinh lệnh (rev 4). */
  asset_type?: string;
}

export type DailyJobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  /** Job bị người dùng dừng giữa chừng (POST /api/daily/{id}/cancel). */
  | "cancelled"
  /** Job còn ở trạng thái chạy khi server khởi động lại — thread đã chết. */
  | "interrupted";

/** Một dòng lịch sử job (GET /api/daily) — đủ để hiển thị, không kèm results. */
export interface DailyJobSummary {
  job_id: string;
  status: DailyJobStatus;
  mode: DailyJobMode;
  auto: boolean;
  top_n: number | null;
  created_at: string;
  finished_at: string | null;
  tickers: string[];
  results_count: number;
  approval_count: number;
  error: string | null;
}

/** Response 200 của GET /api/daily — lịch sử job, mới nhất trước.
 *  `active_job_id` là nguồn duy nhất cho băng "đang chạy" của UI: job đang
 *  chạy không còn vô hình, và cũng không thể báo nhầm job của process khác. */
export interface DailyJobListResponse {
  generated_at: string;
  items: DailyJobSummary[];
  page: number;
  page_size: number;
  total: number;
  active_job_id: string | null;
}

/** Response 202 của POST /api/daily (contract §5). */
export interface DailyJobStart {
  job_id: string;
  status: DailyJobStatus;
  mode: DailyJobMode;
  tickers: string[];
  created_at: string;
  detail_url: string;
}

/** Response 200 của GET /api/daily/{job_id} (contract §5 rev 3+4+7). */
export interface DailyJobStatusResponse {
  generated_at: string;
  job_id: string;
  status: DailyJobStatus;
  mode: DailyJobMode;
  created_at: string;
  last_progress_at: string;
  current_ticker: string | null;
  finished_at: string | null;
  tickers: string[];
  /** Job auto (rev 4): sàng lọc thị trường gắn trên payload. */
  asset_types?: Record<string, string>;
  screening?: ScreeningInfo | null;
  /** Lý do job tự thất bại (screen/bridge); lỗi từng coin nằm ở result. */
  error?: string | null;
  results: DailyResultItem[];
}

export interface FuturesCycleCandidate {
  symbol?: string;
  ticker?: string;
  status?: string;
  signal?: string | null;
  reason?: string | null;
  reason_code?: string | null;
  skipped_reason?: string | null;
  score?: number | null;
}

export interface FuturesCyclePayload {
  signal?: string | null;
  /** Yêu cầu gốc dạng pipeline (vd NEAR-USD) — rev 6. */
  requested_ticker?: string | null;
  /** Unified symbol từ metadata venue (vd NEAR/USDT:USDT) — rev 6. */
  unified_symbol?: string | null;
  /** Lý do market bị loại (spot-only, inactive, missing, wrong settle). */
  market_rejection_reason?: string | null;
  /** Kết quả xác nhận 1h/4h cùng hướng (rev 6). */
  confirmation_result?: boolean | null;
  /** aligned_direction | doji_candle | direction_mismatch | ... */
  confirmation_reason?: string | null;
  /** tradeable_signal | signal_not_tradeable */
  signal_reason?: string | null;
  /** Lý do no_order từ kế hoạch (Hold, minimum, risk refusal...). */
  plan_reason?: string | null;
  /** executed | awaiting_approval | not_executed | failed */
  execution_status?: string | null;
  reason?: string | null;
  reason_code?: string | null;
  candidates?: FuturesCycleCandidate[];
  claims?: { status?: string; signal_id?: string | null; cycle_id?: string | null }[];
  snapshot?: {
    one_hour_open?: number | string;
    four_hour_open?: number | string;
    one_hour_close?: number | string;
    four_hour_close?: number | string;
    /** bullish | bearish | doji (rev 6). */
    one_hour_direction?: string | null;
    four_hour_direction?: string | null;
    confirmation?: boolean;
    next_four_hour_open?: number | string;
    last_four_hour_close?: number | string;
  };
  plan?: {
    side?: string;
    quantity?: number;
    price_est?: number;
    stop_loss?: number | null;
    take_profit?: number | null;
    leverage?: number;
    cost?: number;
    reason?: string | null;
  };
  error?: string | null;
  entry_order_id?: string | null;
  order_id?: string | null;
  executed?: boolean;
  protection_state?: string | null;
  recovery_state?: string | null;
  position_state?: Record<string, unknown> | null;
  skipped_reason?: string | null;
}

export interface FuturesCyclesResponse {
  generated_at: string;
  items: FuturesCycle[];
  running_cycle?: boolean;
  enabled?: boolean;
  interval_minutes?: number;
  last_cycle?: Record<string, unknown> | null;
}

export interface FuturesCycle {
  cycle_id: string;
  symbol: string;
  four_hour_open: string;
  one_hour_open: string;
  strategy_version: string;
  status: string;
  phase: string;
  payload: FuturesCyclePayload;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export type ApiErrorInfo = {
  status: number;
  code: string;
  message: string;
};

/** Một ứng viên sau khi server sàng lọc thị trường (auto-scan, contract §5 rev 4).
 *  Metrics theo chuẩn screener thực chiến: RSI(14), ATR%, khoảng cách đỉnh 52 tuần,
 *  RVOL, breakout 20 phiên kèm khối lượng, dollar-volume trung bình 20 phiên. */
export interface ScreeningCandidate {
  ticker: string;
  asset_type: "crypto" | "stock";
  score: number;
  last_close: number;
  momentum_pct: number;
  trend_pct: number;
  volume_ratio: number;
  rsi14: number | null;
  atr_pct: number | null;
  dist_52w_pct: number;
  breakout: boolean;
  avg_dollar_volume: number;
  reasons: string[];
}

/** Khối screening gắn trên job mode auto (contract §5 rev 4). */
export interface ScreeningInfo {
  universe_count: number;
  universe_crypto: number;
  universe_stock: number;
  candidates: ScreeningCandidate[];
  /** Chỉ job mock — sàng cố định, không đụng network. */
  mock?: boolean;
}
