// Shape của GET/PUT /api/settings (docs/ui-api-contract.md §1) — mirror của
// server/settings.py, không thêm gì ngoài hợp đồng. Nhóm hiển thị sống ở đây;
// nhãn/gợi ý theo từng key nằm ở lib/i18n/messages/settings.ts (en + vi).

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

export type SettingGroup = {
  /** Khoá vào t.settings.groups (tiêu đề + mô tả theo locale). */
  id: "llm" | "risk" | "exec";
  keys: readonly string[];
};

export const SETTING_GROUPS: readonly SettingGroup[] = [
  {
    id: "llm",
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
