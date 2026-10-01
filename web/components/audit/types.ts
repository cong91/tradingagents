// Shape payload backend cho màn M6 — đối chiếu server/audit.py:135-147 và
// server/history.py:92-150 (đọc 2026-09-30), khớp docs/ui-api-contract.md §6-§7.

/** Một dòng audit JSONL nguyên văn (+ `_line_no` do server gắn) hoặc dòng điều khiển halt. */
export type AuditEvent = {
  timestamp?: string;
  ticker?: string;
  ccxt_symbol?: string;
  signal?: string;
  action?: string;
  qty?: number | null;
  price_est?: number | null;
  mode?: string;
  confirmed?: boolean;
  order_id?: string | null;
  phase?: string;
  market?: string;
  leverage?: number | null;
  reason?: string | null;
  /** Chỉ có trên dòng điều khiển: "halt" | "halt_reset" — không có action. */
  event?: string;
  _line_no?: number;
};

export type AuditPayload = {
  generated_at: string;
  items: AuditEvent[];
  page: number;
  page_size: number;
  total_matching: number;
  total_lines: number;
  halted_today: boolean;
  halt_reason?: string | null;
  last_halt: string | null;
  last_halt_reset: string | null;
  skipped_lines?: number;
};

/** Entry decision log — dùng chung cho /api/history và chi tiết /api/backtest. */
export type HistoryItem = {
  date: string;
  ticker: string;
  rating: string;
  pending: boolean;
  /** Chuỗi % từ log, vd "+1.2%" — không phải số. */
  raw: string | null;
  alpha: string | null;
  holding: string | null;
  resolved: string | null;
  decision_excerpt: string;
  reflection: string | null;
  run_artifacts: {
    state_log: string | null;
    reports_dir: string | null;
    saved_report: string | null;
  };
};

export type HistoryPayload = {
  generated_at: string;
  items: HistoryItem[];
  total: number;
  by_ticker: Record<string, number>;
  warning?: string;
};

export type RatingScore = {
  count: number;
  hit_rate: number | null;
  /** Phân số: 0.012 nghĩa là +1,2% (backtest.py:_alpha chia 100). */
  mean_alpha: number;
};

export type BacktestRun = {
  run_id: string;
  log_path: string;
  entries: number;
  resolved: number;
  pending: number;
  unscored: number;
  holding: string;
  by_rating: Record<string, RatingScore>;
  warning?: string;
};

export type BacktestListPayload = {
  generated_at: string;
  runs: BacktestRun[];
};

export type BacktestDetailPayload = BacktestRun & {
  entries: HistoryItem[];
  warning?: string;
};
