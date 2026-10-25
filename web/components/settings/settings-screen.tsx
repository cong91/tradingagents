"use client";

import { useMemo } from "react";
import { RefreshCwIcon, TriangleAlertIcon } from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { AutoloopCard } from "./autoloop-card";
import { CredentialsCard } from "./credentials-card";
import { formatDateTimeVi } from "./format-time";
import { GatesCard } from "./gates-card";
import { OperatorConfigCard } from "./operator-config-card";
import { PathsCard } from "./paths-card";
import { SaveBar } from "./save-bar";
import { SettingsSection } from "./settings-section";
import { groupRows } from "./types";
import { useSettings } from "./use-settings";
import { InterfacePreferencesCard } from "./interface-preferences-card";
import { useT } from "@/lib/i18n";

function SettingsSkeleton() {
  const t = useT();
  return (
    <div aria-busy="true" aria-live="polite">
      <PageHeader page="settings" description={t.settings.screen.loadingDescription} />
      <InterfacePreferencesCard />
      <div className="space-y-4">
        {[0, 1, 2].map((card) => (
          <div key={card} className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
            <div className="h-5 w-44 animate-pulse rounded bg-muted" />
            <div className="mt-4 space-y-3">
              {[0, 1, 2, 3].map((row) => (
                <div key={row} className="h-9 animate-pulse rounded bg-muted" />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function SettingsScreen() {
  const t = useT();
  const c = t.settings.screen;
  const controller = useSettings();
  const grouped = useMemo(
    () => (controller.payload ? groupRows(controller.payload) : null),
    [controller.payload]
  );

  if (controller.phase === "loading") {
    return <SettingsSkeleton />;
  }

  if (controller.phase === "error") {
    return (
      <div>
        <PageHeader page="settings" description={c.errorDescription} />
        <InterfacePreferencesCard />
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TriangleAlertIcon
                className="size-5 shrink-0 text-destructive"
                aria-hidden="true"
              />
              {c.loadFailed}
            </CardTitle>
            <CardDescription>{controller.errorMessage}</CardDescription>
          </CardHeader>
          <CardContent>
            <Button className="min-touch" onClick={() => void controller.reload()}>
              {t.common.retry}
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const payload = controller.payload;
  if (!payload || !grouped) return null;

  return (
    <div>
      <PageHeader page="settings" />
      <InterfacePreferencesCard />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <span>{c.updatedAt(formatDateTimeVi(payload.generated_at))}</span>
        {controller.isStale ? (
          <Badge
            variant="outline"
            className="border-amber-500 text-amber-700 dark:text-amber-400"
          >
            {t.common.staleData}
          </Badge>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="min-touch"
          onClick={() => void controller.reload()}
        >
          <RefreshCwIcon aria-hidden="true" />
          {t.common.refresh}
        </Button>
      </div>
      {controller.serverAhead ? (
        <div
          role="status"
          className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          <span className="min-w-0 flex-1">
            {c.serverAhead}
          </span>
          <Button
            variant="outline"
            size="sm"
            className="min-touch"
            onClick={() => {
              controller.resetChanges();
              void controller.reload();
            }}
          >
            {c.loadServerData}
          </Button>
        </div>
      ) : null}
      {payload.settings.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>{c.emptyTitle}</CardTitle>
            <CardDescription>{c.emptyDescription}</CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <div className="space-y-4">
          {grouped.groups.map(({ group, rows }) =>
            rows.length === 0 ? null : (
              <SettingsSection
                key={group.id}
                title={t.settings.groups[group.id].title}
                description={t.settings.groups[group.id].description}
                rows={rows}
                values={controller.formValues}
                errors={controller.fieldErrors}
                onChange={controller.setValue}
              />
            )
          )}
          {grouped.otherRows.length > 0 ? (
            <SettingsSection
              title={c.otherTitle}
              description={c.otherDescription}

              rows={grouped.otherRows}
              values={controller.formValues}
              errors={controller.fieldErrors}
              onChange={controller.setValue}
            />
          ) : null}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <CredentialsCard credentials={payload.credentials} />
            <GatesCard
              gates={payload.gates_readonly}
              effectiveMode={payload.effective_exec_mode}
            />
          </div>
          {/* Vòng tự động tự fetch GET /api/autoloop — độc lập với payload /api/settings. */}
          <AutoloopCard />
          {/* Override vận hành: đọc/ghi PostgreSQL, thay cho operator_config.json. */}
          <OperatorConfigCard />
          <PathsCard rows={grouped.pathRows} />
          <SaveBar controller={controller} />
        </div>
      )}
    </div>
  );
}
