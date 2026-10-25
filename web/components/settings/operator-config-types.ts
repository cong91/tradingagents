// Shape của GET/PUT /api/operator-config (server/operator_config.py).
// Đây là các override vận hành lưu trong PostgreSQL: nửa cấu hình của các cổng
// thực thi và vài trần rủi ro. Chúng KHÔNG thay thế /api/settings — mỗi key ở
// đây đều nằm ngoài allowlist của settings vì settings cố ý không ghi được cổng.

export type OperatorSettingType = "bool" | "float";

export type OperatorSettingRow = {
  key: string;
  type: OperatorSettingType;
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
  gates: Record<string, boolean>;
  effective_exec_mode: string;
  allowlist: string[];
  applied_at?: string;
};

/** Giá trị form: boolean cho công tắc, chuỗi cho ô số ("" = bỏ trống). */
export type OperatorValue = string | boolean;

export type OperatorDraft = Record<string, OperatorValue>;

/** Key là cổng thực thi → cần confirm + reason khi ghi. */
export function isGateKey(row: OperatorSettingRow): boolean {
  return row.gate;
}

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
