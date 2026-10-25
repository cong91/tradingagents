"use client";

// Màn M4 — Duyệt lệnh: hàng đợi phê duyệt kế hoạch lệnh (contract §4).
// Một hành động chính: duyệt (hoặc từ chối) từng kế hoạch; duyệt là con đường
// duy nhất đưa lệnh vào thực thi, từ chối không đụng sàn.

import { useState } from "react";
import Link from "next/link";
import {
  CircleCheckIcon,
  CircleXIcon,
  InboxIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  XIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

import { ApprovalCard } from "./approval-card";
import { ApproveDialog } from "./approve-dialog";
import { describeActionError, type ActionError } from "./errors";
import { formatDateTime } from "./format";
import { GateBanner, useGateHealth } from "./gate-banner";
import { RejectDialog } from "./reject-dialog";
import { ResolvedList } from "./resolved-list";
import { useApprovals } from "./use-approvals";
import type { ApprovalItem } from "./types";
import { useT } from "@/lib/i18n";

function ActionFeedback({
  lastAction,
  actionError,
  onDismiss,
}: {
  lastAction: ReturnType<typeof useApprovals>["lastAction"];
  actionError: ActionError | null;
  onDismiss: () => void;
}) {
  const t = useT();
  const f = t.approvals.feedback;
  if (actionError) {
    return (
      <div
        role="alert"
        className="mb-4 flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
      >
        <CircleXIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
        <p className="min-w-0 flex-1">{describeActionError(actionError)}</p>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onDismiss}
          aria-label={f.dismissError}
        >
          <XIcon />
        </Button>
      </div>
    );
  }
  if (!lastAction) return null;

  if (lastAction.kind === "approved") {
    // Cờ đỏ hợp đồng §4: order ĐÃ được đặt nhưng ghi audit thất bại — không được retry.
    if (lastAction.warning) {
      return (
        <div
          role="alert"
          className="mb-4 flex items-start gap-3 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100"
        >
          <CircleXIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
          <p className="min-w-0 flex-1">
            <strong>{f.warningLabel}</strong> {lastAction.warning} {f.warningTail(lastAction.id)}
          </p>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onDismiss}
            aria-label={f.dismiss}
          >
            <XIcon />
          </Button>
        </div>
      );
    }
    return (
      <div
        role="status"
        aria-live="polite"
        className="mb-4 flex items-start gap-3 rounded-lg border border-profit/40 bg-profit/10 p-3 text-sm"
      >
        <CircleCheckIcon className="mt-0.5 size-5 shrink-0 text-profit" aria-hidden="true" />
        <p className="min-w-0 flex-1">
          {f.approved(lastAction.id)}{" "}
          {lastAction.orderId
            ? f.orderId(lastAction.orderId)
            : lastAction.mode
              ? f.execMode(lastAction.mode === "live")
              : ""}{" "}
          {f.auditGranted}
        </p>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onDismiss}
          aria-label={f.dismiss}
        >
          <XIcon />
        </Button>
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="mb-4 flex items-start gap-3 rounded-lg border border-muted-foreground/30 bg-muted p-3 text-sm"
    >
      <CircleCheckIcon className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
      <p className="min-w-0 flex-1">
        {f.rejected(lastAction.id)}
      </p>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={onDismiss}
        aria-label={f.dismiss}
      >
        <XIcon />
      </Button>
    </div>
  );
}

export function ApprovalsScreen() {
  const t = useT();
  const q = t.approvals.queue;
  const [approveTarget, setApproveTarget] = useState<ApprovalItem | null>(null);
  const [rejectTarget, setRejectTarget] = useState<ApprovalItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [dialogError, setDialogError] = useState<ActionError | null>(null);
  const [actionError, setActionError] = useState<ActionError | null>(null);

  const gate = useGateHealth();
  const paused = approveTarget !== null || rejectTarget !== null;
  const {
    snapshot,
    initialLoading,
    loadError,
    stale,
    refreshing,
    refresh,
    approve,
    reject,
    lastAction,
    clearLastAction,
  } = useApprovals({ paused });

  const hasData = snapshot !== null;
  const pendingItems = snapshot?.pending ?? [];
  const resolvedItems = snapshot?.resolved ?? [];

  function openApprove(item: ApprovalItem) {
    setActionError(null);
    setDialogError(null);
    setApproveTarget(item);
  }

  function openReject(item: ApprovalItem) {
    setActionError(null);
    setDialogError(null);
    setRejectTarget(item);
  }

  async function confirmApprove() {
    if (!approveTarget || submitting) return;
    setSubmitting(true);
    setDialogError(null);
    const result = await approve(approveTarget.id);
    setSubmitting(false);
    if (result.ok) {
      setApproveTarget(null);
      return;
    }
    const err: ActionError = {
      code: result.code,
      message: result.message,
      details: result.details,
    };
    if (result.code === "conflict" || result.code === "expired") {
      // Item đã được xử lý nơi khác — đóng dialog, banner phía trên giải thích.
      setApproveTarget(null);
      setActionError(err);
    } else {
      setDialogError(err);
    }
  }

  async function confirmReject(reason: string | null) {
    if (!rejectTarget || submitting) return;
    setSubmitting(true);
    setDialogError(null);
    const result = await reject(rejectTarget.id, reason);
    setSubmitting(false);
    if (result.ok) {
      setRejectTarget(null);
      return;
    }
    const err: ActionError = {
      code: result.code,
      message: result.message,
      details: result.details,
    };
    if (result.code === "conflict" || result.code === "expired") {
      setRejectTarget(null);
      setActionError(err);
    } else {
      setDialogError(err);
    }
  }

  return (
    <div>
      <GateBanner gate={gate} />
      <ActionFeedback
        lastAction={lastAction}
        actionError={actionError}
        onDismiss={() => {
          setActionError(null);
          clearLastAction();
        }}
      />

      <Card>
        <CardHeader>
          <CardTitle>{q.title}</CardTitle>
          <CardDescription>{q.description}</CardDescription>
          <CardAction>
            <div className="flex items-center gap-2">
              {stale && (
                <Badge
                  variant="outline"
                  className="border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
                >
                  {t.common.staleData}
                </Badge>
              )}
              {hasData && snapshot.generated_at && (
                <span className="hidden text-xs text-muted-foreground sm:inline">
                  {q.updated(formatDateTime(snapshot.generated_at))}
                </span>
              )}
              <Button
                variant="outline"
                size="sm"
                className="min-touch"
                onClick={() => void refresh()}
                disabled={refreshing}
              >
                {refreshing ? (
                  <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
                ) : (
                  <RefreshCwIcon aria-hidden="true" />
                )}
                {t.common.refresh}
              </Button>
            </div>
          </CardAction>
        </CardHeader>
        <CardContent>
          {initialLoading && !hasData ? (
            // loading — skeleton 3 hàng
            <div className="space-y-2" aria-busy="true" aria-live="polite">
              <span className="sr-only">{q.loading}</span>
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-16 animate-pulse rounded-lg bg-muted" />
              ))}
            </div>
          ) : loadError && !hasData ? (
            // error — hiển thị message + nút thử lại
            <div
              role="alert"
              className="flex flex-col items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
            >
              <p className="font-medium text-destructive">
                {q.loadFailed}
              </p>
              <p className="text-muted-foreground">{loadError}</p>
              <p className="text-xs text-muted-foreground">
                {q.backendHint}{" "}
                <code className="rounded bg-muted px-1 py-0.5">
                  .venv\Scripts\python.exe -m uvicorn server.main:app --port 8000
                </code>
              </p>
              <Button
                variant="outline"
                className="min-touch"
                onClick={() => void refresh()}
              >
                {t.common.retry}
              </Button>
            </div>
          ) : pendingItems.length === 0 ? (
            // empty — nói rõ việc cần làm tiếp theo
            <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed p-8 text-center">
              <InboxIcon className="size-6 text-muted-foreground" aria-hidden="true" />
              <p className="font-medium">{q.emptyTitle}</p>
              <p className="max-w-md text-sm text-muted-foreground">{q.emptyBody}</p>
              <Button
                className="min-touch"
                nativeButton={false}
                render={<Link href="/scanner" />}
              >
                {q.openScanner}
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              {pendingItems.map((item) => (
                <ApprovalCard
                  key={item.id}
                  item={item}
                  actionsDisabled={submitting || paused}
                  onApprove={openApprove}
                  onReject={openReject}
                />
              ))}
            </div>
          )}

          {paused && hasData && (
            <p className="mt-3 text-xs text-muted-foreground">
              {q.paused}

            </p>
          )}
        </CardContent>
      </Card>

      <ResolvedList items={resolvedItems} />

      <ApproveDialog
        key={approveTarget?.id ?? "approve-none"}
        item={approveTarget}
        execMode={gate.status === "ready" ? gate.execMode : null}
        submitting={submitting}
        error={dialogError}
        onOpenChange={(open) => {
          if (!open) {
            setApproveTarget(null);
            setDialogError(null);
          }
        }}
        onConfirm={() => void confirmApprove()}
      />
      <RejectDialog
        key={rejectTarget?.id ?? "reject-none"}
        item={rejectTarget}
        submitting={submitting}
        error={dialogError}
        onOpenChange={(open) => {
          if (!open) {
            setRejectTarget(null);
            setDialogError(null);
          }
        }}
        onConfirm={(reason) => void confirmReject(reason)}
      />
    </div>
  );
}
