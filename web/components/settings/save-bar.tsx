"use client";

import { CheckIcon, LoaderCircleIcon } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { formatTimeVi } from "./format-time";
import type { SettingsController } from "./use-settings";

// Lưu cài đặt là thao tác reversable (PUT lại giá trị cũ) → không confirm-dialog.
export function SaveBar({ controller }: { controller: SettingsController }) {
  const { dirtyCount, fieldErrors, saveState, resetChanges, save } = controller;
  const saving = saveState.kind === "saving";
  const fieldErrorCount = Object.keys(fieldErrors).length;

  let statusText: string;
  if (saveState.kind === "saving") {
    statusText = "Đang lưu thay đổi…";
  } else if (saveState.kind === "saved") {
    statusText = `Đã lưu lúc ${formatTimeVi(saveState.appliedAt)} — áp dụng cho phiên phân tích mới.`;
  } else if (saveState.kind === "failed") {
    statusText = saveState.message;
  } else if (dirtyCount > 0) {
    statusText = `${dirtyCount} thay đổi chưa lưu.`;
  } else {
    statusText = "Không có thay đổi nào chưa lưu.";
  }

  return (
    <div className="sticky bottom-0 z-30 -mx-4 border-t bg-background/95 px-4 py-3 backdrop-blur md:-mx-6 md:px-6">
      <div className="flex flex-wrap items-center gap-3">
        <p
          aria-live="polite"
          className={cn(
            "min-w-0 flex-1 text-sm",
            saveState.kind === "failed" && "text-destructive",
            saveState.kind === "saved" && "text-profit"
          )}
        >
          {statusText}
        </p>
        <div className="flex shrink-0 gap-2">
          <Button
            variant="outline"
            className="min-touch"
            onClick={resetChanges}
            disabled={saving || dirtyCount === 0}
          >
            Huỷ thay đổi
          </Button>
          <Button
            className="min-touch"
            onClick={() => void save()}
            disabled={saving || dirtyCount === 0 || fieldErrorCount > 0}
          >
            {saving ? (
              <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
            ) : (
              <CheckIcon aria-hidden="true" />
            )}
            Lưu cài đặt
          </Button>
        </div>
      </div>
    </div>
  );
}
