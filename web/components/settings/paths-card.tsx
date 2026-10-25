"use client";

import { FolderIcon } from "lucide-react";
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
import type { SettingRow } from "./types";

export function PathsCard({ rows }: { rows: SettingRow[] }) {
  const t = useT();
  const c = t.settings.paths;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
        <CardAction>
          <Badge variant="outline">{t.common.readOnly}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            {c.empty}
          </p>
        ) : (
          <ul className="space-y-2">
            {rows.map((row) => (
              <li key={row.key} className="border-t py-2 first:border-t-0 first:pt-0">
                <p className="text-sm">{t.settings.pathLabels[row.key] ?? row.key}
</p>
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {row.value === null || row.value === undefined ? "—" : String(row.value)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
