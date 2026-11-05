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

// Cổng FR5 là read-only (hợp đồng §1): LIVE chỉ mở khi CẢ HAI nửa cùng mở —
// thẻ này chỉ hiển thị trạng thái, không có điều khiển ghi.
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
  return (
    <Card>
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
