"use client";

// Panel "Quét pipeline" của màn Scanner: chỉ lo *khởi động* một lần quét
// (contract §5) — chọn kiểu quét, gửi POST /api/daily, và nói cho người dùng
// biết khi worker đang bận. Việc theo dõi job (poll + render kết quả) nằm ở
// active-job-card.tsx; lịch sử nằm ở job-history-panel.tsx.
//
// Khi mở màn, panel tự nhận job đang chạy từ GET /api/daily (rev 7) nên không
// còn cảnh "không biết có job đang chạy" cho tới khi bị từ chối 409.

import { useCallback, useState } from "react";
import { Loader2, Play, ScanSearch } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ActiveJobCard } from "./active-job-card";
import { JobHistoryPanel } from "./job-history-panel";
import { ApiHttpError, scannerApi, toApiHttpError } from "./scanner-api";
import { useJobHistory } from "./use-job-history";
import type { DailyJobStatus } from "./scanner-types";

type ScanKind = "watchlist" | "auto";

export function ScanPanel({ symbolsCount }: { symbolsCount: number }) {
  const [starting, setStarting] = useState<ScanKind | null>(null);
  const [error, setError] = useState<ApiHttpError | null>(null);
  // Job đã chọn tường minh: do phiên này tạo, hoặc do bấm từ lịch sử.
  const [pickedJobId, setPickedJobId] = useState<string | null>(null);
  const [watchedStatus, setWatchedStatus] = useState<DailyJobStatus | null>(null);
  const {
    jobs: historyJobs,
    activeJobId,
    loading: historyLoading,
    error: historyError,
    refresh: refreshHistory,
  } = useJobHistory();

  // Suy ra thay vì đồng bộ bằng effect: job đang chạy luôn thắng (nó là việc
  // gấp nhất), còn lại giữ job vừa xem để kết quả không biến mất sau khi xong.
  // Nhờ vậy mở màn giữa chừng là thấy ngay job đang chạy (rev 7).
  const watchedJobId = activeJobId ?? pickedJobId;

  const handleSettled = useCallback(
    (jobId: string) => {
      setPickedJobId((prev) => prev ?? jobId);
      refreshHistory();
    },
    [refreshHistory]
  );
  const handleStatusChange = useCallback(
    (status: DailyJobStatus | null) => setWatchedStatus(status),
    []
  );

  const startScan = useCallback(
    async (scanKind: ScanKind) => {
      setStarting(scanKind);
      setError(null);
      try {
        const res =
          scanKind === "auto"
            ? await scannerApi.startAutoDaily(5)
            : await scannerApi.startDaily();
        setWatchedStatus(res.status);
        setPickedJobId(res.job_id);
        refreshHistory();
      } catch (err) {
        setError(toApiHttpError(err));
        refreshHistory(); // 409 = có job khác đang chạy; lịch sử chỉ ra job nào
      } finally {
        setStarting(null);
      }
    },
    [refreshHistory]
  );

  const openJob = useCallback((jobId: string) => {
    setError(null);
    setWatchedStatus(null);
    setPickedJobId(jobId);
  }, []);

  const busy =
    starting !== null ||
    (watchedStatus !== null && (watchedStatus === "queued" || watchedStatus === "running"));

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Quét pipeline</CardTitle>
          <CardDescription>
            Quét tự động: server tự sàng universe (crypto top khối lượng sàn +
            cổ phiếu Mỹ đang mạnh 52 tuần) bằng bộ tiêu chí thực chiến — động
            lượng, RSI, đỉnh 52 tuần, breakout kèm khối lượng, thanh khoản —
            rồi chạy pipeline LLM trên các ứng viên điểm cao nhất. Không cần
            danh sách theo dõi. “Quét ngay” chạy trên watchlist hiện hành; mỗi
            mã là một pipeline LLM đầy đủ nên job có thể mất nhiều phút.
          </CardDescription>
          <CardAction className="flex items-center gap-2">
            <Button
              variant="outline"
              className="min-touch"
              onClick={() => void startScan("auto")}
              disabled={busy}
            >
              {starting === "auto" ? (
                <Loader2 className="animate-spin" aria-hidden />
              ) : (
                <ScanSearch data-icon="inline-start" aria-hidden />
              )}
              {starting === "auto" ? "Đang gửi…" : "Quét tự động"}
            </Button>
            <Button
              className="min-touch"
              onClick={() => void startScan("watchlist")}
              disabled={symbolsCount === 0 || busy}
            >
              {starting === "watchlist" ? (
                <Loader2 className="animate-spin" aria-hidden />
              ) : (
                <Play data-icon="inline-start" aria-hidden />
              )}
              {starting === "watchlist" ? "Đang gửi…" : "Quét ngay"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-3">
          {symbolsCount === 0 ? (
            <p className="text-sm text-muted-foreground">
              Cần ít nhất một mã trong danh sách theo dõi trước khi “Quét ngay” —
              hoặc bấm “Quét tự động” để server tự tìm mã.
            </p>
          ) : null}

          {error ? (
            <div role="alert" className="space-y-2 rounded-lg border border-destructive/40 p-3">
              <p className="text-sm text-destructive">{error.message}</p>
              {error.status === 409 ? (
                <p className="text-sm text-muted-foreground">
                  Đang có job khác chiếm worker — xem “Lịch sử quét” bên dưới để
                  biết job nào đang chạy và dừng nó nếu cần.
                </p>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <ActiveJobCard
        key={watchedJobId ?? "no-job"}
        jobId={watchedJobId}
        onStatusChange={handleStatusChange}
        onSettled={handleSettled}
      />

      <JobHistoryPanel
        jobs={historyJobs}
        activeJobId={activeJobId}
        loading={historyLoading}
        error={historyError}
        onOpen={openJob}
        onRefresh={refreshHistory}
      />
    </>
  );
}
