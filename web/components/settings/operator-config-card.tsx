"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { LoaderCircleIcon, RotateCcwIcon, SaveIcon, ShieldAlertIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatDateTimeVi } from "./format-time";
import { useT, type Messages } from "@/lib/i18n";
import {
  operatorConfigApi,
  OperatorConfigApiError,
  toOperatorError,
} from "./operator-config-api";
import {
  changedKeys,
  draftFrom,

  type OperatorConfigPayload,
  type OperatorDraft,
  type OperatorSettingRow,
} from "./operator-config-types";

type Feedback = { role: "status" | "alert"; text: string };

function formatValue(
  t: Messages,
  row: OperatorSettingRow,
  value: string | number | boolean | null
): string {
  if (row.type === "bool") return value === true ? t.common.on : t.common.off;
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function RowField({
  row,
  value,
  disabled,
  onChange,
}: {
  row: OperatorSettingRow;
  value: string | boolean;
  disabled: boolean;
  onChange: (next: string | boolean) => void;
}) {
  const t = useT();
  const id = `operator-${row.key}`;
  if (row.type === "bool") {
    return (
      <label htmlFor={id} className="inline-flex min-touch cursor-pointer items-center gap-2">
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className="size-6 cursor-pointer accent-primary disabled:cursor-not-allowed"
        />
        <span className="text-sm text-muted-foreground">{value === true ? t.common.on : t.common.off}</span>
      </label>
    );
  }
  return (
    <input
      id={id}
      type="text"
      inputMode="decimal"
      value={typeof value === "string" ? value : ""}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      placeholder={formatValue(t, row, row.default)}
      className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60"
    />
  );
}

export function OperatorConfigCard() {
  const t = useT();
  const c = t.settings.operator;
  const [payload, setPayload] = useState<OperatorConfigPayload | null>(null);
  const [draft, setDraft] = useState<OperatorDraft>({});
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [errorMessage, setErrorMessage] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmGate, setConfirmGate] = useState(false);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    try {
      const next = await operatorConfigApi.get();
      setPayload(next);
      setDraft(draftFrom(next));
      setPhase("ready");
      setErrorMessage("");
    } catch (err) {
      setPhase("error");
      setErrorMessage(toOperatorError(err).message);
    }
  }, []);

  useEffect(() => {
    // Deferred like autoloop-card: the first fetch must not set state during
    // the effect body itself (react-hooks/set-state-in-effect).
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  const changed = useMemo(
    () => (payload ? changedKeys(payload, draft) : []),
    [payload, draft]
  );
  const gateChanges = useMemo(
    () => (payload ? changed.filter((key) => payload.settings.find((r) => r.key === key)?.gate) : []),
    [payload, changed]
  );
  const needsConfirm = gateChanges.length > 0;

  const groups = useMemo(() => {
    if (!payload) return [];
    const order: string[] = [];
    const byGroup = new Map<string, OperatorSettingRow[]>();
    for (const row of payload.settings) {
      if (!byGroup.has(row.group)) {
        byGroup.set(row.group, []);
        order.push(row.group);
      }
      byGroup.get(row.group)!.push(row);
    }
    return order.map((id) => ({ id, rows: byGroup.get(id)! }));
  }, [payload]);

  const save = async () => {
    if (!payload || changed.length === 0) return;
    const settings: Record<string, string | boolean> = {};
    for (const key of changed) {
      const row = payload.settings.find((item) => item.key === key);
      const value = draft[key];
      if (!row) continue;
      settings[key] = row.type === "bool" ? value === true : String(value).trim();
    }
    setSaving(true);
    setFeedback(null);
    try {
      const next = await operatorConfigApi.update(settings, {
        confirm: needsConfirm ? confirmGate : undefined,
        reason: needsConfirm ? reason.trim() : undefined,
      });
      setPayload(next);
      setDraft(draftFrom(next));
      setConfirmGate(false);
      setReason("");
      setFeedback({
        role: "status",
        text: c.saved(changed.length),
      });
    } catch (err) {
      const apiError = err instanceof OperatorConfigApiError ? err : toOperatorError(err);
      setFeedback({ role: "alert", text: apiError.message });
    } finally {
      setSaving(false);
    }
  };

  const reset = async (key: string) => {
    setSaving(true);
    setFeedback(null);
    try {
      const next = await operatorConfigApi.reset(key);
      setPayload(next);
      setDraft(draftFrom(next));
      setFeedback({ role: "status", text: c.resetDone(key) });
    } catch (err) {
      const apiError = err instanceof OperatorConfigApiError ? err : toOperatorError(err);
      setFeedback({ role: "alert", text: apiError.message });
    } finally {
      setSaving(false);
    }
  };

  if (phase === "loading") {
    return (
      <Card aria-busy="true">
        <CardHeader>
          <CardTitle>{c.title}</CardTitle>
          <CardDescription>{c.loading}</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (phase === "error" || !payload) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlertIcon className="size-5 shrink-0 text-destructive" aria-hidden="true" />
            {c.loadFailed}
          </CardTitle>
          <CardDescription>{errorMessage}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button className="min-touch" onClick={() => void load()}>
            {t.common.retry}
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlertIcon className="size-4" aria-hidden="true" />
          {c.titleFull}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
        <CardAction>
          <Badge variant="outline">{c.mode(payload.effective_exec_mode)}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-5">
        {groups.map(({ id, rows }) => {
          const meta = c.groups[id];
          return (
            <section key={id} className="space-y-2">
              <div>
                <h3 className="text-sm font-semibold">{meta?.title ?? id}</h3>
                {meta?.description ? (
                  <p className="mt-0.5 text-xs text-muted-foreground">{meta.description}</p>
                ) : null}
              </div>
              <ul className="divide-y rounded-lg border">
                {rows.map((row) => (
                  <li
                    key={row.key}
                    className="grid gap-2 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,220px)] sm:items-center sm:gap-4"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium">{row.label}</span>
                        {row.override ? (
                          <Badge variant="secondary">{c.overridden}</Badge>
                        ) : (
                          <Badge variant="outline">{c.default}</Badge>
                        )}
                        {row.gate ? <Badge variant="destructive">{c.gate}</Badge> : null}
                      </div>
                      <p className="mt-0.5 text-xs text-muted-foreground">{row.hint}</p>
                      <p className="mt-0.5 font-mono text-[11px] text-muted-foreground/80">
                        {c.rowMeta(
                          row.key,
                          formatValue(t, row, row.value),
                          formatValue(t, row, row.default),
                          row.updated_at ? formatDateTimeVi(row.updated_at) : null
                        )}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1">
                        <RowField
                          row={row}
                          value={draft[row.key] ?? ""}
                          disabled={saving}
                          onChange={(next) =>
                            setDraft((current) => ({ ...current, [row.key]: next }))
                          }
                        />
                      </span>
                      {row.override ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="min-touch shrink-0"
                          disabled={saving}
                          onClick={() => void reset(row.key)}
                          aria-label={c.resetAria(row.label)}
                        >
                          <RotateCcwIcon aria-hidden="true" />
                        </Button>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}

        {needsConfirm ? (
          <div className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-sm font-medium">
              {c.gateChanges(gateChanges.join(", "))}
            </p>
            <p className="text-xs text-muted-foreground">{c.gateWarning}</p>
            <label className="flex min-touch items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={confirmGate}
                onChange={(event) => setConfirmGate(event.target.checked)}
                className="size-5 accent-destructive"
              />
              {c.confirm}
            </label>
            <input
              type="text"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={c.reasonPlaceholder}
              className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>
        ) : null}

        {feedback ? (
          <p
            role={feedback.role === "alert" ? "alert" : "status"}
            className={
              feedback.role === "alert" ? "text-sm text-destructive" : "text-sm text-muted-foreground"
            }
          >
            {feedback.text}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button
            className="min-touch"
            disabled={
              saving ||
              changed.length === 0 ||
              (needsConfirm && (!confirmGate || reason.trim().length === 0))
            }
            onClick={() => void save()}
          >
            {saving ? (
              <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
            ) : (
              <SaveIcon aria-hidden="true" />
            )}
            {c.save(changed.length)}
          </Button>
          <Button
            variant="ghost"
            className="min-touch"
            disabled={saving || changed.length === 0}
            onClick={() => setDraft(draftFrom(payload))}
          >
            {c.undo}
          </Button>
          <span className="text-xs text-muted-foreground">
            {c.updatedAt(formatDateTimeVi(payload.generated_at))}

          </span>
        </div>
      </CardContent>
    </Card>
  );
}
