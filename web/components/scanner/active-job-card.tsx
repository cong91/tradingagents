"use client";

// Thẻ "Kết quả quét": poll một job và render mọi thứ thuộc về nó — trạng thái,
// bảng sàng lọc, kết quả từng mã, dòng tổng kết, và nút Dừng (contract §5).
// Tách khỏi scan-panel.tsx để panel kia chỉ còn lo *khởi động* quét: vòng poll
// cộng phần hiển thị kết quả là một trách nhiệm riêng, không phải việc của nút
// bấm. Job đã xong vẫn xem lại được (dữ liệu đọc từ database — §5 rev 7).

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Loader2, Square, TriangleAlert } from "lucide-react";
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
import { ApiHttpError, scannerApi, toApiHttpError } from "./scanner-api";
import {
  cashFlowLabel,
  elapsedLabel,
  formatCashFlow,
  formatQty,
  formatUsd,
  isTerminalStatus,
  jobStatusLabel,
  sideLabel,
} from "./scanner-format";
import type {
  DailyJobStatus,
  DailyJobStatusResponse,
  DailyResultItem,
  ScreeningInfo,
} from "./scanner-types";
import { formatNumber } from "@/lib/format";
import { useT } from "@/lib/i18n";

const POLL_INTERVAL_MS = 3_000;
const STUCK_AFTER_MS = 15 * 60_000;

const SIGNAL_VARIANT: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  Buy: "default",
  Overweight: "secondary",
  Hold: "outline",
  Underweight: "destructive",
  Sell: "destructive",
};

export function ActiveJobCard({
  jobId,
  onStatusChange,
  onSettled,
}: {
  /** Job đang xem; null khi chưa chạy lần nào trong phiên này. */
  jobId: string | null;
  /** Mỗi lần trạng thái job đổi — panel khởi động dùng để biết worker có bận. */
  onStatusChange: (status: DailyJobStatus | null) => void;
  /** Job vừa chạm trạng thái kết thúc (một lần cho mỗi job) — panel khởi động
   *  làm mới lịch sử và giữ job lại để kết quả không biến mất. */
  onSettled: (jobId: string) => void;
}) {
  const [job, setJob] = useState<DailyJobStatusResponse | null>(null);
  const t = useT();
  const c = t.scanner.job;
  const [error, setError] = useState<ApiHttpError | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [cancelling, setCancelling] = useState(false);
  // Job đã báo kết thúc, để onSettled bắn đúng một lần dù poll còn chạy tiếp.
  const settledRef = useRef<string | null>(null);

  const status: DailyJobStatus | null = job?.status ?? null;
  const running = status !== null && !isTerminalStatus(status);

  // Không cần effect "xoá kết quả khi đổi job": nơi gọi truyền `key={jobId}`
  // nên React remount thẻ này khi job đổi, và state mới bắt đầu từ rỗng.
  useEffect(() => {
    if (jobId === null) {
      onStatusChange(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const next = await scannerApi.dailyJob(jobId);
        if (cancelled) return;
        setJob(next);
        setError(null);
        onStatusChange(next.status);
        if (isTerminalStatus(next.status) && settledRef.current !== jobId) {
          settledRef.current = jobId;
          onSettled(jobId);
        }
      } catch (err) {
        if (cancelled) return;
        setError(toApiHttpError(err));
      }
    };
    void tick();
    const id = setInterval(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [jobId, onStatusChange, onSettled]);

  // Đồng hồ thời lượng — chỉ chạy khi job còn sống.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [running]);

  const cancelJob = useCallback(async () => {
    if (jobId === null) return;
    setCancelling(true);
    try {
      const next = await scannerApi.cancelDailyJob(jobId);
      setJob(next);
      setError(null);
      onStatusChange(next.status);
      onSettled(jobId);
    } catch (err) {
      setError(toApiHttpError(err));
    } finally {
      setCancelling(false);
    }
  }, [jobId, onStatusChange, onSettled]);

  const isStuck =
    running &&
    job?.last_progress_at != null &&
    now - Date.parse(job.last_progress_at) > STUCK_AFTER_MS;
  const screening: ScreeningInfo | null | undefined = job?.screening;
  const screeningEmpty = screening != null && screening.candidates.length === 0;
  const approvalCount = job?.results.filter((r) => r.approval_id).length ?? 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{c.title}</CardTitle>
        <CardDescription>
          {jobId === null ? (
            <>{c.none}</>
          ) : (
            <>
              Job <span className="font-mono">{jobId}</span>
              {status ? ` — ${jobStatusLabel(status)}` : c.waitingStatus}
              {job && running ? c.elapsed(elapsedLabel(job.created_at, now)) : null}
            </>
          )}
        </CardDescription>
        {running ? (
          <CardAction>
            <Button
              variant="outline"
              className="min-touch"
              onClick={() => void cancelJob()}
              disabled={cancelling}
            >
              {cancelling ? (
                <Loader2 className="animate-spin" aria-hidden />
              ) : (
                <Square data-icon="inline-start" aria-hidden />
              )}
              {c.stop}
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? (
          <div role="alert" className="space-y-2 rounded-lg border border-destructive/40 p-3">
            <p className="text-sm text-destructive">{error.message}</p>
            {error.status === 404 ? (
              <p className="text-sm text-muted-foreground">
                {c.notFound}
              </p>
            ) : null}
          </div>
        ) : null}

        {running && job == null && !error ? (
          <p aria-live="polite" className="text-sm text-muted-foreground">
            {c.waitingFirst}
          </p>
        ) : null}

        {isStuck ? (
          <div className="flex items-start gap-2 rounded-lg bg-muted p-3 text-sm">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>{c.stuck(job?.current_ticker ?? null)}</span>
          </div>
        ) : null}

        {running && job?.screening == null ? (
          <p aria-live="polite" className="text-sm text-muted-foreground">
            {c.screening}
          </p>
        ) : null}

        {screening && screening.candidates.length > 0 ? (
          <ScreeningTable screening={screening} />
        ) : null}
        {screeningEmpty ? (
          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
            {c.screeningEmpty}
          </p>
        ) : null}

        {job && job.results.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">
                {c.caption}
              </caption>
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-2 pr-3 font-medium">{c.colPair}</th>
                  <th scope="col" className="py-2 pr-3 font-medium">{c.colSignal}</th>
                  <th scope="col" className="py-2 pr-3 font-medium">{c.colPlan}</th>
                  <th scope="col" className="py-2 pr-3 font-medium">{c.colCashFlow}</th>
                  <th scope="col" className="py-2 pr-3 font-medium">{c.colResult}</th>
                  <th scope="col" className="py-2 font-medium">{c.colApproval}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {job.results.map((item) => (
                  <ResultRow key={item.ticker} item={item} />
                ))}
              </tbody>
            </table>
            {job.status === "failed" ? (
              <p role="alert" className="mt-3 text-sm text-destructive">
                {c.failedPartial(job.error ?? null)}
              </p>
            ) : null}
          </div>
        ) : null}

        {job && job.results.length === 0 && !running ? (
          <div className="flex h-40 items-center justify-center rounded-lg border border-dashed p-4 text-center text-sm text-muted-foreground">
            {job.status === "failed"
              ? c.failedEmpty(job.error ?? null)
              : job.status === "cancelled"
                ? c.cancelledEmpty
                : job.status === "interrupted"
                  ? c.interruptedEmpty
                  : c.noResults}
          </div>
        ) : null}

        {job && !running ? (
          // Job xong → dẫn thẳng sang màn Duyệt lệnh nơi kế hoạch đang chờ.
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-dashed p-3 text-sm">
            <span>{c.summary(jobStatusLabel(job.status).toLowerCase(), approvalCount)}</span>
            <Link
              href="/approvals"
              className="min-touch inline-flex items-center rounded-lg border px-3 text-sm font-medium hover:bg-muted"
            >
              {c.viewApprovals}
            </Link>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ResultRow({ item }: { item: DailyResultItem }) {
  const plan = item.plan;
  const t = useT();
  return (
    <tr className="align-top">
      <td className="py-2 pr-3 font-mono font-medium">
        {item.ticker}
        {item.mock_source ? (
          <Badge variant="outline" className="ml-2 align-middle text-xs">
            Mock
          </Badge>
        ) : null}
        {item.asset_type === "stock" ? (
          <Badge variant="secondary" className="ml-2 align-middle text-xs">
            {t.scanner.job.stock}
          </Badge>
        ) : null}
      </td>
      <td className="py-2 pr-3">
        {item.signal ? (
          <Badge variant={SIGNAL_VARIANT[item.signal] ?? "outline"}>
            {item.signal}
          </Badge>
        ) : (
          "—"
        )}
      </td>
      <td className="py-2 pr-3">
        {plan ? (
          <span className="whitespace-nowrap">
            {sideLabel(plan.side)} · {formatQty(plan.quantity, item.ticker)} @{" "}
            {formatUsd(plan.price_est)}
            {plan.stop_loss != null ? (
              <span className="ml-2 text-xs text-destructive">
                SL {formatUsd(plan.stop_loss)}
              </span>
            ) : null}
            {plan.take_profit != null ? (
              <span className="ml-2 text-xs text-emerald-700 dark:text-emerald-400">
                TP {formatUsd(plan.take_profit)}
              </span>
            ) : null}
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="py-2 pr-3">
        {plan ? (
          <span className="whitespace-nowrap">
            {formatCashFlow(plan.side, plan.cost)}{" "}
            <span className="text-xs text-muted-foreground">
              ({cashFlowLabel(plan.side)})
            </span>
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="max-w-xs py-2 pr-3">
        {/* Job UI không bao giờ tự thực thi (bất biến §5) — cột này chỉ mang
            lý do nghiệp vụ hoặc lỗi hệ thống của coin. */}
        <span className="line-clamp-2" title={item.error ?? item.reason ?? undefined}>
          {item.error ?? item.reason ?? "—"}
        </span>
      </td>
      <td className="py-2">
        {item.approval_id ? (
          <Link
            href="/approvals"
            className="min-touch inline-flex items-center text-sm underline underline-offset-4"
          >
            {t.scanner.job.viewQueue}
          </Link>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}

/** Bảng ứng viên sau bước sàng lọc kỹ thuật (job auto, contract §5 rev 4). */
function ScreeningTable({ screening }: { screening: ScreeningInfo }) {
  const t = useT();
  const c = t.scanner.job;
  const fixed = (value: number, digits: number) =>
    formatNumber(value, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="outline">{c.screeningResult}</Badge>
        <span className="text-muted-foreground">
          {c.universe(
            screening.universe_crypto,
            screening.universe_stock,
            screening.universe_count,
            screening.candidates.length,
            Boolean(screening.mock)
          )}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">
            {c.screeningCaption}
          </caption>
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-medium">{c.colTicker}</th>
              <th scope="col" className="py-2 pr-3 font-medium">{c.colType}</th>
              <th scope="col" className="py-2 pr-3 font-medium">{c.colScore}</th>
              <th scope="col" className="py-2 pr-3 font-medium">{c.colMomentum}</th>
              <th scope="col" className="py-2 pr-3 font-medium">RSI 14</th>
              <th scope="col" className="py-2 pr-3 font-medium">RVOL</th>
              <th scope="col" className="py-2 pr-3 font-medium">{c.colVs52w}</th>
              <th scope="col" className="py-2 font-medium">{c.colReason}</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {screening.candidates.map((candidate) => (
              <tr key={candidate.ticker} className="align-top">
                <td className="py-2 pr-3 font-mono font-medium">
                  {candidate.ticker}
                  {candidate.breakout ? (
                    <Badge className="ml-2 align-middle text-xs">{c.breakout}</Badge>
                  ) : null}
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={candidate.asset_type === "crypto" ? "default" : "secondary"}>
                    {candidate.asset_type === "crypto" ? "Crypto" : c.stock}
                  </Badge>
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {fixed(candidate.score, 1)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {candidate.momentum_pct >= 0 ? "+" : ""}
                  {fixed(candidate.momentum_pct, 1)}%
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {candidate.rsi14 != null ? fixed(candidate.rsi14, 0) : "—"}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  ×{fixed(candidate.volume_ratio, 1)}
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {fixed(candidate.dist_52w_pct, 1)}%

                </td>
                <td className="py-2 text-muted-foreground">
                  {candidate.reasons.length > 0
                    ? candidate.reasons.join(" · ")
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
