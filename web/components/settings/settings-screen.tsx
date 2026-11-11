"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCwIcon, TriangleAlertIcon } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/lib/i18n";
import { AutoloopCard, AutoloopHaltControl } from "./autoloop-card";
import { DataSourceCard } from "./data-source-card";
import { formatDateTimeVi } from "./format-time";
import { InterfacePreferencesCard } from "./interface-preferences-card";
import { LLM_SETTING_KEYS, LlmConfigCard } from "./llm-config-card";
import { OperatorForecast, OperatorModes, OperatorRisk } from "./operator-config-card";
import { useOperatorConfig } from "./operator-config-controller";
import { PathsCard } from "./paths-card";
import { SaveBar } from "./save-bar";
import { areaForHash, areaForSetting, operatorOwnershipConsistent, visibleSettingsRows, type SettingsArea } from "./settings-layout";
import { SettingsNavigation } from "./settings-navigation";
import { SettingsSection } from "./settings-section";
import { groupRows, type SettingRow } from "./types";
import { useSettings } from "./use-settings";
import { useUnsavedNavigation } from "./use-unsaved-navigation";

export function SettingsScreen() {
  const t = useT();
  const c = t.settings.screen;
  const controller = useSettings();
  const operator = useOperatorConfig();
  const [area, setArea] = useState<SettingsArea>("automation");
  const [llmDirty, setLlmDirty] = useState(false);
  const [cookiesDirty, setCookiesDirty] = useState(false);
  const [scheduleDirty, setScheduleDirty] = useState(false);

  useEffect(() => {
    const selectHash = () => {
      const next = areaForHash(window.location.hash);
      if (next) setArea(next);
      if (window.location.hash === "#operator-config" || window.location.hash === "#exec-gates") {
        setTimeout(() => document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ block: "start" }), 0);
      }
    };
    const initialId = setTimeout(selectHash, 0);
    window.addEventListener("hashchange", selectHash);
    window.addEventListener("popstate", selectHash);
    return () => {
      clearTimeout(initialId);
      window.removeEventListener("hashchange", selectHash);
      window.removeEventListener("popstate", selectHash);
    };
  }, []);

  useEffect(() => {
    if (area !== "automation" || operator.phase !== "ready") return;
    if (window.location.hash !== "#operator-config" && window.location.hash !== "#exec-gates") return;
    const scrollId = setTimeout(() => {
      document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ block: "start" });
    }, 0);
    return () => clearTimeout(scrollId);
  }, [area, operator.phase, controller.phase]);

  const unsaved = controller.dirtyCount > 0 || operator.dirtyCount > 0 || llmDirty || cookiesDirty || scheduleDirty;
  useUnsavedNavigation(unsaved, c.leaveUnsaved);

  const grouped = useMemo(
    () => (controller.payload ? groupRows(controller.payload) : null),
    [controller.payload],
  );
  const operatorAllowlist = operator.phase === "ready" && operator.payload &&
    operatorOwnershipConsistent(operator.payload.settings, operator.payload.allowlist)
    ? operator.payload.allowlist
    : null;
  const rowsByArea = useMemo(() => {
    const result: Record<SettingsArea, SettingRow[]> = {
      automation: [], ai: [], risk: [], connections: [], system: [],
    };
    if (!controller.payload) return result;
    const operatorKeys = operator.payload?.settings.map((row) => row.key) ?? [];
    const rows = visibleSettingsRows(controller.payload.settings, operatorAllowlist, operatorKeys);
    for (const row of rows) {
      if (!row.editable && row.key !== "exec_base_url") continue;
      if (LLM_SETTING_KEYS.includes(row.key as (typeof LLM_SETTING_KEYS)[number])) continue;
      result[areaForSetting(row.key)].push(row);
    }
    return result;
  }, [controller.payload, operatorAllowlist, operator.payload]);

  const selectArea = useCallback((next: SettingsArea) => setArea(next), []);
  const systemRows = rowsByArea.system;
  // Kronos forecast tuning is a distinct group inside the AI area (beside its
  // managed switch), not a loose row in the generic advanced pile.
  const kronosTuningRows = rowsByArea.ai.filter((row) => row.key.startsWith("kronos_active_"));
  const otherAiRows = rowsByArea.ai.filter((row) => !row.key.startsWith("kronos_active_"));
  const aiKeys = [
    ...LLM_SETTING_KEYS,
    ...rowsByArea.ai.map((row) => row.key),
  ];
  const systemKeys = systemRows.map((row) => row.key);
  const connectionKeys = rowsByArea.connections.map((row) => row.key);
  const riskKeys = rowsByArea.risk.map((row) => row.key);
  const summary: Record<SettingsArea, string> = {
    automation: c.automationSummary,
    ai: c.aiSummary,
    risk: c.riskSummary,
    connections: c.connectionsSummary,
    system: c.systemSummary,
  };

  return (
    <div>
      <PageHeader page="settings" />
      <div className="grid gap-5 md:grid-cols-[190px_minmax(0,1fr)] lg:grid-cols-[210px_minmax(0,1fr)]">
        <SettingsNavigation selected={area} onSelect={selectArea} />
        <div className="min-w-0">
          <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="text-lg font-semibold">{c.sections[area]}</h2>
              <p className="mt-1 text-sm text-muted-foreground">{summary[area]}</p>
            </div>
            {controller.payload ? (
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>{c.updatedAt(formatDateTimeVi(controller.payload.generated_at))}</span>
                {controller.isStale ? <Badge variant="outline">{t.common.staleData}</Badge> : null}
                <Button variant="ghost" size="sm" className="min-touch" onClick={() => void controller.reload()}>
                  <RefreshCwIcon aria-hidden="true" />{t.common.refresh}
                </Button>
              </div>
            ) : null}
          </header>
          {unsaved ? <p role="status" className="mb-3 text-sm text-muted-foreground">{c.dirtyNotice}</p> : null}
          {controller.serverAhead ? (
            <div id="settings-conflict" role="status" className="mb-4 space-y-3 rounded-md border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
              <p>{c.serverAhead}</p>
              {controller.saveState.kind === "uncertain" ? <p role="alert">{t.settings.save.uncertain}</p> : null}
              {Object.keys(controller.conflictValues).length > 0 ? (
                <div>
                  <h3 className="font-medium">{c.conflictTitle}</h3>
                  <ul className="mt-2 space-y-2">
                    {Object.entries(controller.conflictValues).map(([key, values]) => {
                      const sensitive = !t.settings.settingMeta[key] || /url|token|secret|key|password|credential/i.test(key);
                      const display = (value: string | boolean | undefined) => value === undefined
                        ? c.conflictMissing
                        : sensitive ? c.conflictSensitive
                        : typeof value === "boolean" ? (value ? t.common.on : t.common.off) : value || "—";
                      return (
                        <li key={key} className="break-words">
                          <span className="font-medium">{t.settings.settingMeta[key]?.label ?? key}</span>
                          <span className="block">{c.conflictServer}: {display(values.server)}</span>
                          <span className="block">{c.conflictDraft}: {display(values.draft)}</span>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" className="min-touch" disabled={controller.reviewing} onClick={() => void controller.reviewLatest()}>{c.conflictAcknowledge}</Button>
                <Button variant="ghost" size="sm" className="min-touch" disabled={controller.reviewing} onClick={() => {
                  if (!window.confirm(c.discardServerData)) return;
                  void controller.discardAndReload();
                }}>{c.loadServerData}</Button>
              </div>
            </div>
          ) : null}
          {controller.phase === "error" ? (
            <Card className="mb-4">
              <CardHeader>
                <CardTitle className="flex items-center gap-2"><TriangleAlertIcon className="size-4 text-destructive" aria-hidden="true" />{c.loadFailed}</CardTitle>
                <CardDescription>{controller.errorMessage}</CardDescription>
              </CardHeader>
              <CardContent><Button className="min-touch" onClick={() => void controller.reload()}>{t.common.retry}</Button></CardContent>
            </Card>
          ) : null}
          {controller.phase === "loading" ? <p aria-live="polite" className="mb-4 text-sm text-muted-foreground">{c.loadingDescription}</p> : null}
          {controller.payload && controller.payload.settings.length === 0 ? (
            <p role="status" className="mb-4 text-sm text-muted-foreground">{c.emptyDescription}</p>
          ) : null}

          <section id="automation" hidden={area !== "automation"} className="space-y-4">
            <div className="rounded-md border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
              <p className="font-medium">{t.settings.settingMeta.exec_base_url.label}</p>
              <p className="mt-1">{c.venueReview}</p>
              <a href="#connections" onClick={(event) => { event.preventDefault(); history.pushState(null, "", "#connections"); setArea("connections"); }} className="mt-1 inline-block underline underline-offset-2">{c.sections.connections}</a>
            </div>
            <div id="exec-gates"><OperatorModes controller={operator} /></div>
            <AutoloopHaltControl />
            <AutoloopCard hideHalt onDirtyChange={setScheduleDirty} />
          </section>
          <section id="ai" hidden={area !== "ai"} className="space-y-4">
            {controller.payload ? <LlmConfigCard controller={controller} onDirtyChange={setLlmDirty} /> : null}
            {/* The managed Kronos switch lives in the AI/forecasting area —
                deliberately NOT beside the Auto Spot/Futures gates in
                automation: it changes what a cycle reads, never what it arms. */}
            <OperatorForecast controller={operator} />
            {kronosTuningRows.length ? (
              <details className="rounded-md border bg-card p-4" open={kronosTuningRows.some((row) => Boolean(controller.fieldErrors[row.key])) || undefined}>
                <summary className="cursor-pointer text-sm font-medium">{c.kronosTuning}</summary>
                <div className="mt-4"><SettingsSection title={t.settings.kronos.tuningTitle} description={t.settings.kronos.tuningDescription} rows={kronosTuningRows} values={controller.formValues} errors={controller.fieldErrors} onChange={controller.setValue} showTechnicalDetails /></div>
              </details>
            ) : null}
            {otherAiRows.length ? (
              <details className="rounded-md border bg-card p-4" open={otherAiRows.some((row) => Boolean(controller.fieldErrors[row.key])) || undefined}>
                <summary className="cursor-pointer text-sm font-medium">{c.advanced}</summary>
                <div className="mt-4"><SettingsSection title={t.settings.groups.llm.title} description={t.settings.groups.llm.description} rows={otherAiRows} values={controller.formValues} errors={controller.fieldErrors} onChange={controller.setValue} showTechnicalDetails /></div>
              </details>
            ) : null}
            {controller.payload ? <SaveBar controller={controller} keys={aiKeys} scope="ai" /> : null}
          </section>
          <section id="risk" hidden={area !== "risk"} className="space-y-4">
            {operatorAllowlist !== null || operator.phase !== "ready" ? <OperatorRisk controller={operator} /> : null}
            {operatorAllowlist === null ? (
              <p role="status" className="text-sm text-muted-foreground">{operator.phase === "loading" ? c.loadingRisk : c.operatorUnavailable}</p>
            ) : rowsByArea.risk.length ? (
              <details className="rounded-md border bg-card p-4" open={rowsByArea.risk.some((row) => Boolean(controller.fieldErrors[row.key])) || undefined}>
                <summary className="cursor-pointer text-sm font-medium">{c.otherRisk}</summary>
                <div className="mt-4"><SettingsSection title={t.settings.groups.risk.title} description={t.settings.groups.risk.description} rows={rowsByArea.risk} values={controller.formValues} errors={controller.fieldErrors} onChange={controller.setValue} /></div>
              </details>
            ) : null}
            {operatorAllowlist !== null && riskKeys.length ? <SaveBar controller={controller} keys={riskKeys} scope="risk" /> : null}
          </section>
          <section id="connections" hidden={area !== "connections"} className="space-y-4">
            {rowsByArea.connections.length ? (
              <SettingsSection title={t.settings.groups.exec.title} description={t.settings.groups.exec.description} rows={rowsByArea.connections} values={controller.formValues} errors={controller.fieldErrors} onChange={controller.setValue} />
            ) : null}
            {connectionKeys.length ? <SaveBar controller={controller} keys={connectionKeys} scope="connections" /> : null}
            <DataSourceCard onDirtyChange={setCookiesDirty} />
          </section>
          <section id="system" hidden={area !== "system"} className="space-y-4">
            <InterfacePreferencesCard />
            {systemRows.length ? (
              <details className="rounded-md border bg-card p-4" open={systemRows.some((row) => Boolean(controller.fieldErrors[row.key])) || undefined}>
                <summary className="cursor-pointer text-sm font-medium">{c.advanced}</summary>
                <div className="mt-4"><SettingsSection title={c.otherTitle} description={c.otherDescription} rows={systemRows} values={controller.formValues} errors={controller.fieldErrors} onChange={controller.setValue} showTechnicalDetails /></div>
              </details>
            ) : null}
            {systemKeys.length ? <SaveBar controller={controller} keys={systemKeys} scope="system" /> : null}
            {grouped ? <PathsCard rows={grouped.pathRows} /> : null}
          </section>
        </div>
      </div>
    </div>
  );
}
