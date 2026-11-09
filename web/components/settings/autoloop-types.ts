// Shape của GET/POST /api/autoloop, POST /api/autoloop/run-now, POST /api/halt,
// POST /api/halt/reset (server/autoloop.py) — server-configured automation modes.

export type AutoloopModes = {
  auto_spot: boolean;
  auto_futures: boolean;
  any_auto_enabled: boolean;
};

export type AutoloopModeKey = Exclude<keyof AutoloopModes, "any_auto_enabled">;

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

export type AutoloopModeCycle = {
  mode?: string;
  status?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  candidates?: string[];
  results?: AutoloopLastCycleResult[];
  error?: string | null;
  telemetry?: AutoloopTelemetry | null;
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
  modes?: Partial<Record<AutoloopModeKey, AutoloopModeCycle | null>> | null;
  active_mode?: string | null;
};

export type AutoloopPayload = {
  generated_at: string;
  enabled: boolean;
  interval_minutes: number;
  running_cycle: boolean;
  modes: AutoloopModes;
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

/** Thứ tự hiển thị của các chế độ tự động do server trả về. */
export const AUTOLOOP_MODE_ROWS: readonly AutoloopModeKey[] = [
  "auto_spot",
  "auto_futures",
];
