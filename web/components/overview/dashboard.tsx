"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  LoaderCircleIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  fetchAudit,
  fetchHealth,
  fetchHistory,
  fetchPortfolio,
  type AuditPayload,
  type HistoryPayload,
} from "./contract";
import { summarizeHistory } from "./summary";
import { usePoll, type ReadyPanel } from "./use-panel";
import { KpiCards } from "./kpi-cards";
import { AlphaChart } from "./alpha-chart";
import { OrderTable } from "./order-table";
import { EmptyState } from "./empty-state";
import { PortfolioCard } from "./portfolio-card";

// Chu kỳ poll theo stale_after của từng endpoint (hợp đồng §0.2/§6/§7/§8/§9):
// audit 10s, health 15s, history 60s, portfolio 30s (stale_after §6).
const HEALTH_INTERVAL_MS = 15_000;
const AUDIT_INTERVAL_MS = 10_000;
const HISTORY_INTERVAL_MS = 60_000;
const PORTFOLIO_INTERVAL_MS = 30_000;
const AUDIT_PAGE_SIZE = 200;
const HISTORY_LIMIT = 200;

const loadHealth = () => fetchHealth();
const loadHistory = () => fetchHistory(HISTORY_LIMIT);
const loadAudit = () => fetchAudit(AUDIT_PAGE_SIZE);
const loadPortfolio = () => fetchPortfolio();

function PanelSkeleton() {
  return (
    <div
      className="flex h-40 items-center justify-center gap-2 rounded-lg border border-dashed text-sm text-muted-foreground"
      role="status"
    >
      <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
      Đang tải dữ liệu…
    </div>
  );
}

function PanelError({
  message,
  code,
  onRetry,
}: {
  message: string;
  code: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium text-destructive">
        Không đọc được dữ liệu ({code})
      </p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        Thử lại
      </Button>
    </div>
  );
}

function StaleErrorNote({ message, code }: { message: string; code: string }) {
  return (
    <p role="alert" className="text-sm text-destructive">
      Lần poll gần nhất lỗi ({code}) — đang hiển thị dữ liệu cũ: {message}
    </p>
  );
}

function StaleBadge() {
  return (
    <Badge variant="outline" className="gap-1">
      <TriangleAlertIcon className="size-3" aria-hidden="true" />
      Dữ liệu cũ
    </Badge>
  );
}

function HistorySection({ state }: { state: ReadyPanel<HistoryPayload> }) {
  // useMemo giữ identity của series ổn định giữa các render — biểu đồ không bị dựng lại chỉ vì đồng hồ stale chạy.
  const summary = useMemo(
    () => summarizeHistory(state.data.items),
    [state.data]
  );

  if (state.data.items.length === 0) {
    return (
      <EmptyState
        title="Chưa có quyết định nào trong nhật ký"
        hint="Lịch sử quyết định được ghi lại khi một phiên phân tích chạy xong. Hãy chạy pipeline để bắt đầu."
        action={{ label: "Chạy phân tích ở Agent Pipeline", href: "/pipeline" }}
      />
    );
  }

  return (
    <div className="space-y-4">
      {state.status === "stale" ? (
        <StaleErrorNote message={state.message} code={state.code} />
      ) : null}
      {state.data.warning ? (
        <p className="text-xs text-muted-foreground">Cảnh báo: {state.data.warning}</p>
      ) : null}
      <KpiCards summary={summary} />
      {summary.series.length > 0 ? (
        <AlphaChart points={summary.series} />
      ) : (
        <EmptyState
          title="Chưa có quyết định nào đã chốt để vẽ biểu đồ"
          hint="Một quyết định chỉ có alpha sau thời gian nắm giữ. Các quyết định đang chờ sẽ về đây khi được chốt."
          action={{ label: "Xem quyết định đang chờ ở Lịch sử & Audit", href: "/audit" }}
        />
      )}
      <p className="text-xs text-muted-foreground">
        Số liệu tính trên {state.data.items.length}
        {state.data.total > state.data.items.length
          ? ` quyết định gần nhất (tổng ${state.data.total})`
          : " quyết định"}
        .
      </p>
    </div>
  );
}

function AuditSection({ state }: { state: ReadyPanel<AuditPayload> }) {
  return (
    <div className="space-y-3">
      {state.status === "stale" ? (
        <StaleErrorNote message={state.message} code={state.code} />
      ) : null}
      <OrderTable items={state.data.items} />
      {state.data.skipped_lines > 0 ? (
        <p className="text-xs text-muted-foreground">
          Cảnh báo: {state.data.skipped_lines} dòng log hỏng đã bị bỏ qua.
        </p>
      ) : null}
    </div>
  );
}

export function OverviewDashboard() {
  // Đồng hồ 5s chỉ để tính lại badge "cũ" giữa hai lần poll.
  const [clock, setClock] = useState<number>(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 5_000);
    return () => clearInterval(id);
  }, []);

  const health = usePoll(loadHealth, HEALTH_INTERVAL_MS, HEALTH_INTERVAL_MS, clock);
  const history = usePoll(loadHistory, HISTORY_INTERVAL_MS, HISTORY_INTERVAL_MS, clock);
  const audit = usePoll(loadAudit, AUDIT_INTERVAL_MS, AUDIT_INTERVAL_MS, clock);
  const portfolio = usePoll(loadPortfolio, PORTFOLIO_INTERVAL_MS, PORTFOLIO_INTERVAL_MS, clock);
  // refresh của từng hook là callback ổn định — destructure để deps của refreshAll ổn định.
  const { refresh: refreshHealth } = health;
  const { refresh: refreshHistory } = history;
  const { refresh: refreshAudit } = audit;
  const { refresh: refreshPortfolio } = portfolio;

  const refreshAll = useCallback(() => {
    refreshHealth();
    refreshHistory();
    refreshAudit();
    refreshPortfolio();
  }, [refreshHealth, refreshHistory, refreshAudit, refreshPortfolio]);

  const allLoading =
    health.state.status === "loading" &&
    history.state.status === "loading" &&
    audit.state.status === "loading" &&
    portfolio.state.status === "loading";

  const latestFetchedAt = [health.state, history.state, audit.state, portfolio.state]
    .map((s) => (s.status === "ready" || s.status === "stale" ? s.fetchedAt : null))
    .filter((t): t is number => t !== null)
    .reduce<number | null>((max, t) => (max === null || t > max ? t : max), null);

  const anyStale = health.stale || history.stale || audit.stale || portfolio.stale;

  const halted = health.state.status !== "loading" && health.state.status !== "error"
    ? health.state.data.halted_today === true
    : false;
  const haltReason =
    audit.state.status === "ready" || audit.state.status === "stale"
      ? audit.state.data.halt_reason
      : null;
  const pendingApprovals =
    health.state.status === "ready" || health.state.status === "stale"
      ? health.state.data.pending_approvals
      : 0;

  return (
    <div>
      {/* Hành động chính của màn: làm mới toàn bộ dữ liệu (≥44px). */}
      <div className="mb-4 flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {latestFetchedAt === null
            ? "Chưa có dữ liệu"
            : `Cập nhật lúc ${new Date(latestFetchedAt).toLocaleTimeString("vi-VN")}`}
        </p>
        <div className="flex items-center gap-2">
          {anyStale ? <StaleBadge /> : null}
          <Button className="min-touch" onClick={refreshAll}>
            <RefreshCwIcon aria-hidden="true" />
            Làm mới
          </Button>
        </div>
      </div>

      {halted ? (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-red-600 bg-red-50 px-4 py-3 text-sm text-red-900 dark:bg-red-950 dark:text-red-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">RiskGuard đã dừng giao dịch hôm nay.</span>
          {haltReason ? <span className="truncate">{haltReason}</span> : null}
          <a href="/audit" className="ml-auto inline-flex min-touch items-center underline">
            Xem audit
          </a>
        </div>
      ) : null}

      {pendingApprovals > 0 ? (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-amber-500 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">{pendingApprovals} lệnh đang chờ duyệt.</span>
          <a href="/approvals" className="ml-auto inline-flex min-touch items-center underline">
            Mở màn Duyệt lệnh
          </a>
        </div>
      ) : null}

      {allLoading ? (
        <Card>
          <CardContent>
            <PanelSkeleton />
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Danh mục tự xử lý mọi trạng thái (kể cả 404 khi backend chưa có
              §6) — lỗi của card này không được chạm tới các panel còn lại. */}
          <div className="mb-4">
            <PortfolioCard
              state={portfolio.state}
              stale={portfolio.stale}
              onRetry={portfolio.refresh}
            />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Hiệu suất quyết định</CardTitle>
              <CardDescription>
                Alpha của các quyết định trong decision log, đơn vị điểm % — lãi luôn kèm
                dấu +, lỗ luôn kèm dấu −.
              </CardDescription>
              {history.stale ? <StaleBadge /> : null}
            </CardHeader>
            <CardContent>
              {history.state.status === "loading" ? (
                <PanelSkeleton />
              ) : history.state.status === "error" ? (
                <PanelError
                  message={history.state.message}
                  code={history.state.code}
                  onRetry={history.refresh}
                />
              ) : (
                <HistorySection state={history.state} />
              )}
            </CardContent>
          </Card>

          <Card className="mt-4">
            <CardHeader>
              <CardTitle>Lệnh thực thi gần đây</CardTitle>
              <CardDescription>
                Từ nhật ký audit của engine — mua là tiền ra (−), bán là tiền vào (+).
              </CardDescription>
              {audit.stale ? <StaleBadge /> : null}
            </CardHeader>
            <CardContent>
              {audit.state.status === "loading" ? (
                <PanelSkeleton />
              ) : audit.state.status === "error" ? (
                <PanelError
                  message={audit.state.message}
                  code={audit.state.code}
                  onRetry={audit.refresh}
                />
              ) : (
                <AuditSection state={audit.state} />
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
