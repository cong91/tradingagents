"use client";

// Bảy giai đoạn pipeline theo đúng thứ tự engine: analyst → tranh luận →
// research manager → trader → tranh luận rủi ro → PM → quyết định cuối.
// Chỉ các event tường minh mới chuyển giai đoạn (khớp _steps_from_state —
// server/runs.py:160-200); message/tool_call chỉ là hoạt động, không nhảy bậc.

import {
  CircleCheckIcon,
  CircleIcon,
  CircleXIcon,
  LoaderCircleIcon,
} from "lucide-react";
import { cn } from "cn";
import type { RunEvent, RunEventType, RunStatus } from "./types";

const STAGES = [
  "Analyst (phân tích thị trường)",
  "Tranh luận Bull/Bear",
  "Research Manager",
  "Trader",
  "Tranh luận rủi ro",
  "Quản lý danh mục (PM)",
  "Quyết định cuối",
] as const;

const ADVANCING: Partial<Record<RunEventType, number>> = {
  debate_update: 1,
  research_manager: 2,
  trader_update: 3,
  risk_update: 4,
  pm_decision: 5,
  run_completed: 6,
};

export function currentStageIndex(events: RunEvent[]): number {
  let index = -1;
  for (const event of events) {
    if (
      index < 0 &&
      (event.type === "run_started" ||
        event.type === "analyst_status" ||
        event.type === "message" ||
        event.type === "tool_call")
    ) {
      index = 0;
    }
    const advanced = ADVANCING[event.type];
    if (advanced !== undefined) index = Math.max(index, advanced);
  }
  return index;
}

type StageStatus = "pending" | "active" | "done" | "failed";

const STAGE_ICON: Record<StageStatus, typeof CircleIcon> = {
  pending: CircleIcon,
  active: LoaderCircleIcon,
  done: CircleCheckIcon,
  failed: CircleXIcon,
};

const STAGE_ICON_CLASS: Record<StageStatus, string> = {
  pending: "text-muted-foreground/50",
  active: "text-foreground",
  done: "text-profit",
  failed: "text-loss",
};

export function StageTracker({
  events,
  status,
}: {
  events: RunEvent[];
  status: RunStatus;
}) {
  const current = currentStageIndex(events);

  const statusFor = (index: number): StageStatus => {
    if (status === "failed") {
      if (index < current) return "done";
      if (index === current) return "failed";
      return "pending";
    }
    if (status === "completed") return "done";
    // cancelled: phiên dừng giữa chừng — chỉ các giai đoạn đã qua là done.
    if (status === "cancelled") return index < current ? "done" : "pending";
    if (index < current) return "done";
    if (index === current) return "active";
    return "pending";
  };

  return (
    <ol className="space-y-1.5">
      {STAGES.map((label, index) => {
        const stageStatus = statusFor(index);
        const Icon = STAGE_ICON[stageStatus];
        return (
          <li key={label} className="flex items-center gap-2 text-sm">
            <Icon
              aria-hidden="true"
              className={cn(
                "size-4 shrink-0",
                STAGE_ICON_CLASS[stageStatus],
                stageStatus === "active" && "animate-spin",
              )}
            />
            <span
              className={cn(
                stageStatus === "active" && "font-medium",
                (stageStatus === "pending" || stageStatus === "done") &&
                  "text-muted-foreground",
                stageStatus === "failed" && "font-medium text-loss",
              )}
            >
              {label}
            </span>
            {stageStatus === "active" && (
              <span className="sr-only">(đang chạy)</span>
            )}
            {stageStatus === "failed" && <span className="sr-only">(thất bại)</span>}
          </li>
        );
      })}
    </ol>
  );
}
