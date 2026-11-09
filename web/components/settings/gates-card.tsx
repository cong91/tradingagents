"use client";

import { ShieldCheckIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useT } from "@/lib/i18n";
import { type SettingsPayload } from "./types";

const AUTOMATION_MODE_KEYS = ["auto_spot", "auto_futures"] as const;

// Automation mode status is read-only here; changes live in operator config.
export function GatesCard({
  gates,
}: {
  gates: Pick<SettingsPayload, "gates_readonly">["gates_readonly"];
}) {
  const anyAutoEnabled = gates.auto_spot === true || gates.auto_futures === true;
  const t = useT();
  const c = t.settings.gates;
  return (
    // id là đích của chip trạng thái cổng trong sidebar (/settings#exec-gates).
    <Card id="exec-gates">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheckIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
        <CardAction>
          <Badge variant="outline">{t.common.readOnly}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3">
          <span className="text-sm font-medium">{c.anyAuto}</span>
          {anyAutoEnabled ? (
            <Badge variant="default">{c.open}</Badge>
          ) : (
            <Badge variant="outline">{c.closed}</Badge>
          )}
        </div>
        <ul className="space-y-2">
          {AUTOMATION_MODE_KEYS.map((name) => (
            <li
              key={name}
              className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
            >
              <span className="text-sm">{t.settings.gateLabels[name] ?? name}</span>
              {gates[name] ? (
                <Badge variant="default">{c.open}</Badge>
              ) : (
                <Badge variant="outline">{c.closed}</Badge>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
