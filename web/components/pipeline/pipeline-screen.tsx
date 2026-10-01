"use client";

// Compose màn M2 Agent Pipeline: form chạy phiên + thẻ phiên (meta, tracker,
// kết quả) + thẻ luồng sự kiện. Mỗi trạng thái (loading/empty/error/stale)
// đều có hiển thị riêng theo hợp đồng §0.2 và §3.

import { LoaderCircleIcon, RadioTowerIcon, TriangleAlertIcon } from "lucide-react";
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
import { cn } from "cn";
import { EventFeed } from "./event-feed";
import { RunForm } from "./run-form";
import { RunSummary } from "./run-summary";
import { StageTracker } from "./stage-tracker";
import { useRunStream } from "./use-run-stream";
import type { RunPayload } from "./types";

const STATUS_LABELS: Record<string, string> = {
  queued: "Đang xếp hàng",
  running: "Đang chạy",
  completed: "Hoàn tất",
  failed: "Thất bại",
  cancelled: "Đã huỷ",
};

const ASSET_LABELS: Record<string, string> = {
  crypto: "Crypto",
  stock: "Cổ phiếu",
};

function RunMeta({ run }: { run: RunPayload }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <span>
        <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{run.run_id}</code>
      </span>
      <span className="font-medium">{run.ticker}</span>
      <span className="text-muted-foreground">{run.trade_date}</span>
      <span className="text-muted-foreground">
        {ASSET_LABELS[run.asset_type] ?? run.asset_type}
      </span>
      <Badge variant={run.mode === "mock" ? "outline" : "secondary"}>
        {run.mode === "mock" ? "Mock (không gọi LLM)" : "Live (gọi LLM thật)"}
      </Badge>
      <Badge
        variant="outline"
        aria-live="polite"
        className={cn(
          run.status === "failed" && "border-loss/40 text-loss",
          run.status === "completed" && "border-profit/40 text-profit",
        )}
      >
        {STATUS_LABELS[run.status] ?? run.status}
      </Badge>
      {run.analysts.length > 0 && (
        <span className="text-muted-foreground">Analyst: {run.analysts.join(", ")}</span>
      )}
    </div>
  );
}

function ErrorBox({
  message,
  activeRunId,
  onAttach,
}: {
  message: string;
  activeRunId?: string;
  onAttach: (runId: string) => void;
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
      <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-loss" />
      <div className="min-w-0">
        <p className="font-medium text-loss">Không chạy được phiên</p>
        <p className="mt-0.5 break-words">{message}</p>
        {activeRunId && (
          <Button
            variant="outline"
            size="sm"
            className="min-touch mt-2"
            onClick={() => onAttach(activeRunId)}
          >
            Theo dõi phiên đang chạy ({activeRunId})
          </Button>
        )}
      </div>
    </div>
  );
}

function StaleHint() {
  return (
    <p
      role="status"
      className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-800 dark:text-amber-200"
    >
      <TriangleAlertIcon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span>
        Không nhận được sự kiện mới hơn 60 giây trong khi phiên vẫn đang chạy. v1 chưa
        có nút huỷ phiên — hãy đợi thêm, kiểm tra log server, hoặc khởi động lại server.
      </span>
    </p>
  );
}

export function PipelineScreen() {
  const { state, startRun, attachRun, reset } = useRunStream();
  const { phase } = state;
  const busy = phase.phase === "starting" || phase.phase === "streaming";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Chạy phân tích mới</CardTitle>
          <CardDescription>
            {
              "POST /api/runs → GET /api/runs/{id}/events (SSE). Phiên chỉ phân tích — không bao giờ sinh lệnh giao dịch."
            }
          </CardDescription>
          {busy && <CardAction><Badge variant="secondary">Phiên đang chạy</Badge></CardAction>}
        </CardHeader>
        <CardContent>
          <RunForm disabled={busy} onSubmit={startRun} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Phiên phân tích</CardTitle>
          <CardDescription>
            Chạy đúng một phiên tại một thời điểm (worker duy nhất — hợp đồng §3).
          </CardDescription>
          {phase.phase === "streaming" && (
            <CardAction>
              {state.stale ? (
                <Badge className="bg-amber-500/10 text-amber-700 ring-1 ring-inset ring-amber-500/40 dark:text-amber-400">
                  <TriangleAlertIcon aria-hidden="true" />
                  Đang treo?
                </Badge>
              ) : (
                <Badge
                  variant={state.sseConnected ? "default" : "secondary"}
                  className={state.sseConnected ? undefined : "animate-pulse"}
                >
                  <RadioTowerIcon aria-hidden="true" />
                  {state.sseConnected ? "SSE trực tiếp" : "Đang kết nối lại…"}
                </Badge>
              )}
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          {phase.phase === "idle" && (
            <div className="flex h-40 items-center justify-center rounded-lg border border-dashed px-4 text-center text-sm text-muted-foreground">
              Chưa có phiên phân tích nào. Nhập mã tài sản ở trên và nhấn “Chạy phân
              tích” — chọn chế độ Mock để chạy thử mà không cần khoá LLM.
            </div>
          )}
          {phase.phase === "starting" && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircleIcon aria-hidden="true" className="size-4 animate-spin" />
              Đang gửi yêu cầu tới backend…
            </p>
          )}
          {(phase.phase === "streaming" || phase.phase === "completed" || phase.phase === "failed") &&
            state.run && (
              <>
                <RunMeta run={state.run} />
                {phase.phase === "streaming" && (
                  <>
                    {state.stale && <StaleHint />}
                    <StageTracker events={state.events} status={state.run.status} />
                  </>
                )}
                {(phase.phase === "completed" || phase.phase === "failed") && (
                  <RunSummary run={state.run} onNewRun={reset} />
                )}
              </>
            )}
          {phase.phase === "error" && (
            <ErrorBox
              message={phase.message}
              activeRunId={phase.activeRunId}
              onAttach={attachRun}
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Luồng sự kiện</CardTitle>
          <CardDescription>
            Mới nhất nằm cuối danh sách; mất kết nối SSE sẽ tự reconnect với
            Last-Event-ID và phát lại phần thiếu.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <EventFeed
            events={state.events}
            emptyText={
              phase.phase === "idle"
                ? "Chưa có sự kiện nào — bắt đầu phiên để xem luồng agent."
                : "Đang chờ sự kiện đầu tiên từ pipeline…"
            }
          />
        </CardContent>
      </Card>
    </div>
  );
}
