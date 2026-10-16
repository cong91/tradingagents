"use client";

// Lịch sử job quét (contract §5 rev 7): mọi lần chạy nằm trong database nên
// vẫn tra được sau khi restart, không còn là state in-memory biến mất cùng
// process. Một dòng = một lần chạy; bấm vào để xem lại kết quả từng mã.

import { History, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatTimestamp } from "./scanner-format";
import { jobStatusLabel } from "./scanner-format";
import type { DailyJobSummary } from "./scanner-types";

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "destructive" | "outline"
> = {
  queued: "outline",
  running: "default",
  completed: "secondary",
  failed: "destructive",
  cancelled: "outline",
  interrupted: "destructive",
};

export function JobHistoryPanel({
  jobs,
  activeJobId,
  loading,
  error,
  onOpen,
  onRefresh,
}: {
  jobs: DailyJobSummary[];
  activeJobId: string | null;
  loading: boolean;
  error: string | null;
  onOpen: (jobId: string) => void;
  onRefresh: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <History className="size-4" aria-hidden />
          Lịch sử quét
        </CardTitle>
        <CardDescription>
          Mọi lần quét được lưu lại — mở lại xem tín hiệu và kế hoạch của từng
          mã bất cứ lúc nào, kể cả sau khi khởi động lại server.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        {loading && jobs.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Đang tải lịch sử…
          </p>
        ) : null}

        {!loading && jobs.length === 0 ? (
          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
            Chưa có lần quét nào được ghi lại.
          </p>
        ) : null}

        {jobs.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Lịch sử các lần quét pipeline</caption>
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-2 pr-3 font-medium">Thời gian</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Kiểu</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Trạng thái</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Mã</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Chờ duyệt</th>
                  <th scope="col" className="py-2 font-medium"></th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {jobs.map((job) => (
                  <tr key={job.job_id} className="align-top">
                    <td className="py-2 pr-3 whitespace-nowrap">
                      {formatTimestamp(job.created_at)}
                    </td>
                    <td className="py-2 pr-3">
                      <Badge variant="outline">
                        {job.auto ? `Tự động · top ${job.top_n ?? "?"}` : "Watchlist"}
                      </Badge>
                      {job.mode === "mock" ? (
                        <Badge variant="secondary" className="ml-1">
                          Mock
                        </Badge>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3">
                      <Badge variant={STATUS_VARIANT[job.status] ?? "outline"}>
                        {jobStatusLabel(job.status)}
                      </Badge>
                      {job.status === "running" && job.job_id === activeJobId ? (
                        <Loader2
                          className="ml-2 inline size-3.5 animate-spin align-middle"
                          aria-hidden
                        />
                      ) : null}
                    </td>
                    <td className="py-2 pr-3 max-w-xs truncate" title={job.tickers.join(", ")}>
                      {job.results_count} mã
                      {job.tickers.length > 0 ? ` · ${job.tickers.join(", ")}` : ""}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {job.approval_count > 0 ? job.approval_count : "—"}
                    </td>
                    <td className="py-2">
                      <Button
                        variant="outline"
                        size="sm"
                        className="min-touch"
                        onClick={() => onOpen(job.job_id)}
                      >
                        Xem
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        <Button variant="outline" size="sm" className="min-touch" onClick={onRefresh}>
          Làm mới
        </Button>
      </CardContent>
    </Card>
  );
}
