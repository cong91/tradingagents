// Wire types cho màn M2 — khớp docs/ui-api-contract.md §3 và server/runs.py.
// Chỉ mô tả dữ liệu trao đổi với backend; không chứa logic.

export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export type RunMode = "live" | "mock";

export type AssetType = "stock" | "crypto";

export interface StartRunInput {
  ticker: string;
  trade_date: string;
  asset_type: AssetType;
  mode: RunMode;
  /** undefined → server theo config `checkpoint_enabled` (hợp đồng §3). */
  checkpoint?: boolean;
  source_run?: string | null;
}

export interface RunReportPaths {
  complete_report?: string | null;
  state_log?: string | null;
}

export interface RunError {
  code: string;
  message: string;
}

/** Shape trả về của POST /api/runs và GET /api/runs/{id} (server/runs.py:97-113). */
export interface RunPayload {
  run_id: string;
  status: RunStatus;
  mode: RunMode;
  ticker: string;
  trade_date: string;
  asset_type: AssetType;
  created_at: string;
  finished_at: string | null;
  signal: string | null;
  is_review: boolean;
  error: RunError | null;
  report_paths: RunReportPaths | null;
  analysts: string[];
  events_url: string;
}

export type RunStage = "analyst" | "researcher" | "trader" | "risk" | "pm" | "system";

export type RunEventType =
  | "run_started"
  | "message"
  | "tool_call"
  | "analyst_status"
  | "debate_update"
  | "research_manager"
  | "trader_update"
  | "risk_update"
  | "pm_decision"
  | "run_completed"
  | "run_failed";

/** Một event SSE (bên trong `data:` JSON — server/runs.py:126-141). */
export interface RunEvent {
  seq: number;
  ts: string;
  type: RunEventType;
  role: string;
  stage: RunStage;
  agent: string | null;
  replay: boolean;
  payload: Record<string, unknown>;
}
