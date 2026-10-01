"use client";

// Danh sách kế hoạch đã được xử lý (approved/rejected/executed/execute_failed/
// expired) — ngữ cảnh đối chiếu dưới hàng đợi, mới giải quyết nhất trước.

import { InboxIcon } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

import { formatDateTime, formatOrderAmount } from "./format";
import { ApprovalStatusBadge, SideChip } from "./labels";
import type { ApprovalItem } from "./types";

export function ResolvedList({ items }: { items: ApprovalItem[] }) {
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle>Đã xử lý gần đây</CardTitle>
        <CardDescription>
          {items.length > 0
            ? `${items.length} mục — mới giải quyết nhất trước.`
            : "Kế hoạch sau khi duyệt / từ chối sẽ xuất hiện ở đây."}
        </CardDescription>
      </CardHeader>
      {items.length === 0 ? (
        <CardContent>
          <div className="flex h-24 flex-col items-center justify-center gap-1 rounded-lg border border-dashed text-sm text-muted-foreground">
            <InboxIcon className="size-5" aria-hidden="true" />
            Chưa có kế hoạch nào được xử lý.
          </div>
        </CardContent>
      ) : (
        <CardContent>
          <ul className="divide-y">
            {items.map((item) => (
              <li
                key={item.id}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5 text-sm first:pt-0 last:pb-0"
              >
                <span className="w-20 shrink-0 font-medium tabular-nums">
                  {item.id}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  {item.ticker ?? "—"}
                </span>
                <SideChip side={item.side} />
                <span className="tabular-nums text-muted-foreground">
                  {item.cost != null
                    ? formatOrderAmount(item.cost, item.side)
                    : "—"}
                </span>
                <ApprovalStatusBadge status={item.status} />
                {/* Kết quả thực thi (contract §4 rev 3): order_id từ sàn khi
                    live; dry-fill hiển thị mode để phân biệt với lệnh thật. */}
                {item.order_id ? (
                  <span className="text-xs text-muted-foreground">
                    order {item.order_id}
                  </span>
                ) : item.status === "executed" && item.mode === "dry" ? (
                  <span className="text-xs text-muted-foreground">
                    dry-fill (chưa gửi sàn)
                  </span>
                ) : null}
                <span className="text-xs text-muted-foreground">
                  {item.resolved_at ? formatDateTime(item.resolved_at) : ""}
                </span>
                {item.resolution && (
                  <span className="w-full truncate text-xs text-muted-foreground">
                    Lý do: {item.resolution}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </CardContent>
      )}
    </Card>
  );
}
