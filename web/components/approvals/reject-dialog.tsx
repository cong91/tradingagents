"use client";

// Dialog TỪ CHỐI kế hoạch — không đụng sàn (hợp đồng §4), lý do được ghi vào
// audit (approval_denied). Thao tác một bước, lý do là tùy chọn nhưng được khuyến khích.

import { useState } from "react";
import { LoaderCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { describeActionError, type ActionError } from "./errors";
import type { ApprovalItem } from "./types";
import { useT } from "@/lib/i18n";

export function RejectDialog({
  item,
  submitting,
  error,
  onOpenChange,
  onConfirm,
}: {
  item: ApprovalItem | null;
  submitting: boolean;
  error: ActionError | null;
  onOpenChange: (open: boolean) => void;
  onConfirm: (reason: string | null) => void;
}) {
  const [reason, setReason] = useState("");
  const t = useT();
  const c = t.approvals.rejectDialog;

  const trimmed = reason.trim();
  const canSubmit = !submitting; // lý do tùy chọn — server/approvals.py:180-182

  return (
    <Dialog
      open={item !== null}
      onOpenChange={(open) => {
        if (!open) setReason("");
        onOpenChange(open);
      }}
    >
      {item && (
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{c.title(item.id)}</DialogTitle>
            <DialogDescription>{c.description}</DialogDescription>
          </DialogHeader>

          <div className="grid gap-1.5">
            <label htmlFor="reject-reason" className="text-sm font-medium">
              {c.reasonLabel}
            </label>
            <textarea
              id="reject-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              placeholder={c.reasonPlaceholder}
              className="min-h-11 rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>

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
              {t.approvals.approveDialog.cancel}
            </Button>
            <Button
              variant="destructive"
              className="min-touch"
              disabled={!canSubmit}
              onClick={() => onConfirm(trimmed || null)}
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
