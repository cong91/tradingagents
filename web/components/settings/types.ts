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
  quick_think_provider: { label: "Provider tầng nhanh", hint: "Bỏ trống = theo Nhà cung cấp LLM. Dùng cho analyst/trader/risk." },
  deep_think_provider: { label: "Provider tầng sâu", hint: "Bỏ trống = theo Nhà cung cấp LLM. Dùng cho Research Manager/Portfolio Manager." },
  deep_think_llm: { label: "Mô hình suy luận sâu" },
  quick_think_llm: { label: "Mô hình suy luận nhanh" },
  backend_url: { label: "URL backend LLM", hint: "Bỏ trống để dùng endpoint mặc định của nhà cung cấp." },
  quick_think_backend_url: { label: "URL backend tầng nhanh", hint: "Bỏ trống = theo URL backend LLM." },
  deep_think_backend_url: { label: "URL backend tầng sâu", hint: "Bỏ trống = theo URL backend LLM." },
  llm_wire_protocol: { label: "Giao thức gọi LLM", hint: "chat (Chat Completions) hoặc responses (/responses)." },
  output_language: { label: "Ngôn ngữ đầu ra", hint: "Ví dụ: Vietnamese, English." },
  max_debate_rounds: { label: "Số vòng tranh luận nghiên cứu" },
  max_risk_discuss_rounds: { label: "Số vòng tranh luận rủi ro" },
  checkpoint_enabled: { label: "Bật checkpoint", hint: "Lưu trạng thái sau mỗi bước để chạy lại khi gặp sự cố." },
  benchmark_ticker: { label: "Ticker benchmark", hint: "Bỏ trống để tự dò theo sàn niêm yết (mặc định SPY)." },
  temperature: { label: "Temperature", hint: "Bỏ trống để dùng mặc định của nhà cung cấp." },
  llm_timeout: { label: "Timeout LLM (giây)", hint: "Thời gian tối đa cho một request; phải lớn hơn 0." },
  llm_max_retries: { label: "Số lần thử lại LLM", hint: "Bỏ trống để dùng mặc định của SDK." },
  max_tokens: { label: "Giới hạn token đầu ra", hint: "Bỏ trống để không giới hạn." },
  google_thinking_level: { label: "Mức tư duy Google", hint: "Ví dụ: high, minimal. Bỏ trống = mặc định." },
  openai_reasoning_effort: { label: "OpenAI reasoning effort", hint: "medium, high, low. Bỏ trống = mặc định." },
  anthropic_effort: { label: "Anthropic effort", hint: "high, medium, low. Bỏ trống = mặc định." },
  risk_max_daily_loss_pct: { label: "Lỗ tối đa trong ngày", unit: "%" },
  risk_max_position_pct_per_asset: { label: "Tỷ trọng vị thế tối đa mỗi tài sản", unit: "%" },
  risk_max_total_exposure_pct: { label: "Tổng mức phơi nhiễm tối đa", unit: "%" },
  risk_max_consecutive_loss_count: { label: "Giới hạn lệnh lỗ liên tiếp" },
  risk_max_derivatives_leverage: { label: "Đòn bẩy phái sinh tối đa", unit: "x", hint: "Trần RiskGuard. Agent đề xuất đòn bẩy thực tế; vượt trần này lệnh bị từ chối." },
  risk_max_derivatives_exposure_pct: { label: "Phơi nhiễm phái sinh tối đa", unit: "%", hint: "0 = chặn hoàn toàn giao dịch phái sinh." },
  risk_derivatives_max_risk_fraction: { label: "Rủi ro tối đa mỗi lệnh", hint: "Tỷ lệ equity chấp nhận mất khi SL khớp (0.02 = 2%). Đề xuất của agent vượt mức này bị từ chối." },
  risk_derivatives_max_margin_fraction: { label: "Ký quỹ tối đa mỗi lệnh", hint: "Tỷ lệ equity dùng làm margin ban đầu (0.5 = 50%). Đề xuất của agent vượt mức này bị từ chối." },
  risk_derivatives_risk_fraction: { label: "Rủi ro mặc định/lệnh", hint: "Chỉ dùng khi lệnh không có đề xuất của agent (CLI/script)." },
  risk_derivatives_margin_fraction: { label: "Ký quỹ mặc định/lệnh", hint: "Chỉ dùng khi lệnh không có đề xuất của agent (CLI/script)." },
  risk_derivatives_profit_protection_enabled: { label: "Tự khóa lợi nhuận Futures", hint: "Chỉ quản lý vị thế bot có state + bracket đã lưu. Không mở cổng LIVE/phái sinh." },
  risk_derivatives_break_even_r: { label: "Mốc hòa vốn", unit: "R", hint: "Dời SL về entry khi lợi nhuận đạt bội số R này." },
  risk_derivatives_lock_profit_r: { label: "Mốc khóa lợi nhuận", unit: "R", hint: "Từ mốc này, SL khóa tối thiểu 1R." },
  risk_derivatives_trail_start_r: { label: "Bắt đầu trailing", unit: "R", hint: "Trên mốc này, SL bám mark cách 1R theo cấu hình." },
  risk_derivatives_trail_distance_r: { label: "Khoảng trailing", unit: "R" },
  risk_derivatives_target_extension_r: { label: "Gia hạn TP", unit: "R", hint: "Khi mark vượt TP mà vị thế còn mở, đẩy target thêm từng bậc." },
  risk_derivatives_min_stop_move_r: { label: "Bước dời SL tối thiểu", unit: "R", hint: "Chỉ thay SL khi cải thiện ít nhất mức này; tránh huỷ/tạo lệnh liên tục vì nhiễu giá." },
  risk_derivatives_mark_max_age_seconds: { label: "Tuổi mark tối đa", unit: "giây", hint: "Mark cũ hơn mức này bị từ chối; monitor không thay lệnh trên giá đã lỗi thời." },
  risk_derivatives_scale_out_enabled: { label: "Chốt lời nhiều tầng", hint: "Bật: hai mức chốt theo R + phần còn lại chạy trailing. Tắt: một TP duy nhất." },
  risk_derivatives_scale_out_first_r: { label: "Mức chốt 1", unit: "R", hint: "Bội số R của tầng chốt lời đầu tiên." },
  risk_derivatives_scale_out_first_fraction: { label: "Tỷ lệ chốt 1", hint: "Tỷ lệ khối lượng vị thế ban đầu chốt tại mức 1 (0.33 = 33%)." },
  risk_derivatives_scale_out_second_r: { label: "Mức chốt 2", unit: "R", hint: "Bội số R của tầng chốt lời thứ hai; phải lớn hơn mức 1." },
  risk_derivatives_scale_out_second_fraction: { label: "Tỷ lệ chốt 2", hint: "Tỷ lệ khối lượng vị thế ban đầu chốt tại mức 2. Tổng hai tỷ lệ phải nhỏ hơn 1." },
  exec_exchange_id: { label: "ID sàn (ccxt)", hint: "Ví dụ: binance." },
  exec_quote_currency: { label: "Đồng tiền báo giá", hint: "Ví dụ: USDT." },
  exec_base_url: { label: "URL sàn đang dùng", hint: "Chỉ đọc. Đổi endpoint và đúng cặp API key/secret trong .env rồi khởi động lại backend để chuyển Demo/production." },
  exec_sandbox: { label: "Sandbox cho sàn khác Binance", hint: "Binance chọn Demo hoặc production bằng URL phía trên; chế độ này chỉ áp dụng các sàn khác." },
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
      "quick_think_provider",
      "deep_think_provider",
      "deep_think_llm",
      "quick_think_llm",
      "backend_url",
      "quick_think_backend_url",
      "deep_think_backend_url",
      "llm_wire_protocol",
      "output_language",
      "max_debate_rounds",
      "max_risk_discuss_rounds",
      "checkpoint_enabled",
      "benchmark_ticker",
      "temperature",
      "llm_timeout",
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
      "Ngưỡng RiskGuard dùng để chặn lệnh và chính sách bảo vệ lợi nhuận cho vị thế Futures do bot quản lý.",
    keys: [
      "risk_max_daily_loss_pct",
      "risk_max_position_pct_per_asset",
      "risk_max_total_exposure_pct",
      "risk_max_consecutive_loss_count",
      "risk_max_derivatives_leverage",
      "risk_max_derivatives_exposure_pct",
      "risk_derivatives_max_risk_fraction",
      "risk_derivatives_max_margin_fraction",
      "risk_derivatives_risk_fraction",
      "risk_derivatives_margin_fraction",
      "risk_derivatives_profit_protection_enabled",
      "risk_derivatives_break_even_r",
      "risk_derivatives_lock_profit_r",
      "risk_derivatives_trail_start_r",
      "risk_derivatives_trail_distance_r",
      "risk_derivatives_target_extension_r",
      "risk_derivatives_min_stop_move_r",
      "risk_derivatives_mark_max_age_seconds",
      "risk_derivatives_scale_out_enabled",
      "risk_derivatives_scale_out_first_r",
      "risk_derivatives_scale_out_first_fraction",
      "risk_derivatives_scale_out_second_r",
      "risk_derivatives_scale_out_second_fraction",
    ],
  },
  {
    id: "exec",
    title: "Thực thi & Sàn",
    description:
      "Sàn, đồng tiền báo giá và venue URL. Binance mặc định dùng Demo; chuyển sang production phải đổi URL cùng cặp key/secret trong .env. Công tắc LIVE là cổng riêng, chỉ-đọc ở đây.",
    keys: ["exec_exchange_id", "exec_quote_currency", "exec_base_url", "exec_sandbox"],
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
