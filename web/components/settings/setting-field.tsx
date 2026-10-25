"use client";

import { useT, type Messages } from "@/lib/i18n";
import type { SettingRow, SettingValue } from "./types";
import { fieldTypeOf } from "./use-settings";

function formatDefault(t: Messages, row: SettingRow): string {
  if (fieldTypeOf(row.type) === "bool")
    return row.default === true ? t.settings.field.defaultOn : t.settings.field.defaultOff;
  if (row.default === null || row.default === undefined) return t.settings.field.defaultEmpty;
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
  const t = useT();
  const meta = t.settings.settingMeta[row.key];
  const label = meta?.label ?? row.key;
  const id = `setting-${row.key}`;
  const fieldType = fieldTypeOf(row.type);
  const describedBy = error ? `${id}-error` : undefined;

  return (
    <div className="grid gap-2 border-t py-3 first:border-t-0 sm:grid-cols-[minmax(0,1fr)_minmax(0,340px)] sm:items-center sm:gap-4">
      <div className="min-w-0">
        {!row.editable ? (
          <span className="text-sm font-medium">{label}</span>
        ) : (
          <label htmlFor={id} className="text-sm font-medium">
            {label}
          </label>
        )}
        {meta?.hint ? (
          <p className="mt-0.5 text-xs text-muted-foreground">{meta.hint}</p>
        ) : null}
        <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/80">
          {row.env_var} · {t.settings.field.defaultLabel(formatDefault(t, row))}
        </p>
      </div>
      <div className="flex items-center gap-2">
        {!row.editable ? (
          <span className="min-w-0 flex-1 break-all rounded-md border border-border bg-muted/40 px-3 py-2 font-mono text-xs text-muted-foreground">
            {value === null || value === undefined ? "—" : String(value)}
          </span>
        ) : fieldType === "bool" ? (
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
              {value === true ? t.common.on : t.common.off}
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
              placeholder={formatDefault(t, row)}

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
