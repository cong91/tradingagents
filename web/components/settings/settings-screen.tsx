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
import { CredentialsCard } from "./credentials-card";
import { formatDateTimeVi } from "./format-time";
import { GatesCard } from "./gates-card";
import { PathsCard } from "./paths-card";
import { SaveBar } from "./save-bar";
import { SettingsSection } from "./settings-section";
import { groupRows } from "./types";
import { useSettings } from "./use-settings";

function SettingsSkeleton() {
  return (
    <div aria-busy="true" aria-live="polite">
      <PageHeader
        title="Cáº¥u hÃ¬nh"
        description="Äang táº£i cÃ i Ä‘áº·t tá»« GET /api/settingsâ€¦"
      />
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
        <PageHeader
          title="Cáº¥u hÃ¬nh"
          description="Äá»c vÃ  ghi cÃ i Ä‘áº·t engine qua GET / PUT /api/settings."
        />
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <TriangleAlertIcon
                className="size-5 shrink-0 text-destructive"
                aria-hidden="true"
              />
              KhÃ´ng táº£i Ä‘Æ°á»£c cÃ i Ä‘áº·t
            </CardTitle>
            <CardDescription>{controller.errorMessage}</CardDescription>
          </CardHeader>
          <CardContent>
            <Button className="min-touch" onClick={() => void controller.reload()}>
              Thá»­ láº¡i
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
      <PageHeader
        title="Cáº¥u hÃ¬nh"
        description="Äá»c vÃ  ghi cÃ i Ä‘áº·t engine qua GET / PUT /api/settings. Thay Ä‘á»•i Ã¡p dá»¥ng cho phiÃªn phÃ¢n tÃ­ch má»›i; khÃ³a API vÃ  cá»•ng LIVE chá»‰-Ä‘á»c theo thiáº¿t káº¿."
      />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <span>Cáº­p nháº­t lÃºc {formatDateTimeVi(payload.generated_at)}</span>
        {controller.isStale ? (
          <Badge
            variant="outline"
            className="border-amber-500 text-amber-700 dark:text-amber-400"
          >
            Dá»¯ liá»‡u cÅ©
          </Badge>
        ) : null}
        <Button
          variant="ghost"
          size="sm"
          className="min-touch"
          onClick={() => void controller.reload()}
        >
          <RefreshCwIcon aria-hidden="true" />
          LÃ m má»›i
        </Button>
      </div>
      {controller.serverAhead ? (
        <div
          role="status"
          className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          <span className="min-w-0 flex-1">
            MÃ¡y chá»§ cÃ³ dá»¯ liá»‡u má»›i hÆ¡n báº£n Ä‘ang xem. Thay Ä‘á»•i chÆ°a lÆ°u cá»§a báº¡n khÃ´ng bá»‹
            ghi Ä‘Ã¨.
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
            Náº¡p dá»¯ liá»‡u mÃ¡y chá»§
          </Button>
        </div>
      ) : null}
      {payload.settings.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>ChÆ°a cÃ³ cÃ i Ä‘áº·t nÃ o</CardTitle>
            <CardDescription>
              MÃ¡y chá»§ tráº£ vá» danh sÃ¡ch cÃ i Ä‘áº·t trá»‘ng. Kiá»ƒm tra backend Ä‘Ã£ cháº¡y Ä‘Ãºng
              phiÃªn báº£n há»£p Ä‘á»“ng rá»“i báº¥m LÃ m má»›i.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <div className="space-y-4">
          {grouped.groups.map(({ group, rows }) =>
            rows.length === 0 ? null : (
              <SettingsSection
                key={group.id}
                title={group.title}
                description={group.description}
                rows={rows}
                values={controller.formValues}
                errors={controller.fieldErrors}
                onChange={controller.setValue}
              />
            )
          )}
          {grouped.otherRows.length > 0 ? (
            <SettingsSection
              title="CÃ i Ä‘áº·t khÃ¡c"
              description="CÃ¡c key mÃ¡y chá»§ tráº£ vá» ngoÃ i cÃ¡c nhÃ³m chuáº©n."
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
          <PathsCard rows={grouped.pathRows} />
          <SaveBar controller={controller} />
        </div>
      )}
    </div>
  );
}
