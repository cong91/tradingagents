"use client";

// Nhật ký sự kiện SSE của phiên: mới nhất nằm cuối, tự cuộn xuống đáy.
// Nội dung dài bị chặn 4 dòng, mở rộng bằng nút riêng cho từng dòng.

import { useEffect, useRef, useState } from "react";
import { cn } from "cn";
import type { RunEvent } from "./types";
import { formatClock } from "./format";
import { useT, type Messages } from "@/lib/i18n";

function eventTitle(t: Messages, event: RunEvent): string {
  const agent = event.agent ? ` · ${event.agent}` : "";
  return `${t.pipeline.events.types[event.type] ?? event.type}${agent}`;
}

function eventBody(t: Messages, event: RunEvent): string | null {
  const payload = event.payload;
  const e = t.pipeline.events;
  switch (event.type) {
    case "message":
      return typeof payload["content"] === "string"
        ? (payload["content"] as string)
        : null;
    case "tool_call": {
      const tool = typeof payload["tool"] === "string" ? payload["tool"] : "unknown";
      let args = "{}";
      try {
        args = JSON.stringify(payload["args"] ?? {});
      } catch {
        // args không tuần tự hoá được — hiển thị rỗng.
      }
      return `${tool} ${args}`;
    }
    case "analyst_status": {
      const agent = typeof payload["agent"] === "string" ? payload["agent"] : "";
      return payload["status"] === "completed" ? e.analystDone(agent) : e.analystRunning(agent);
    }
    case "debate_update":
    case "research_manager":
    case "trader_update":
    case "risk_update":
    case "pm_decision":
      return typeof payload["content"] === "string"
        ? (payload["content"] as string)
        : null;
    case "run_started": {
      const analysts = Array.isArray(payload["analysts"])
        ? (payload["analysts"] as unknown[]).filter((a) => typeof a === "string").join(", ")
        : "";
      return analysts !== "" ? e.analystsJoined(analysts) : null;
    }
    case "run_completed": {
      const signal = typeof payload["signal"] === "string" ? payload["signal"] : "?";
      return e.finalSignal(signal);
    }
    case "run_failed": {
      const error = payload["error"];
      const message =
        error && typeof error === "object" && typeof (error as { message?: unknown }).message === "string"
          ? (error as { message: string }).message
          : t.pipeline.summary.failedUnknown;
      return e.error(message);
    }
    case "run_interrupted": {
      const error = payload["error"];
      const message =
        error && typeof error === "object" && typeof (error as { message?: unknown }).message === "string"
          ? (error as { message: string }).message
          : t.pipeline.summary.interruptedUnknown;
      return e.interrupted(message);
    }
  }
}

const PREVIEW_LIMIT = 220;

function EventRow({ event }: { event: RunEvent }) {
  const [expanded, setExpanded] = useState(false);
  const t = useT();
  const body = eventBody(t, event);
  const tone =
    event.type === "run_failed"
      ? "text-loss"
      : event.type === "run_completed"
        ? "text-profit"
        : "text-foreground";
  return (
    <li className="border-b border-border/60 py-2 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
        <span className="tabular-nums">{formatClock(event.ts)}</span>
        <span className={cn("font-medium", tone)}>{eventTitle(t, event)}</span>
        {event.replay && (
          <span className="rounded-full border px-1.5 text-[10px] uppercase tracking-wide">
            mock
          </span>
        )}
      </div>
      {body !== null && (
        <p
          className={cn(
            "mt-1 whitespace-pre-wrap break-words text-sm",
            !expanded && "line-clamp-4",
          )}
        >
          {body}
        </p>
      )}
      {body !== null && body.length > PREVIEW_LIMIT && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          className="mt-1 min-h-6 text-xs font-medium text-primary underline-offset-2 hover:underline"
        >
          {expanded ? t.pipeline.events.collapse : t.pipeline.events.expand}

        </button>
      )}
    </li>
  );
}

export function EventFeed({
  events,
  emptyText,
}: {
  events: RunEvent[];
  emptyText: string;
}) {
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [events]);

  if (events.length === 0) {
    return (
      <div className="flex h-40 items-center justify-center rounded-lg border border-dashed px-4 text-center text-sm text-muted-foreground">
        {emptyText}
      </div>
    );
  }
  return (
    <div
      ref={boxRef}
      className="max-h-96 overflow-y-auto rounded-lg border bg-background px-3"
    >
      <ol>
        {events.map((event) => (
          <EventRow key={event.seq} event={event} />
        ))}
      </ol>
    </div>
  );
}
