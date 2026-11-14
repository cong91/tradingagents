"use client";

import { useEffect, useRef } from "react";
import { LoaderCircleIcon, RotateCcwIcon, SaveIcon, ShieldAlertIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useT, type Messages } from "@/lib/i18n";
import { formatDateTimeVi } from "./format-time";
import { type OperatorConfigController } from "./operator-config-controller";
import {
  isOperatorGateKey,
  operatorGateKeys,
  operatorRiskGroup,
  operatorSection,
  type OperatorRiskGroup,
  type OperatorSettingRow,
  type OperatorValue,
} from "./operator-config-types";

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
  rowLabel,
  value,
  disabled,
  switchControl,
  invalid,
  unit,
  describedBy,
  onChange,
}: {
  row: OperatorSettingRow;
  rowLabel: string;
  value: OperatorValue;
  disabled: boolean;
  switchControl: boolean;
  invalid: boolean;
  unit?: string;
  describedBy?: string;
  onChange: (next: OperatorValue) => void;
}) {
  const t = useT();
  const id = `operator-${row.key}`;
  if (row.type === "bool") {
    return (
      <label htmlFor={id} className="inline-flex min-touch cursor-pointer items-center gap-2">
        <input
          id={id}
          type="checkbox"
          role={switchControl ? "switch" : undefined}
          aria-label={t.settings.operator.draftSwitchAria(rowLabel)}
          aria-describedby={describedBy}
          checked={value === true}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className={switchControl ? "peer sr-only" : "size-6 cursor-pointer accent-primary disabled:cursor-not-allowed"}
        />
        {switchControl ? (
          <span
            aria-hidden="true"
            className="relative h-6 w-11 shrink-0 rounded-full border border-input bg-muted transition-colors after:absolute after:left-0.5 after:top-0.5 after:size-4 after:rounded-full after:bg-background after:transition-transform peer-checked:bg-primary peer-checked:after:translate-x-5 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring peer-disabled:cursor-not-allowed peer-disabled:opacity-60"
          />
        ) : null}
        <span className="text-sm text-muted-foreground">
          {switchControl ? `${t.settings.operator.draft}: ` : ""}
          {value === true ? t.common.on : t.common.off}
        </span>
      </label>
    );
  }
  if (row.type === "timeframe") {
    return (
      <select
        id={id}
        value={typeof value === "string" ? value : ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        aria-describedby={describedBy}
        className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm"
      >
        {(row.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    );
  }
  return (
    <div className="flex min-w-0 items-center gap-2">
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={typeof value === "string" ? value : ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        placeholder={formatValue(t, row, row.default)}
        className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60 aria-[invalid=true]:border-destructive"
      />
      {unit ? <span id={`${id}-unit`} className="shrink-0 text-sm text-muted-foreground">{unit}</span> : null}
    </div>
  );
}

function OperatorRow({ row, controller }: { row: OperatorSettingRow; controller: OperatorConfigController }) {
  const t = useT();
  const c = t.settings.operator;
  const meta = t.settings.settingMeta[row.key];
  const rowLabel = row.gate ? t.settings.gateLabels[row.key] ?? row.label : meta?.label ?? row.label;
  const rowHint = row.gate ? t.settings.gateHints[row.key] ?? row.hint : meta?.hint ?? row.hint;
  const switchControl = row.gate;
  const pending = controller.changed.includes(row.key);
  const invalid = controller.invalidKeys.includes(row.key);
  const id = `operator-${row.key}`;
  const unit = !row.gate && row.type !== "bool" ? meta?.unit : undefined;
  const describedBy = [
    !row.gate && rowHint ? `${id}-hint` : null,
    switchControl ? `${id}-status` : null,
    unit ? `${id}-unit` : null,
    invalid && row.type !== "bool" ? `${id}-error` : null,
  ].filter(Boolean).join(" ") || undefined;
  return (
    <li className={
      switchControl
        ? "grid gap-2 bg-primary/5 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,220px)] sm:items-center sm:gap-4"
        : "grid gap-2 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,220px)] sm:items-center sm:gap-4"
    }>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={id} className="text-sm font-medium">{rowLabel}</label>
          <Badge variant={row.override ? "secondary" : "outline"}>
            {row.override ? c.overridden : c.noOverride}
          </Badge>
          {row.gate ? (
            <Badge variant="outline">
              {isOperatorGateKey(row.key) ? c.gate : c.contextSwitch}
            </Badge>
          ) : null}
        </div>
        <p id={!row.gate && rowHint ? `${id}-hint` : undefined} className="mt-0.5 text-xs text-muted-foreground">{rowHint}</p>
        {switchControl ? (
          <div id={`${id}-status`} className="mt-1 flex flex-wrap gap-2">
            <Badge variant="outline">{c.currentState(formatValue(t, row, row.value))}</Badge>
            {pending ? <Badge variant="secondary">{c.pendingSave}</Badge> : null}
          </div>
        ) : null}
        <p className="mt-0.5 text-xs text-muted-foreground/80">
          {c.effectiveDefault(formatValue(t, row, row.value), formatValue(t, row, row.default))}
        </p>
      </div>
      <div className="flex min-w-0 items-center gap-2">
        <div className="min-w-0 flex-1">
          <RowField
            row={row}
            rowLabel={rowLabel}
            value={controller.draft[row.key] ?? ""}
            disabled={controller.saving}
            switchControl={switchControl}
            invalid={invalid}
            unit={unit}
            describedBy={describedBy}
            onChange={(next) => controller.setValue(row.key, next)}
          />
          {invalid ? (
            <p id={`${id}-error`} role="alert" className="mt-1 text-xs text-destructive">
              {controller.invalidMessages[row.key] ?? c.invalidNumber}
            </p>
          ) : null}
        </div>
        {row.override && !row.gate && !isOperatorGateKey(row.key) ? (
          <Button
            variant="ghost"
            size="sm"
            className="min-touch shrink-0"
            disabled={controller.saving || controller.refreshing || controller.serverAhead || controller.refreshFailed || controller.dirtyCount > 0}
            onClick={() => void controller.reset(row.key)}
            aria-label={c.resetAria(rowLabel)}
            title={controller.dirtyCount > 0 ? c.resetDraftWarning : c.resetAria(rowLabel)}
          >
            <RotateCcwIcon aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </li>
  );
}

const riskOrder: readonly OperatorRiskGroup[] = ["general", "futures", "protection", "advanced"];

function OperatorRowsGroup({
  title,
  rows,
  collapsible,
  controller,
}: {
  title: string;
  rows: OperatorSettingRow[];
  collapsible: boolean;
  controller: OperatorConfigController;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  const hasChanges = rows.some((row) => controller.changed.includes(row.key));
  const lastChanges = useRef(false);
  useEffect(() => {
    if (hasChanges && !lastChanges.current && details.current) details.current.open = true;
    lastChanges.current = hasChanges;
  }, [hasChanges]);
  const list = (
    <ul className="divide-y rounded-lg border">
      {rows.map((row) => <OperatorRow key={row.key} row={row} controller={controller} />)}
    </ul>
  );
  if (collapsible) {
    return (
      <details ref={details} className="space-y-2">
        <summary className="min-touch cursor-pointer text-sm font-semibold">{title}</summary>
        {list}
      </details>
    );
  }
  return (
    <section className="space-y-2">
      {title ? <h3 className="text-sm font-semibold">{title}</h3> : null}
      {list}
    </section>
  );
}

function OperatorSectionView({
  section,
  controller,
}: {
  section: "modes" | "risk";
  controller: OperatorConfigController;
}) {
  const t = useT();
  const c = t.settings.operator;
  // The section key ("modes") and the copy group key ("gates") differ on
  // purpose: the modes card is titled "Chế độ tự động", not "modes".
  const title = c.groups[section === "modes" ? "gates" : section];
  const { payload } = controller;
  if (controller.phase === "loading") {
    return (
      <Card aria-busy="true">
        <CardHeader>
          <CardTitle>{title.title}</CardTitle>
          <CardDescription>{c.loading}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (controller.phase === "error" || !payload) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlertIcon className="size-5 shrink-0 text-destructive" aria-hidden="true" />
            {c.loadFailed}
          </CardTitle>
          <CardDescription>{controller.errorMessage}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button className="min-touch" onClick={() => void controller.reload()}>{t.common.retry}</Button>
        </CardContent>
      </Card>
    );
  }

  const rows = payload.settings.filter((row) => operatorSection(row) === section);
  const changes = controller.sectionChanges[section] ?? [];
  const groups = section === "modes"
    ? [{ id: "gates", title: "", rows }]
    : riskOrder.map((id) => ({
        id,
        title: c.riskGroups[id],
        rows: rows.filter((row) => operatorRiskGroup(row.key) === id),
      })).filter((group) => group.rows.length > 0);
  const labels = changes.map((key) => t.settings.gateLabels[key] ?? key).join(", ");
  const feedback = controller.feedback[section];
  const hasInvalid = changes.some((key) => controller.invalidKeys.includes(key));
  const gateKeys = operatorGateKeys(payload.settings);
  const sectionGateChange = changes.some((key) => gateKeys.has(key));
  const reasonInputId = "operator-gate-reason";
  return (
    <Card id={section === "modes" ? "operator-config" : "operator-risk"}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlertIcon className="size-4" aria-hidden="true" />
          {title.title}
        </CardTitle>
        <CardDescription>{title.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {controller.refreshFailed ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 p-3 text-sm">
            <span className="min-w-0 flex-1">{c.refreshFailed}</span>
            <Button variant="outline" size="sm" disabled={controller.refreshing || controller.saving} onClick={() => void controller.reload()}>
              {t.common.retry}
            </Button>
          </div>
        ) : null}
        {controller.serverAhead ? (
          <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 p-3 text-sm">
            <span className="min-w-0 flex-1">{c.serverAhead}</span>
            <Button variant="outline" size="sm" disabled={controller.refreshing || controller.refreshFailed || controller.saving} onClick={controller.reviewLatest}>
              {c.reviewLatest}
            </Button>
          </div>
        ) : null}
        {groups.map((group) => (
          <OperatorRowsGroup
            key={group.id}
            title={group.title}
            rows={group.rows}
            collapsible={group.id === "protection" || group.id === "advanced"}
            controller={controller}
          />
        ))}
        {sectionGateChange ? (
          <div className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3">
            <p className="text-sm font-medium">
              {c.gateChanges(labels)}
            </p>
            <p className="text-xs text-muted-foreground">
              {c.gateWarning}
            </p>
            <label className="flex min-touch items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={controller.confirmGate}
                disabled={controller.saving}
                onChange={(event) => controller.setConfirmation(event.target.checked)}
                className="size-5 accent-destructive"
              />
              {c.confirm}
            </label>
            <label htmlFor={reasonInputId} className="text-sm font-medium">
              {c.reasonLabel}
            </label>
            <input
              id={reasonInputId}
              type="text"
              value={controller.reason}
              disabled={controller.saving}
              onChange={(event) => controller.setGateReason(event.target.value)}
              placeholder={c.reasonPlaceholder}
              className="h-11 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60"
            />
          </div>
        ) : null}
        {section === "risk" && controller.dirtyCount > 0 && rows.some((row) => row.override) ? (
          <p className="text-xs text-muted-foreground">{c.resetDraftWarning}</p>
        ) : null}
        {feedback ? (
          <p role={feedback.role} className={feedback.role === "alert" ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>
            {feedback.text}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            className="min-touch"
            disabled={
              controller.saving || controller.refreshing || controller.serverAhead || controller.refreshFailed || changes.length === 0 || hasInvalid ||
              (sectionGateChange && (!controller.confirmGate || !controller.reason.trim()))
            }
            onClick={() => void controller.save(section)}
          >
            {controller.saving ? <LoaderCircleIcon className="animate-spin" aria-hidden="true" /> : <SaveIcon aria-hidden="true" />}
            {c.save(changes.length)}
          </Button>
          <Button
            variant="ghost"
            className="min-touch"
            disabled={controller.saving || changes.length === 0}
            onClick={() => controller.undo(section)}
          >
            {c.undo}
          </Button>
          <span className="text-xs text-muted-foreground">{c.updatedAt(formatDateTimeVi(payload.generated_at))}</span>
        </div>
      </CardContent>
    </Card>
  );
}

export function OperatorModes({ controller }: { controller: OperatorConfigController }) {
  return <OperatorSectionView section="modes" controller={controller} />;
}

export function OperatorRisk({ controller }: { controller: OperatorConfigController }) {
  return <OperatorSectionView section="risk" controller={controller} />;
}
