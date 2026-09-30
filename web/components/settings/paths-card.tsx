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
import { PATH_LABELS, type SettingRow } from "./types";

export function PathsCard({ rows }: { rows: SettingRow[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderIcon className="size-4" aria-hidden="true" />
          Đường dẫn hệ thống
        </CardTitle>
        <CardDescription>
          Chỉ đọc — đặt qua biến môi trường tương ứng, không ghi được qua API.
        </CardDescription>
        <CardAction>
          <Badge variant="outline">Chỉ đọc</Badge>
        </CardAction>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            Máy chủ không trả về đường dẫn nào.
          </p>
        ) : (
          <ul className="space-y-2">
            {rows.map((row) => (
              <li key={row.key} className="border-t py-2 first:border-t-0 first:pt-0">
                <p className="text-sm">{PATH_LABELS[row.key] ?? row.key}</p>
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
