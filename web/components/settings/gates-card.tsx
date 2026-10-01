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
import { GATE_LABELS } from "./types";

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
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheckIcon className="size-4" aria-hidden="true" />
          Cổng thực thi (chỉ đọc)
        </CardTitle>
        <CardDescription>
          LIVE chỉ mở khi cả hai nửa cổng cùng mở. Các cổng này cố ý vắng khỏi API ghi —
          không thể bật từ UI.
        </CardDescription>
        <CardAction>
          <Badge variant="outline">Chỉ đọc</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3">
          <span className="text-sm font-medium">Chế độ thực thi hiệu lực</span>
          {live ? (
            <Badge variant="destructive">LIVE — LỆNH THẬT</Badge>
          ) : (
            <Badge variant="secondary">Mô phỏng (dry)</Badge>
          )}
        </div>
        <ul className="space-y-2">
          {Object.entries(gates).map(([name, open]) => (
            <li
              key={name}
              className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
            >
              <span className="text-sm">{GATE_LABELS[name] ?? name}</span>
              {open ? (
                <Badge variant="destructive">MỞ</Badge>
              ) : (
                <Badge variant="outline">ĐÓNG</Badge>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
