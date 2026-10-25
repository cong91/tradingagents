// Shape của GET/POST /api/autoloop, POST /api/autoloop/run-now, POST /api/halt,
// POST /api/halt/reset (server/autoloop.py) — mirror hợp đồng, không thêm gì.
// Thứ tự 6 hàng cổng an toàn sống ở đây vì ánh xạ 1-1 theo key máy chủ trả.

export type AutoloopGates = {
  auto_confirm_config: boolean;
  auto_confirm_env: boolean;
  exec_live_config: boolean;
  exec_live_env: boolean;
  exec_derivatives_config: boolean;
  exec_derivatives_env: boolean;
  all_open: boolean;
};

export type AutoloopGateKey = Exclude<keyof AutoloopGates, "all_open">;

export type AutoloopLastCycleResult = {
  ticker: string;
  signal: string | null;
  executed: boolean;
  order_id: string | null;
  reason: string | null;
  stop_loss: number | null;
  take_profit: number | null;
};

export type AutoloopTelemetryEvent = {
  event: "started" | "finished" | "failed";
  stage: string;
  ticker: string | null;
  node: string | null;
  started_at: string;
  duration_ms?: number;
  error_type?: string;
};

export type AutoloopTelemetry = {
  active_stage: string | null;
  active_ticker: string | null;
  active_elapsed_ms: number | null;
  active_node?: string | null;
  active_path?: string[];
  active_spans?: Array<{
    stage: string;
    ticker: string | null;
    node: string | null;
    started_at: string;
    elapsed_ms: number;
  }>;
  last_stage: string | null;
  current_ticker: string | null;
  events: AutoloopTelemetryEvent[];
  dropped_events: number;
};

export type AutoloopLastCycle = {
  started_at: string;
  finished_at: string | null;
  status: "running" | "completed" | "timeout" | "failed" | "skipped_busy";
  reason: string | null;
  error: string | null;
  candidates: string[];
  results: AutoloopLastCycleResult[];
  universe?: { crypto: number; stock: number };
  telemetry?: AutoloopTelemetry;
};

export type AutoloopPayload = {
  generated_at: string;
  enabled: boolean;
  interval_minutes: number;
  running_cycle: boolean;
  gates: AutoloopGates;
  last_cycle: AutoloopLastCycle | null;
};

export type AutoloopRunNowResponse = {
  generated_at: string;
  status: string;
};

export type AutoloopHaltResponse = {
  generated_at: string;
  halted: boolean;
};

// Ràng buộc giống server/autoloop.py (MIN/MAX/DEFAULT_INTERVAL_MINUTES).
export const AUTOLOOP_MIN_INTERVAL = 15;
export const AUTOLOOP_MAX_INTERVAL = 1440;
export const AUTOLOOP_DEFAULT_INTERVAL = 1440;

/** Thứ tự hiển thị của 6 hàng cổng an toàn (server/autoloop.py gates()); nhãn ở
 * lib/i18n/messages/settings.ts (autoloop.gateRows). */
export const AUTOLOOP_GATE_ROWS: readonly AutoloopGateKey[] = [
  "auto_confirm_config",
  "auto_confirm_env",
  "exec_live_config",
  "exec_live_env",
  "exec_derivatives_config",
  "exec_derivatives_env",
];
