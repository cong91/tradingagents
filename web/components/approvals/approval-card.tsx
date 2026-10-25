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
import { useT } from "@/lib/i18n";

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
  const t = useT();
  const c = t.approvals.card;
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
            {c.proposedAt(item.created_at ? formatDateTime(item.created_at) : "—")}
          </span>
          {item.expires_at ? (
            <>
              <span aria-hidden="true">·</span>
              <span>{c.deadline(formatDateTime(item.expires_at))}</span>
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
          <Field label={c.quantity}>
            {item.quantity != null
              ? formatQuantity(item.quantity, item.ccxt_symbol, item.ticker)
              : "—"}
          </Field>
          <Field label={c.estPrice}>
            {item.price_est != null ? formatPrice(item.price_est) : "—"}
          </Field>
          <Field label={c.orderValue}>
            {item.cost != null ? (
              <span className="tabular-nums">
                {formatOrderAmount(item.cost, item.side)}
              </span>
            ) : (
              "—"
            )}
          </Field>
          <Field label={c.market}>
            {item.market === "spot" ? "Spot" : (item.market ?? "—")}
            {hasLeverage && item.leverage != null ? c.leverage(item.leverage) : ""}
          </Field>
          <Field label={c.stopLoss}>
            {item.stop_loss != null ? (
              <span className="tabular-nums text-destructive">
                {formatPrice(item.stop_loss)}
              </span>
            ) : (
              "—"
            )}
          </Field>
          <Field label={c.takeProfit}>
            {item.take_profit != null ? (
              <span className="tabular-nums text-emerald-700 dark:text-emerald-400">
                {formatPrice(item.take_profit)}
              </span>
            ) : (
              "—"
            )}
          </Field>
        </dl>
        {(item.plan_reason || item.mode_at_plan) && (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            <PlanModeBadge mode={item.mode_at_plan} />
            {item.plan_reason && (
              <span className="text-muted-foreground">{c.reason(item.plan_reason)}</span>
            )}
          </div>
        )}
        {item.mock ? (
          <p className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-2.5 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
            {c.mockNote}
          </p>
        ) : null}
      </CardContent>
      <CardFooter className="flex-wrap gap-2">
        <Button
          className="min-touch"
          disabled={actionsDisabled}
          onClick={() => onApprove(item)}
        >
          {c.approve}
        </Button>
        <Button
          variant="destructive"
          className="min-touch"
          disabled={actionsDisabled}
          onClick={() => onReject(item)}
        >
          {c.reject}
        </Button>

      </CardFooter>
    </Card>
  );
}
