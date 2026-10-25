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
  fetchPerformance,
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
import { PerformanceCard } from "./performance-card";
import { OverviewActivityCard } from "./activity-card";
import { useT } from "@/lib/i18n";
import { formatDateWith } from "@/lib/format";

// Chu kỳ poll theo stale_after của từng endpoint (hợp đồng §0.2/§6/§7/§8/§9):
// audit 10s, health 15s, history 60s, portfolio 30s (stale_after §6);
// performance 30s — endpoint chưa trả stale_after, chọn bằng chu kỳ portfolio
// (cùng đọc audit log + mark giá từ sàn).
const HEALTH_INTERVAL_MS = 15_000;
const AUDIT_INTERVAL_MS = 10_000;
const HISTORY_INTERVAL_MS = 60_000;
const PORTFOLIO_INTERVAL_MS = 30_000;
const PERFORMANCE_INTERVAL_MS = 30_000;
const AUDIT_PAGE_SIZE = 200;
const HISTORY_LIMIT = 200;

const loadHealth = () => fetchHealth();
const loadHistory = () => fetchHistory(HISTORY_LIMIT);
const loadAudit = () => fetchAudit(AUDIT_PAGE_SIZE);
const loadPortfolio = () => fetchPortfolio();
const loadPerformance = () => fetchPerformance();

function PanelSkeleton() {
  const t = useT();
  return (
    <div
      className="flex h-40 items-center justify-center gap-2 rounded-lg border border-dashed text-sm text-muted-foreground"
      role="status"
    >
      <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
      {t.common.loadingData}
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
  const t = useT();
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium text-destructive">
        {t.overview.cannotReadData(code)}
      </p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

function StaleErrorNote({ message, code }: { message: string; code: string }) {
  const t = useT();
  return (
    <p role="alert" className="text-sm text-destructive">
      {t.overview.staleNote(code, message)}
    </p>
  );
}

function StaleBadge() {
  const t = useT();
  return (
    <Badge variant="outline" className="gap-1">
      <TriangleAlertIcon className="size-3" aria-hidden="true" />
      {t.common.staleData}
    </Badge>
  );
}

function HistorySection({ state }: { state: ReadyPanel<HistoryPayload> }) {
  const t = useT();
  // useMemo giữ identity của series ổn định giữa các render — biểu đồ không bị dựng lại chỉ vì đồng hồ stale chạy.
  const summary = useMemo(
    () => summarizeHistory(state.data.items),
    [state.data]
  );

  if (state.data.items.length === 0) {
    return (
      <EmptyState
        title={t.overview.noDecisionsTitle}
        hint={t.overview.noDecisionsHint}
        action={{ label: t.overview.noDecisionsAction, href: "/pipeline" }}
      />
    );
  }

  return (
    <div className="space-y-4">
      {state.status === "stale" ? (
        <StaleErrorNote message={state.message} code={state.code} />
      ) : null}
      {state.data.warning ? (
        <p className="text-xs text-muted-foreground">{t.common.warning(state.data.warning)}</p>
      ) : null}
      <KpiCards summary={summary} />
      {summary.series.length > 0 ? (
        <AlphaChart points={summary.series} />
      ) : (
        <EmptyState
          title={t.overview.noResolvedTitle}
          hint={t.overview.noResolvedHint}
          action={{ label: t.overview.noResolvedAction, href: "/audit" }}
        />
      )}
      <p className="text-xs text-muted-foreground">
        {t.overview.computedOn(state.data.items.length, state.data.total)}
      </p>
    </div>
  );
}

function AuditSection({ state }: { state: ReadyPanel<AuditPayload> }) {
  const t = useT();
  return (
    <div className="space-y-3">
      {state.status === "stale" ? (
        <StaleErrorNote message={state.message} code={state.code} />
      ) : null}
      <OrderTable items={state.data.items} />
      {state.data.skipped_lines > 0 ? (
        <p className="text-xs text-muted-foreground">
          {t.overview.skippedLines(state.data.skipped_lines)}
        </p>
      ) : null}
    </div>
  );
}

export function OverviewDashboard() {
  const t = useT();
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
  const performance = usePoll(
    loadPerformance,
    PERFORMANCE_INTERVAL_MS,
    PERFORMANCE_INTERVAL_MS,
    clock
  );
  // refresh của từng hook là callback ổn định — destructure để deps của refreshAll ổn định.
  const { refresh: refreshHealth } = health;
  const { refresh: refreshHistory } = history;
  const { refresh: refreshAudit } = audit;
  const { refresh: refreshPortfolio } = portfolio;
  const { refresh: refreshPerformance } = performance;

  const refreshAll = useCallback(() => {
    refreshHealth();
    refreshHistory();
    refreshAudit();
    refreshPortfolio();
    refreshPerformance();
  }, [refreshHealth, refreshHistory, refreshAudit, refreshPortfolio, refreshPerformance]);

  const allLoading =
    health.state.status === "loading" &&
    history.state.status === "loading" &&
    audit.state.status === "loading" &&
    portfolio.state.status === "loading" &&
    performance.state.status === "loading";

  const latestFetchedAt = [health.state, history.state, audit.state, portfolio.state, performance.state]
    .map((s) => (s.status === "ready" || s.status === "stale" ? s.fetchedAt : null))
    .filter((t): t is number => t !== null)
    .reduce<number | null>((max, t) => (max === null || t > max ? t : max), null);

  const anyStale =
    health.stale || history.stale || audit.stale || portfolio.stale || performance.stale;

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
            ? t.overview.noDataYet
            : t.overview.updatedAt(
                formatDateWith(latestFetchedAt, { hour: "numeric", minute: "2-digit", second: "2-digit" })
              )}
        </p>
        <div className="flex items-center gap-2">
          {anyStale ? <StaleBadge /> : null}
          <Button className="min-touch" onClick={refreshAll}>
            <RefreshCwIcon aria-hidden="true" />
            {t.common.refresh}
          </Button>
        </div>
      </div>

      <OverviewActivityCard />

      {halted ? (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-red-600 bg-red-50 px-4 py-3 text-sm text-red-900 dark:bg-red-950 dark:text-red-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">{t.overview.haltedToday}</span>
          {haltReason ? <span className="truncate">{haltReason}</span> : null}
          <a href="/audit" className="ml-auto inline-flex min-touch items-center underline">
            {t.overview.viewAudit}
          </a>
        </div>
      ) : null}

      {pendingApprovals > 0 ? (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-amber-500 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">{t.overview.pendingApprovals(pendingApprovals)}</span>
          <a href="/approvals" className="ml-auto inline-flex min-touch items-center underline">
            {t.overview.openApprovals}
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
              <CardTitle>{t.overview.decisionPerformanceTitle}</CardTitle>
              <CardDescription>{t.overview.decisionPerformanceDescription}</CardDescription>
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

          <div className="mt-4">
            <PerformanceCard
              state={performance.state}
              onRetry={performance.refresh}
            />
          </div>

          <Card className="mt-4">
            <CardHeader>
              <CardTitle>{t.overview.recentOrdersTitle}</CardTitle>
              <CardDescription>{t.overview.recentOrdersDescription}</CardDescription>

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
