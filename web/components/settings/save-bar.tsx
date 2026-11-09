"use client";

import { CheckIcon, LoaderCircleIcon } from "lucide-react";
import { cn } from "cn";
import { Button, buttonVariants } from "@/components/ui/button";
import { formatTimeVi } from "./format-time";
import type { SettingsController } from "./use-settings";
import { useT } from "@/lib/i18n";

// Lưu cài đặt là thao tác reversable (PUT lại giá trị cũ) → không confirm-dialog.
export function SaveBar({
  controller,
  keys,
  scope = "settings",
}: {
  controller: SettingsController;
  keys?: readonly string[];
  scope?: string;
}) {
  const { dirtyKeys, saveState, serverAhead, reviewing, resetChanges, save, reviewLatest } = controller;
  const included = (key: string) => !keys || keys.includes(key);
  const dirtyCount = dirtyKeys.filter(included).length;
  const saving = saveState.kind === "saving";
  const activeState = saveState.kind === "uncertain"
    ? saveState
    : saveState.kind !== "idle" && saveState.scope === scope
      ? saveState
      : { kind: "idle" as const };
  const reviewRequired = activeState.kind === "uncertain" || serverAhead;
  const t = useT();
  const c = t.settings.save;

  let statusText: string;
  if (activeState.kind === "saving") {
    statusText = c.saving;
  } else if (activeState.kind === "failed") {
    statusText = activeState.message;
  } else if (activeState.kind === "uncertain") {
    statusText = c.uncertain;
  } else if (dirtyCount > 0) {
    statusText = c.unsaved(dirtyCount);
  } else if (activeState.kind === "saved") {
    statusText = c.saved(formatTimeVi(activeState.appliedAt));
  } else {
    statusText = c.clean;
  }

  return (
    <div className="border-t bg-background px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <p
          aria-live="polite"
          className={cn(
            "min-w-0 flex-1 text-sm",
            activeState.kind === "failed" && "text-destructive",
            activeState.kind === "saved" && dirtyCount === 0 && "text-profit"
          )}
        >
          {statusText}
        </p>
        <div className="flex shrink-0 gap-2">
          {serverAhead ? (
            <a href="#settings-conflict" className={buttonVariants({ variant: "outline", className: "min-touch" })}>
              {c.reviewLatest}
            </a>
          ) : reviewRequired ? (
            <Button
              variant="outline"
              className="min-touch"
              onClick={() => void reviewLatest()}
              disabled={saving || reviewing}
            >
              {c.reviewLatest}
            </Button>
          ) : null}
          <Button
            variant="outline"
            className="min-touch"
            onClick={() => resetChanges(keys)}
            disabled={saving || dirtyCount === 0}
          >
            {c.discard}
          </Button>
          <Button
            className="min-touch"
            onClick={() => void save(keys, scope)}
            disabled={saving || reviewing || dirtyCount === 0 || activeState.kind === "uncertain" || serverAhead}
          >
            {saving ? (
              <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
            ) : (
              <CheckIcon aria-hidden="true" />
            )}
            {c.submit}

          </Button>
        </div>
      </div>
    </div>
  );
}
