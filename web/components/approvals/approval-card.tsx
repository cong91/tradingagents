"use client";

// Card một kế hoạch lệnh đang chờ duyệt (GET /api/approvals, status=pending).

import { ClockIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

import { formatDateTime, formatOrderAmount, formatPrice, formatQuantity } from "./format";
import {
  ExpiredBadge,
  MockPlanBadge,
  PlanModeBadge,
  SideChip,
  SignalBadge,
} from "./labels";
import type { ApprovalItem } from "./types";

/** Badge "quá hạn" là suy đoán phía client (contract §4): item còn pending
 * nhưng expires_at đã qua. Server không lưu trạng thái này. */
function isExpired(item: ApprovalItem): boolean {
  if (item.status !== "pending" || !item.expires_at) return false;
  const expiry = Date.parse(item.expires_at);
  return !Number.isNaN(expiry) && expiry <= Date.now();
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="truncate text-sm font-medium">{children}</dd>
    </div>
  );
}

export function ApprovalCard({
  item,
  actionsDisabled,
  onApprove,
  onReject,
}: {
  item: ApprovalItem;
  actionsDisabled: boolean;
  onApprove: (item: ApprovalItem) => void;
  onReject: (item: ApprovalItem) => void;
}) {
  const ticker = item.ticker ?? item.id;
  const hasLeverage = item.leverage != null && item.leverage > 1;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span className="font-heading text-base">{ticker}</span>
          <SideChip side={item.side} />
          <SignalBadge signal={item.signal} />
          {item.mock ? <MockPlanBadge /> : null}
          {isExpired(item) ? <ExpiredBadge /> : null}
        </CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-x-2">
          <span className="inline-flex items-center gap-1">
            <ClockIcon className="size-3.5" aria-hidden="true" />
            Đề xuất lúc {item.created_at ? formatDateTime(item.created_at) : "—"}
          </span>
          {item.expires_at ? (
            <>
              <span aria-hidden="true">·</span>
              <span>Hạn duyệt {formatDateTime(item.expires_at)}</span>
            </>
          ) : null}
          <span aria-hidden="true">·</span>
          <span>
            ID {item.id}
            {item.daily_job_id ? ` · job ${item.daily_job_id}` : ""}
            {item.run_id ? ` · run ${item.run_id}` : ""}
          </span>
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="Khối lượng">
            {item.quantity != null
              ? formatQuantity(item.quantity, item.ccxt_symbol, item.ticker)
              : "—"}
          </Field>
          <Field label="Giá ước tính">
            {item.price_est != null ? formatPrice(item.price_est) : "—"}
          </Field>
          <Field label="Giá trị lệnh">
            {item.cost != null ? (
              <span className="tabular-nums">
                {formatOrderAmount(item.cost, item.side)}
              </span>
            ) : (
              "—"
            )}
          </Field>
          <Field label="Thị trường">
            {item.market === "spot" ? "Spot" : (item.market ?? "—")}
            {hasLeverage ? ` · đòn bẩy ${item.leverage}×` : ""}
          </Field>
        </dl>
        {(item.plan_reason || item.mode_at_plan) && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            <PlanModeBadge mode={item.mode_at_plan} />
            {item.plan_reason && (
              <span className="text-muted-foreground">Lý do: {item.plan_reason}</span>
            )}
          </div>
        )}
        {item.mock ? (
          <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-2.5 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
            Kế hoạch này sinh từ job quét chế độ mô phỏng — dữ liệu tổng hợp
            không thể thành lệnh thật: server sẽ từ chối duyệt (409), chỉ có
            thể từ chối kế hoạch.
          </p>
        ) : null}
      </CardContent>
      <CardFooter className="flex-wrap gap-2">
        <Button
          className="min-touch"
          disabled={actionsDisabled}
          onClick={() => onApprove(item)}
        >
          Duyệt lệnh
        </Button>
        <Button
          variant="destructive"
          className="min-touch"
          disabled={actionsDisabled}
          onClick={() => onReject(item)}
        >
          Từ chối
        </Button>
      </CardFooter>
    </Card>
  );
}
