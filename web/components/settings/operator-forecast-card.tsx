"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { InfoIcon, ShieldCheckIcon, TriangleAlertIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useT } from "@/lib/i18n";
import type { OperatorConfigController } from "./operator-config-controller";
import type { KronosProfileId, KronosStatus } from "./operator-config-types";
import { SaveBar } from "./save-bar";
import type { SettingsController } from "./use-settings";

const PROFILE_KEY = "kronos_active_profile";
const PROFILE_KEYS = [PROFILE_KEY] as const;
const PROFILE_OPTIONS: readonly KronosProfileId[] = ["small_base", "mini_2k"];

function KronosReadiness({ controller, settings }: {
  controller: OperatorConfigController;
  settings: SettingsController;
}) {
  const t = useT();
  const c = t.settings.operator;
  const k = t.settings.kronos;
  const kronos: KronosStatus | null | undefined = controller.payload?.kronos;
  const savedProfile = settings.payload?.settings.find((row) => row.key === PROFILE_KEY)?.value;
  const mismatch = typeof savedProfile === "string" && kronos?.profile_id !== savedProfile;
  const stale = controller.serverAhead || controller.refreshFailed || settings.serverAhead || settings.isStale || mismatch;
  const profileLabel = kronos && (kronos.profile_id === "small_base" || kronos.profile_id === "mini_2k")
    ? k.profileOptions[kronos.profile_id] : kronos?.profile_id;
  const readable = (value: number | null): string => value === null ? "—" : String(value);

  return (
    <section className="space-y-3 rounded-lg border p-4" aria-labelledby="kronos-readiness-title" data-kronos-status>
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="kronos-readiness-title" className="text-sm font-semibold">{k.readinessLabel}</h3>
        {kronos ? (
          <Badge
            variant={kronos.readiness.status === "ready" ? "outline" : "destructive"}
            data-kronos-readiness={kronos.readiness.status}
          >
            {kronos.readiness.status === "ready" ? k.modelReadyShort : k.modelUnavailableShort}
          </Badge>
        ) : null}
      </div>
      {controller.refreshFailed ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 p-3 text-sm">
          <span className="min-w-0 flex-1">{c.refreshFailed}</span>
          <Button variant="outline" size="sm" className="min-touch" disabled={controller.refreshing || controller.saving} onClick={() => void controller.reload()}>{t.common.retry}</Button>
        </div>
      ) : null}
      {controller.serverAhead ? (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 p-3 text-sm">
          <span className="min-w-0 flex-1">{c.serverAhead}</span>
          <Button variant="outline" size="sm" className="min-touch" disabled={controller.refreshing || controller.refreshFailed || controller.saving} onClick={controller.reviewLatest}>{c.reviewLatest}</Button>
        </div>
      ) : null}
      {controller.phase === "loading" ? <p aria-live="polite" className="text-sm text-muted-foreground">{c.loading}</p> : null}
      {controller.phase !== "loading" && !kronos ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
          <span className="min-w-0 flex-1">{controller.errorMessage || c.kronosUnavailable}</span>
          <Button variant="outline" size="sm" className="min-touch" disabled={controller.refreshing} onClick={() => void controller.reload()}>
            {t.common.retry}
          </Button>
        </div>
      ) : null}
      {kronos ? (
        <>
          {stale ? <p id="kronos-stale-status" role="status" className="text-xs text-muted-foreground">{mismatch ? k.profileStatusMismatch : k.staleStatus}</p> : null}
          {kronos.readiness.reason ? (
            <p role="alert" className="flex items-start gap-2 break-words text-xs text-destructive">
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              {kronos.readiness.reason}
            </p>
          ) : null}
          <div className="space-y-1 text-sm">
            <p><span className="text-muted-foreground">{k.profileLabel}: </span>{profileLabel}</p>
            <p className="text-muted-foreground">
              {k.scopeSummary(kronos.timeframe, readable(kronos.lookback_bars), readable(kronos.horizon_hours), readable(kronos.paths))}
            </p>
          </div>
          {mismatch ? (
            <Button variant="outline" size="sm" className="min-touch" disabled={controller.refreshing || controller.saving} onClick={() => void controller.reload()}>{t.common.refresh}</Button>
          ) : null}
          <details className="rounded-md border bg-card px-3 py-2">
            <summary className="min-touch cursor-pointer py-1 text-sm font-medium">{k.detailsTitle}</summary>
            <dl className="mt-3 space-y-2 border-t pt-3">
              <div className="grid gap-1 sm:grid-cols-[minmax(0,150px)_minmax(0,1fr)] sm:items-baseline">
                <dt className="text-xs text-muted-foreground">{k.modelLabel}</dt>
                <dd className="break-all font-mono text-xs">{kronos.model.repo || "—"} @ {kronos.model.model_revision.slice(0, 12) || "—"}</dd>
              </div>
              <div className="grid gap-1 sm:grid-cols-[minmax(0,150px)_minmax(0,1fr)] sm:items-baseline">
                <dt className="text-xs text-muted-foreground">{k.tokenizerLabel}</dt>
                <dd className="break-all font-mono text-xs">{kronos.model.tokenizer_repo || "—"} @ {kronos.model.tokenizer_revision.slice(0, 12) || "—"}</dd>
              </div>
              <div className="grid gap-1 sm:grid-cols-[minmax(0,150px)_minmax(0,1fr)] sm:items-baseline">
                <dt className="text-xs text-muted-foreground">{k.sourceRevisionLabel}</dt>
                <dd className="break-all font-mono text-xs">{kronos.model.source_revision || "—"}</dd>
              </div>
              <div className="grid gap-1 sm:grid-cols-[minmax(0,150px)_minmax(0,1fr)] sm:items-baseline">
                <dt className="text-xs text-muted-foreground">{k.parameterLabel}</dt>
                <dd className="text-sm">{k.maxContextLabel}: {readable(kronos.max_context)} · {k.temperatureLabel}: {readable(kronos.temperature)}</dd>
              </div>
            </dl>
          </details>
        </>
      ) : null}
    </section>
  );
}

export function OperatorForecast({ controller, operator }: {
  controller: SettingsController;
  operator: OperatorConfigController;
}) {
  const t = useT();
  const c = t.settings.operator;
  const k = t.settings.kronos;
  const profile = controller.payload?.settings.find((row) => row.key === PROFILE_KEY);
  const value = controller.formValues[PROFILE_KEY];
  const error = controller.fieldErrors[PROFILE_KEY];
  const focusRef = useRef<HTMLButtonElement | null>(null);
  const focusMovedRef = useRef(false);

  useEffect(() => {
    const trackFocus = (event: FocusEvent) => {
      if (focusRef.current && event.target !== focusRef.current) focusMovedRef.current = true;
    };
    const trackPointer = () => {
      if (focusRef.current) focusMovedRef.current = true;
    };
    document.addEventListener("focusin", trackFocus);
    document.addEventListener("pointerdown", trackPointer);
    return () => {
      document.removeEventListener("focusin", trackFocus);
      document.removeEventListener("pointerdown", trackPointer);
    };
  }, []);
  useLayoutEffect(() => {
    if (controller.saveState.kind === "saving") return;
    const button = focusRef.current;
    if (
      button && !focusMovedRef.current && document.activeElement === document.body &&
      controller.saveState.kind === "failed" && controller.saveState.scope === "kronos-profile" &&
      button.isConnected && !button.disabled && button.getClientRects().length > 0
    ) button.focus();
    focusRef.current = null;
  }, [controller.saveState]);

  return (
    <Card id="operator-forecast">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheckIcon className="size-4" aria-hidden="true" />
          {c.groups.forecast.title}
        </CardTitle>
        <CardDescription>{c.groups.forecast.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <InfoIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          {k.contextNote}
        </p>
        {profile?.editable ? (
          <>
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,340px)] sm:items-center sm:gap-4">
              <div className="min-w-0">
                <label htmlFor="setting-kronos_active_profile" className="text-sm font-medium">{t.settings.settingMeta[PROFILE_KEY].label}</label>
                <p id="setting-kronos_active_profile-hint" className="mt-0.5 text-xs text-muted-foreground">{t.settings.settingMeta[PROFILE_KEY].hint}</p>
              </div>
              <select
                id="setting-kronos_active_profile"
                value={typeof value === "string" ? value : ""}
                onChange={(event) => controller.setValue(PROFILE_KEY, event.target.value)}
                aria-invalid={error ? true : undefined}
                aria-describedby={`setting-kronos_active_profile-hint${error ? " setting-kronos_active_profile-error" : ""}`}
                className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-60 aria-[invalid=true]:border-destructive"
              >
                {typeof value === "string" && value !== "small_base" && value !== "mini_2k" ? <option value={value} disabled>{value || "—"}</option> : null}
                {PROFILE_OPTIONS.map((profileId) => <option key={profileId} value={profileId}>{k.profileOptions[profileId]}</option>)}
              </select>
              {error ? <p id="setting-kronos_active_profile-error" role="alert" className="text-xs text-destructive sm:col-span-2">{error}</p> : null}
            </div>
            <div onClickCapture={(event) => {
              const button = event.target instanceof Element ? event.target.closest<HTMLButtonElement>('button[data-settings-action="save"]') : null;
              if (button && document.activeElement === button) {
                focusRef.current = button;
                focusMovedRef.current = false;
              }
            }}>
              <SaveBar controller={controller} keys={PROFILE_KEYS} scope="kronos-profile" scopeLabel={k.profileLabel} suppressUncertainMessage={controller.saveState.kind === "uncertain" && controller.saveState.scope !== "kronos-profile"} />
            </div>
          </>
        ) : controller.phase === "loading" ? (
          <p aria-live="polite" className="text-sm text-muted-foreground">{t.settings.screen.loadingDescription}</p>
        ) : (
          <p role="alert" className="text-sm text-destructive">{c.kronosUnavailable}</p>
        )}
        <KronosReadiness controller={operator} settings={controller} />
      </CardContent>
    </Card>
  );
}
