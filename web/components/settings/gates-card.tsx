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

// Gate status is read-only on this summary card; changes are made in the
// operator configuration card with confirmation and an audit reason.
export function GatesCard({
  gates,
  effectiveMode,
}: {
  gates: Record<string, boolean>;
  effectiveMode: string;
}) {
  const live = effectiveMode === "live";
  const t = useT();
  const c = t.settings.gates;
  // Giải thích theo trạng thái đọc được của cổng «Lệnh thật»; giá trị ngoài
  // contract live|dry (server lạ / chưa đọc được) rơi về nhắc Làm mới.
  const explain =
    effectiveMode === "live"
      ? c.explainLive
      : effectiveMode === "dry"
        ? c.explainSafe
        : c.explainChecking;
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
          <span className="text-sm font-medium">{c.effectiveMode}</span>
          {live ? (
            <Badge variant="destructive">{c.live}</Badge>
          ) : (
            <Badge variant="secondary">{c.dry}</Badge>
          )}
        </div>
        <p className="text-sm text-muted-foreground">{explain}</p>
        <ul className="space-y-2">
          {Object.entries(gates).map(([name, open]) => (
            <li
              key={name}
              className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
            >
              <span className="text-sm">{t.settings.gateLabels[name] ?? name}</span>
              {open ? (
                <Badge variant="destructive">{c.open}</Badge>
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
