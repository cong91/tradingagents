// Shape của GET/PUT /api/operator-config (server/operator_config.py).
// Đây là các cấu hình vận hành lưu trong PostgreSQL: chế độ tự động và
// các trần rủi ro. Chúng nằm ngoài allowlist của /api/settings.

export type OperatorSettingType = "bool" | "float" | "timeframe";

export type OperatorGateKey = "auto_spot_enabled" | "auto_futures_enabled";

export type OperatorGates = {
  auto_spot: boolean;
  auto_futures: boolean;
  any_auto_enabled: boolean;
};

export const OPERATOR_GATE_KEYS: readonly OperatorGateKey[] = [
  "auto_spot_enabled",
  "auto_futures_enabled",
];

export type OperatorSettingRow = {
  key: string;
  type: OperatorSettingType;
  options?: string[] | null;
  group: "gates" | "risk" | string;
  label: string;
  hint: string;
  gate: boolean;
  /** Giá trị đang có hiệu lực trong process (override hoặc default). */
  value: string | number | boolean | null;
  /** Giá trị mặc định của engine khi không có override. */
  default: string | number | boolean | null;
  /** Có row override trong PostgreSQL hay không. */
  override: boolean;
  stored_value: string | number | boolean | null;
  updated_at: string | null;
  updated_by: string | null;
};

export type OperatorConfigPayload = {
  generated_at: string;
  settings: OperatorSettingRow[];
  gates: OperatorGates;
  effective_exec_mode: string;
  kronos?: KronosStatus | null;
  allowlist: string[];
  applied_at?: string;
};

/** Giá trị form: boolean cho công tắc, chuỗi cho ô số ("" = bỏ trống). */
export type OperatorValue = string | boolean;

export type OperatorDraft = Record<string, OperatorValue>;

export type OperatorSection = "modes" | "risk" | "forecast";
export type OperatorRiskGroup = "general" | "futures" | "protection" | "advanced";

/** Each operator row belongs to exactly one editable section. */
export function operatorSection(row: OperatorSettingRow): OperatorSection {
  if (row.group === "forecast") return "forecast";
  if (row.group === "modes") return "modes";
  return row.gate ? "modes" : "risk";
}

/** Classify known tasks without hiding new keys returned by the server. */
export function operatorRiskGroup(key: string): OperatorRiskGroup {
  if (key === "risk_max_open_heat_pct") return "general";
  if (
    key === "risk_max_derivatives_leverage" ||
    key === "risk_max_derivatives_exposure_pct"
  ) return "futures";
  if (key === "risk_max_derivatives_exposure_hard_pct" || key.startsWith("risk_derivatives_override_")) {
    return "advanced";
  }
  if (key.startsWith("risk_derivatives_")) return "protection";
  return "advanced";
}

/** Convert form text to the finite, non-negative number required by the API. */
export function parseOperatorNumber(value: OperatorValue | undefined): number | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  const number = Number(value.trim().replace(",", "."));
  return Number.isFinite(number) && number >= 0 ? number : null;
}

/** Key là cổng thực thi → cần confirm + reason khi ghi. */
export function isGateKey(row: OperatorSettingRow): boolean {
  return row.gate;
}

/** True when the server row is one of the two market automation gates. */
export function isOperatorGateKey(key: string): key is OperatorGateKey {
  return key === OPERATOR_GATE_KEYS[0] || key === OPERATOR_GATE_KEYS[1];
}

/** Collect server-declared gates for confirmation and audit. */
export function operatorGateKeys(settings: readonly OperatorSettingRow[]): Set<string> {
  return new Set(settings.filter((row) => row.gate).map((row) => row.key));
}

export type KronosProfileId = "small_base" | "mini_2k";

export type KronosStatus = {
  profile_id: KronosProfileId;
  timeframe: string;
  horizon_hours: number | null;
  lookback_bars: number | null;
  max_context: number | null;
  paths: number | null;
  temperature: number | null;
  readiness: { status: "ready" | "unavailable"; reason: string | null };
  model: {
    source_revision: string;
    model_revision: string;
    repo: string;
    tokenizer_repo: string;
    tokenizer_revision: string;
  };
};

/** Giá trị form ban đầu từ payload: dùng value đang hiệu lực. */
export function draftFrom(payload: OperatorConfigPayload): OperatorDraft {
  const draft: OperatorDraft = {};
  for (const row of payload.settings) {
    if (row.type === "bool") draft[row.key] = row.value === true;
    else draft[row.key] = row.value === null || row.value === undefined ? "" : String(row.value);
  }
  return draft;
}

/** Chỉ gửi các key thực sự khác giá trị đang hiệu lực. */
export function changedKeys(
  payload: OperatorConfigPayload,
  draft: OperatorDraft
): string[] {
  const changed: string[] = [];
  for (const row of payload.settings) {
    const next = draft[row.key];
    if (row.type === "bool") {
      if ((next === true) !== (row.value === true)) changed.push(row.key);
      continue;
    }
    const nextText = typeof next === "string" ? next.trim() : "";
    const currentText =
      row.value === null || row.value === undefined ? "" : String(row.value);
    if (nextText !== currentText) changed.push(row.key);
  }
  return changed;
}

// Tiêu đề/mô tả của từng nhóm: lib/i18n/messages/settings.ts (operator.groups).
