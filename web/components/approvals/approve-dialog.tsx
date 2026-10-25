"use client";

// Dialog xác nhận DUYỆT — hành động không thể hoàn tác (đưa kế hoạch vào đường
// thực thi duy nhất của hệ thống), nên xác nhận hai bước là bắt buộc; khác với
// thao tác reversable thì không đặt confirm.

import { LoaderCircleIcon, TriangleAlertIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "cn";

import { describeActionError, type ActionError } from "./errors";
import { formatDateTime, formatOrderAmount, formatPrice, formatQuantity } from "./format";
import { SideChip } from "./labels";
import type { ApprovalItem } from "./types";
import { useT } from "@/lib/i18n";

export function ApproveDialog({
  item,
  execMode,
  submitting,
  error,
  onOpenChange,
  onConfirm,
}: {
  item: ApprovalItem | null;
  execMode: "live" | "dry" | null;
  submitting: boolean;
  error: ActionError | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const t = useT();
  const c = t.approvals.approveDialog;
  return (
    <Dialog open={item !== null} onOpenChange={onOpenChange}>
      {item && (
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{c.title(item.id)}</DialogTitle>
            <DialogDescription>
              {execMode === "live" ? c.liveDescription : c.dryDescription}
            </DialogDescription>
          </DialogHeader>

          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div>
              <dt className="text-xs text-muted-foreground">{c.pair}</dt>
              <dd className="flex items-center gap-2 font-medium">
                {item.ticker ?? item.id} <SideChip side={item.side} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{t.approvals.card.quantity}</dt>
              <dd className="font-medium">
                {item.quantity != null
                  ? formatQuantity(item.quantity, item.ccxt_symbol, item.ticker)
                  : "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{t.approvals.card.estPrice}</dt>
              <dd className="font-medium">
                {item.price_est != null ? formatPrice(item.price_est) : "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">{t.approvals.card.orderValue}</dt>
              <dd className="font-medium tabular-nums">
                {item.cost != null ? formatOrderAmount(item.cost, item.side) : "—"}
              </dd>
            </div>
            <div className="col-span-2">
              <dt className="text-xs text-muted-foreground">{c.proposedAt}</dt>
              <dd>
                {item.created_at ? formatDateTime(item.created_at) : "—"}
              </dd>
            </div>
          </dl>

          {execMode === "live" && (
            <p className="flex items-start gap-2 rounded-lg border border-red-300 bg-red-50 p-2.5 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100">
              <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              {c.liveWarning}
            </p>
          )}

          {error && (
            <p
              role="alert"
              className="rounded-lg border border-destructive/40 bg-destructive/10 p-2.5 text-sm text-destructive"
            >
              {describeActionError(error)}
            </p>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              className="min-touch"
              disabled={submitting}
              onClick={() => onOpenChange(false)}
            >
              {c.cancel}
            </Button>
            <Button
              className={cn("min-touch", execMode === "live" && "bg-loss text-white hover:bg-loss/85")}
              disabled={submitting}
              onClick={onConfirm}
            >
              {submitting && (
                <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
              )}
              {submitting ? c.submitting : c.confirm}

            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  );
}
