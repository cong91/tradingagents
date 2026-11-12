import { SETTING_GROUPS, type SettingRow } from "./types";

export type SettingsArea = "automation" | "ai" | "risk" | "connections" | "system";

export const SETTINGS_AREAS: readonly SettingsArea[] = [
  "automation", "ai", "risk", "connections", "system",
];

export function areaForHash(hash: string): SettingsArea | null {
  const value = hash.replace(/^#/, "");
  if (value === "exec-gates" || value === "operator-config") return "automation";
  return SETTINGS_AREAS.find((area) => area === value) ?? null;
}

export function areaForSetting(key: string): SettingsArea {
  if (key === "benchmark_ticker" || key === "checkpoint_enabled") return "system";
  if (key === "quick_think_fallback_models" || key === "deep_think_fallback_models" || key === "max_tool_rounds") return "ai";
  // The Kronos profile is displayed with its read-only readiness in the AI area.
  if (key.startsWith("kronos_active_")) return "ai";
  const group = SETTING_GROUPS.find((candidate) => candidate.keys.includes(key));
  if (group?.id === "llm") return "ai";
  if (group?.id === "exec") return "connections";
  if (group?.id === "risk" || key.startsWith("risk_")) return "risk";
  return "system";
}

export function operatorOwnershipConsistent(
  settings: readonly { key: string }[],
  allowlist: readonly string[],
): boolean {
  const keys = settings.map((row) => row.key);
  return keys.length === new Set(keys).size &&
    keys.every((key) => allowlist.includes(key)) &&
    allowlist.every((key) => keys.includes(key));
}

export function visibleSettingsRows(
  rows: readonly SettingRow[],
  operatorAllowlist: readonly string[] | null,
  operatorKeys: readonly string[] = [],
): SettingRow[] {
  return rows.filter((row) => {
    if (operatorAllowlist === null) return !row.key.startsWith("risk_") && !operatorKeys.includes(row.key);
    return !operatorAllowlist.includes(row.key) && !operatorKeys.includes(row.key);
  });
}
