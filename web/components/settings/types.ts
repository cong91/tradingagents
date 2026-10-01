// Shape của GET/PUT /api/settings (docs/ui-api-contract.md §1) — mirror của
// server/settings.py, không thêm gì ngoài hợp đồng. Nhóm hiển thị và nhãn
// tiếng Việt sống ở đây vì chúng ánh xạ 1-1 theo key mà hợp đồng trả về.

export type SettingRow = {
  key: string;
  env_var: string;
  value: string | number | boolean | null;
  default: string | number | boolean | null;
  type: string;
  editable: boolean;
  source: string;
};

export type CredentialRow = {
  env_var: string;
  configured: boolean;
  masked: string | null;
};

export type SettingsPayload = {
  generated_at: string;
  settings: SettingRow[];
  credentials: CredentialRow[];
  gates_readonly: Record<string, boolean>;
  effective_exec_mode: string;
  applied_at?: string;
};

/** Giá trị form: boolean cho công tắc, chuỗi cho ô văn bản/số ("" = bỏ trống). */
export type SettingValue = string | boolean;

export type SettingMeta = {
  label: string;
  hint?: string;
  unit?: string;
};

export const SETTING_META: Record<string, SettingMeta> = {
  llm_provider: { label: "Nhà cung cấp LLM", hint: "openai, google, anthropic hoặc endpoint tương thích." },
  quick_provider: { label: "Provider tầng nhanh", hint: "Bỏ trống = theo Nhà cung cấp LLM. Dùng cho analyst/trader/risk." },
  deep_provider: { label: "Provider tầng sâu", hint: "Bỏ trống = theo Nhà cung cấp LLM. Dùng cho Research Manager/Portfolio Manager." },
  deep_think_llm: { label: "Mô hình suy luận sâu" },
  quick_think_llm: { label: "Mô hình suy luận nhanh" },
  backend_url: { label: "URL backend LLM", hint: "Bỏ trống để dùng endpoint mặc định của nhà cung cấp." },
  quick_backend_url: { label: "URL backend tầng nhanh", hint: "Bỏ trống = theo URL backend LLM." },
  deep_backend_url: { label: "URL backend tầng sâu", hint: "Bỏ trống = theo URL backend LLM." },
  llm_wire_protocol: { label: "Giao thức gọi LLM", hint: "chat (Chat Completions) hoặc responses (/responses)." },
  output_language: { label: "Ngôn ngữ đầu ra", hint: "Ví dụ: Vietnamese, English." },
  max_debate_rounds: { label: "Số vòng tranh luận nghiên cứu" },
  max_risk_discuss_rounds: { label: "Số vòng tranh luận rủi ro" },
  checkpoint_enabled: { label: "Bật checkpoint", hint: "Lưu trạng thái sau mỗi bước để chạy lại khi gặp sự cố." },
  benchmark_ticker: { label: "Ticker benchmark", hint: "Bỏ trống để tự dò theo sàn niêm yết (mặc định SPY)." },
  temperature: { label: "Temperature", hint: "Bỏ trống để dùng mặc định của nhà cung cấp." },
  llm_max_retries: { label: "Số lần thử lại LLM", hint: "Bỏ trống để dùng mặc định của SDK." },
  max_tokens: { label: "Giới hạn token đầu ra", hint: "Bỏ trống để không giới hạn." },
  google_thinking_level: { label: "Mức tư duy Google", hint: "Ví dụ: high, minimal. Bỏ trống = mặc định." },
  openai_reasoning_effort: { label: "OpenAI reasoning effort", hint: "medium, high, low. Bỏ trống = mặc định." },
  anthropic_effort: { label: "Anthropic effort", hint: "high, medium, low. Bỏ trống = mặc định." },
  risk_max_daily_loss_pct: { label: "Lỗ tối đa trong ngày", unit: "%" },
  risk_max_position_pct_per_asset: { label: "Tỷ trọng vị thế tối đa mỗi tài sản", unit: "%" },
  risk_max_total_exposure_pct: { label: "Tổng mức phơi nhiễm tối đa", unit: "%" },
  risk_max_consecutive_loss_count: { label: "Giới hạn lệnh lỗ liên tiếp" },
  risk_max_derivatives_leverage: { label: "Đòn bẩy phái sinh tối đa", unit: "x" },
  risk_max_derivatives_exposure_pct: { label: "Phơi nhiễm phái sinh tối đa", unit: "%", hint: "0 = chặn hoàn toàn giao dịch phái sinh." },
  exec_exchange_id: { label: "ID sàn (ccxt)", hint: "Ví dụ: binance." },
  exec_quote_currency: { label: "Đồng tiền báo giá", hint: "Ví dụ: USDT." },
  exec_sandbox: { label: "Chế độ sandbox (testnet)", hint: "Chuyển kết nối sang testnet của sàn — hướng an toàn." },
};

export const PATH_LABELS: Record<string, string> = {
  results_dir: "Thư mục kết quả run",
  data_cache_dir: "Bộ nhớ đệm dữ liệu",
  memory_log_path: "Nhật ký quyết định",
  exec_log_path: "Nhật ký audit thực thi",
};

// Hợp đồng §1: các cổng FR5/FR-S2/FR-D là read-only — không ghi được qua API.
export const GATE_LABELS: Record<string, string> = {
  exec_live_config: "LIVE · cấu hình exec_live",
  TRADINGAGENTS_EXEC_LIVE: "LIVE · biến môi trường TRADINGAGENTS_EXEC_LIVE",
  exec_auto_confirm_config: "Tự động duyệt · cấu hình exec_auto_confirm",
  TRADINGAGENTS_EXEC_AUTO_CONFIRM: "Tự động duyệt · biến môi trường",
  exec_derivatives_config: "Phái sinh · cấu hình exec_derivatives",
  TRADINGAGENTS_EXEC_DERIVATIVES: "Phái sinh · biến môi trường",
};

export type SettingGroup = {
  id: string;
  title: string;
  description: string;
  keys: readonly string[];
};

export const SETTING_GROUPS: readonly SettingGroup[] = [
  {
    id: "llm",
    title: "LLM & Mô hình",
    description:
      "Nhà cung cấp, mô hình, vòng tranh luận và tham số suy luận. Áp dụng cho phiên phân tích mới.",
    keys: [
      "llm_provider",
      "quick_provider",
      "deep_provider",
      "deep_think_llm",
      "quick_think_llm",
      "backend_url",
      "quick_backend_url",
      "deep_backend_url",
      "llm_wire_protocol",
      "output_language",
      "max_debate_rounds",
      "max_risk_discuss_rounds",
      "checkpoint_enabled",
      "benchmark_ticker",
      "temperature",
      "llm_max_retries",
      "max_tokens",
      "google_thinking_level",
      "openai_reasoning_effort",
      "anthropic_effort",
    ],
  },
  {
    id: "risk",
    title: "Giới hạn rủi ro",
    description:
      "Ngưỡng RiskGuard dùng để chặn lệnh. Giá trị mới chỉ áp cho guard được dựng sau khi lưu.",
    keys: [
      "risk_max_daily_loss_pct",
      "risk_max_position_pct_per_asset",
      "risk_max_total_exposure_pct",
      "risk_max_consecutive_loss_count",
      "risk_max_derivatives_leverage",
      "risk_max_derivatives_exposure_pct",
    ],
  },
  {
    id: "exec",
    title: "Thực thi & Sàn",
    description:
      "Sàn, đồng tiền báo giá và sandbox. Công tắc LIVE là cổng chỉ-đọc — xem thẻ “Cổng thực thi”.",
    keys: ["exec_exchange_id", "exec_quote_currency", "exec_sandbox"],
  },
];

export type GroupedRows = {
  groups: { group: SettingGroup; rows: SettingRow[] }[];
  otherRows: SettingRow[];
  pathRows: SettingRow[];
};

/** Chia rows trả về theo nhóm chuẩn; key lạ vào "other", row không editable vào paths. */
export function groupRows(payload: SettingsPayload): GroupedRows {
  const byKey = new Map(payload.settings.map((row) => [row.key, row]));
  const used = new Set<string>();
  const groups = SETTING_GROUPS.map((group) => {
    const rows = group.keys
      .map((key) => byKey.get(key))
      .filter((row): row is SettingRow => Boolean(row));
    for (const row of rows) used.add(row.key);
    return { group, rows };
  });
  const otherRows: SettingRow[] = [];
  const pathRows: SettingRow[] = [];
  for (const row of payload.settings) {
    if (used.has(row.key)) continue;
    if (row.editable) otherRows.push(row);
    else pathRows.push(row);
  }
  return { groups, otherRows, pathRows };
}
