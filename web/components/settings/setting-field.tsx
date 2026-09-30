"use client";

import { SETTING_META, type SettingRow, type SettingValue } from "./types";
import { fieldTypeOf } from "./use-settings";

function formatDefault(row: SettingRow): string {
  if (fieldTypeOf(row.type) === "bool") return row.default === true ? "bật" : "tắt";
  if (row.default === null || row.default === undefined) return "trống";
  return String(row.default);
}

export function SettingField({
  row,
  value,
  error,
  onChange,
}: {
  row: SettingRow;
  value: SettingValue;
  error?: string;
  onChange: (value: SettingValue) => void;
}) {
  const meta = SETTING_META[row.key];
  const label = meta?.label ?? row.key;
  const id = `setting-${row.key}`;
  const fieldType = fieldTypeOf(row.type);
  const describedBy = error ? `${id}-error` : undefined;

  return (
    <div className="grid gap-2 border-t py-3 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_minmax(0,340px)] sm:items-center sm:gap-4">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium">
          {label}
        </label>
        {meta?.hint ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{meta.hint}</p>
        ) : null}
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/80">
          {row.env_var} · mặc định: {formatDefault(row)}
        </p>
      </div>
      <div className="flex items-center gap-2">
        {fieldType === "bool" ? (
          // Mục tiêu chạm = cả nhãn (min-touch ≥44px), không chỉ ô checkbox.
          <label
            htmlFor={id}
            className="inline-flex min-touch cursor-pointer items-center gap-2"
          >
            <input
              id={id}
              type="checkbox"
              checked={value === true}
              onChange={(event) => onChange(event.target.checked)}
              aria-describedby={describedBy}
              className="size-6 cursor-pointer accent-primary"
            />
            <span className="text-sm text-muted-foreground">
              {value === true ? "Bật" : "Tắt"}
            </span>
          </label>
        ) : (
          <span className="flex min-w-0 flex-1 items-center gap-2">
            <input
              id={id}
              type="text"
              inputMode={fieldType === "text" ? "text" : "decimal"}
              value={value === null || value === undefined ? "" : String(value)}
              onChange={(event) => onChange(event.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={describedBy}
              placeholder={formatDefault(row)}
              className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-[invalid=true]:border-destructive"
            />
            {meta?.unit ? (
              <span className="shrink-0 text-sm text-muted-foreground">{meta.unit}</span>
            ) : null}
          </span>
        )}
        {row.source === "env" ? (
          <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground">
            env
          </span>
        ) : null}
      </div>
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive sm:col-span-2">
          {error}
        </p>
      ) : null}
    </div>
  );
}
