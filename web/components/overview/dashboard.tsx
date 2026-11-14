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
  fetchPerformanceHistory,
  type AuditPayload,
  type HistoryPayload,
  type PortfolioPayload,
} from "./contract";
import { summarizeHistory } from "./summary";
import { usePoll, type PanelState, type ReadyPanel } from "./use-panel";
import { KpiCards } from "./kpi-cards";
import { AlphaChart } from "./alpha-chart";
import { OrderTable } from "./order-table";
import { EmptyState } from "./empty-state";
import { PortfolioCard } from "./portfolio-card";
import { PerformanceCard } from "./performance-card";
import { OverviewActivityCard } from "./activity-card";
import { BriefCard } from "./brief/brief-card";
import { useBrief } from "./brief/use-brief";
import { useScope } from "./brief/scope-filter";
import { useT } from "@/lib/i18n";
import { formatDateWith } from "@/lib/format";

// Chu kỳ poll theo stale_after của từng endpoint (hợp đồng §0.2/§6/§7/§8/§9):
// brief 30s (kèm luôn lần đọc danh mục), audit 10s, health 15s, history 60s;
// venue fill history 5 minutes to limit exchange requests.
//
// The account is read ONCE, by the brief: /api/brief carries the portfolio body
// verbatim, so this screen does not poll /api/portfolio as well. One read
// replaces one read instead of adding a second venue call.
const HEALTH_INTERVAL_MS = 15_000;
const AUDIT_INTERVAL_MS = 10_000;
const HISTORY_INTERVAL_MS = 60_000;
const PERFORMANCE_INTERVAL_MS = 30_000;
const PERFORMANCE_HISTORY_INTERVAL_MS = 300_000;
const AUDIT_PAGE_SIZE = 200;
const HISTORY_LIMIT = 200;
// UX plan §⑤: the dashboard shows a short digest — at most 10 rows — and links
// out to the full history. The page still fetches AUDIT_PAGE_SIZE so
// total_matching reflects the real backlog, not just what is rendered.
const AUDIT_VISIBLE_ROWS = 10;

const loadHealth = () => fetchHealth();
const loadHistory = () => fetchHistory(HISTORY_LIMIT);
const loadAudit = () => fetchAudit(AUDIT_PAGE_SIZE);
const loadPerformance = () => fetchPerformance();
const loadPerformanceHistory = () => fetchPerformanceHistory();
export const portfolioTimestamp = (data: PortfolioPayload): number | null => {
  if (data.snapshot?.cache === "stale" || data.snapshot?.dirty || data.snapshot?.pending) return null;
  const created = Date.parse(data.generated_at);
  return Number.isFinite(created) ? created : null;
};

export function isFuturesStale(
  state: ReadyPanel<PortfolioPayload>,
  portfolioStale: boolean,
  clock: number
): boolean {
  if (portfolioStale || state.status === "stale") return true;
  if (state.data.futures?.status !== "ok") return true;
  const observed = state.data.futures.positions_read?.observed_at;
  const timestamp = observed ? Date.parse(observed) : NaN;
  const age = clock - timestamp;
  return !Number.isFinite(timestamp) || age < -5_000 || age > 30_000;
}
const generatedTimestamp = (data: { generated_at: string }): string => data.generated_at;

const PORTFOLIO_UNAVAILABLE_CODE = "portfolio_unavailable";

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
        action={{ label: t.overview.noDecisionsAction, href: "/cycles" }}
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
  // UX plan §⑤: cap the visible rows at 10 and always offer the full-history
  // link. This is a digest, not the audit screen — a 200-row table on the
  // overview pushes the operational cards off the first screen.
  const visible = state.data.items.slice(0, AUDIT_VISIBLE_ROWS);
  return (
    <div className="space-y-3">
      {state.status === "stale" ? (
        <StaleErrorNote message={state.message} code={state.code} />
      ) : null}
      <OrderTable items={visible} />
      <a
        href="/audit"
        className="inline-flex min-touch items-center underline text-sm"
      >
        {t.overview.viewAllOrders(visible.length, state.data.total_matching)}
      </a>
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

  const [scope, setScope] = useScope();
  const health = usePoll(loadHealth, HEALTH_INTERVAL_MS, HEALTH_INTERVAL_MS, clock);
  const history = usePoll(loadHistory, HISTORY_INTERVAL_MS, HISTORY_INTERVAL_MS, clock);
  const audit = usePoll(loadAudit, AUDIT_INTERVAL_MS, AUDIT_INTERVAL_MS, clock);
  const brief = useBrief(clock);
  const performance = usePoll(
    loadPerformance,
    PERFORMANCE_INTERVAL_MS,
    PERFORMANCE_INTERVAL_MS,
    clock,
    false,
    generatedTimestamp
  );
  const performanceHistory = usePoll(
    loadPerformanceHistory,
    PERFORMANCE_HISTORY_INTERVAL_MS,
    PERFORMANCE_HISTORY_INTERVAL_MS,
    clock,
    true,
    generatedTimestamp
  );
  // The cards below read the account from the brief's verbatim portfolio body:
  // one poll for the whole screen, and the card contracts stay unchanged.
  //
  // A brief FAILURE must not become a page failure: while the brief has no data
  // yet the portfolio is simply "no data yet", and a brief error reports
  // against the portfolio card only — health, history, audit and performance
  // keep rendering on their own polls (blocking finding B5).
  const portfolioState: PanelState<PortfolioPayload> = useMemo(() => {
    if (brief.state.status === "loading") return { status: "loading" };
    if (brief.state.status === "error") {
      return { status: "error", message: brief.state.message, code: brief.state.code };
    }
    const data = brief.state.data.portfolio;
    if (data === null) {
      return {
        status: "error",
        message:
          brief.state.data.risk.futures_error ??
          t.overview.portfolio.unknownErrorLower,
        code: PORTFOLIO_UNAVAILABLE_CODE,
      };
    }
    if (brief.state.status === "stale") {
      return {
        status: "stale",
        data,
        fetchedAt: brief.state.fetchedAt,
        message: brief.state.message,
        code: brief.state.code,
      };
    }
    return { status: "ready", data, fetchedAt: brief.state.fetchedAt };
  }, [brief.state, t]);

  const portfolioStale = useMemo(() => {
    if (portfolioState.status !== "ready" && portfolioState.status !== "stale") return false;
    return (
      portfolioState.status === "stale" ||
      clock - (portfolioTimestamp(portfolioState.data) ?? 0) > 30_000
    );
  }, [portfolioState, clock]);
  const futuresStale =
    portfolioState.status === "ready" || portfolioState.status === "stale"
      ? isFuturesStale(portfolioState, portfolioStale, clock)
      : true;
  // refresh của từng hook là callback ổn định — destructure để deps của refreshAll ổn định.
  const { refresh: refreshHealth } = health;
  const { refresh: refreshHistory } = history;
  const { refresh: refreshAudit } = audit;
  const { refresh: refreshBrief } = brief;
  const { refresh: refreshPerformance } = performance;
  const { refresh: refreshPerformanceHistory } = performanceHistory;
  const snapshotContext =
    portfolioState.status === "ready" || portfolioState.status === "stale"
      ? portfolioState.data.snapshot?.context_id
      : undefined;
  const snapshotEpoch =
    portfolioState.status === "ready" || portfolioState.status === "stale"
      ? portfolioState.data.snapshot?.epoch
      : undefined;

  useEffect(() => {
    if (!snapshotContext || !snapshotEpoch || typeof EventSource === "undefined") return;
    const stream = new EventSource("/api/snapshot/events");
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onHint = (event: MessageEvent) => {
      try {
        const hint = JSON.parse(event.data) as { context_id?: string | null; epoch?: string };
        if (hint.context_id && hint.context_id !== snapshotContext && hint.epoch === snapshotEpoch) return;
        if (timer) clearTimeout(timer);
        timer = setTimeout(refreshBrief, 1_000);
      } catch {
        // Polling remains the fallback for malformed hints.
      }
    };
    stream.addEventListener("dirty", onHint);
    stream.addEventListener("reset", onHint);
    return () => {
      if (timer) clearTimeout(timer);
      stream.removeEventListener("dirty", onHint);
      stream.removeEventListener("reset", onHint);
      stream.close();
    };
  }, [refreshBrief, snapshotContext, snapshotEpoch]);

  const refreshAll = useCallback(() => {
    refreshHealth();
    refreshHistory();
    refreshAudit();
    refreshBrief();
    refreshPerformance();
    refreshPerformanceHistory();
  }, [refreshHealth, refreshHistory, refreshAudit, refreshBrief, refreshPerformance, refreshPerformanceHistory]);

  // Only the panels that own their own data gate the page skeleton. The brief
  // is deliberately NOT a term here: waiting on it blanked every independent
  // panel (health/history/audit) behind one skeleton (blocking finding B5).
  const allLoading =
    health.state.status === "loading" &&
    history.state.status === "loading" &&
    audit.state.status === "loading" &&
    performance.state.status === "loading";

  const latestFetchedAt = [health.state, history.state, audit.state, brief.state, performance.state]
    .map((s) => (s.status === "ready" || s.status === "stale" ? s.fetchedAt : null))
    .filter((t): t is number => t !== null)
    .reduce<number | null>((max, t) => (max === null || t > max ? t : max), null);

  const anyStale =
    health.stale || history.stale || audit.stale || brief.stale || futuresStale ||
    performance.stale || performanceHistory.stale;

  // Halt and pending-approval warnings are owned by the brief's "Cần xử lý"
  // block, deduplicated and prioritised. That only works while the brief has
  // data: if the brief itself failed, its attention list is gone, so these two
  // warnings are restated here from the independent health poll. Measured
  // before this fallback: a brief 500 removed the halt and both pending
  // approvals from the screen entirely (blocking finding B4).
  const briefHasData =
    brief.state.status === "ready" || brief.state.status === "stale";
  // Read the health values through the narrowed state: a boolean flag does not
  // narrow the union for TypeScript.
  const healthData =
    health.state.status === "ready" || health.state.status === "stale"
      ? health.state.data
      : null;
  // Both fallback banners exist ONLY for the window where the brief has no data
  // to show them in its attention list. Gating on `halted_today` alone printed
  // the halt three times while the brief was healthy — Status row, attention
  // item, and this banner — which is exactly the repetition the dedup exists to
  // prevent. "stale" counts as having data: the brief keeps rendering its list.
  const showFallbackHalt = !briefHasData && healthData?.halted_today === true;
  const fallbackPending = !briefHasData ? healthData?.pending_approvals ?? 0 : 0;
  const haltReason =
    audit.state.status === "ready" || audit.state.status === "stale"
      ? audit.state.data.halt_reason
      : null;

  return (
    <div>
      {/* Hành động chính của màn: làm mới toàn bộ dữ liệu (≥44px). Trên mobile
          dòng này wrap thay vì ép hai cụm vào một hàng hẹp. */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {latestFetchedAt === null
            ? t.overview.noDataYet
            : t.overview.updatedAt(
                formatDateWith(latestFetchedAt, { hour: "numeric", minute: "2-digit", second: "2-digit" })
              )}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {anyStale ? <StaleBadge /> : null}
          <Button className="min-touch" onClick={refreshAll}>
            <RefreshCwIcon aria-hidden="true" />
            {t.common.refresh}
          </Button>
        </div>
      </div>

      {/* Bản tin vận hành đứng đầu trang: nó trả lời câu hỏi đầu tiên của
          người vận hành (cần xử lý gì), và đã bao gồm cảnh báo halt/chờ duyệt
          trong mục "Cần xử lý" — nên không còn banner riêng lặp lại bên dưới,
          trừ khi bản tin không có dữ liệu (khi đó hai banner dự phòng dưới đây
          giữ cảnh báo không bị mất). */}
      <div className="mb-4">
        <BriefCard
          state={brief.state}
          scope={scope}
          onScopeChange={setScope}
          onRetry={brief.refresh}
        />
      </div>

      {showFallbackHalt ? (
        <div
          data-fallback-banner="halt"
          className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-red-600 bg-red-50 px-4 py-3 text-sm text-red-900 dark:bg-red-950 dark:text-red-100"
        >
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">{t.overview.haltedToday}</span>
          {haltReason ? <span className="truncate">{haltReason}</span> : null}
          <a href="/audit" className="ml-auto inline-flex min-touch items-center underline">
            {t.overview.viewAudit}
          </a>
        </div>
      ) : null}

      {fallbackPending > 0 ? (
        <div
          data-fallback-banner="approvals"
          className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border-l-4 border-amber-500 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
        >
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          <span className="font-semibold">{t.overview.pendingApprovals(fallbackPending)}</span>
          <a href="/approvals" className="ml-auto inline-flex min-touch items-center underline">
            {t.overview.openApprovals}
          </a>
        </div>
      ) : null}

      <OverviewActivityCard />

      {allLoading ? (
        <Card>
          <CardContent>
            <PanelSkeleton />
          </CardContent>
        </Card>
      ) : (
        <>
          {/* Danh mục tự xử lý mọi trạng thái (kể cả 404 khi backend chưa có
              §6, hoặc lần đọc danh mục trong bản tin thất bại) — lỗi của card
              này không được chạm tới các panel còn lại. Dữ liệu đến từ bản
              tin: cả màn chỉ đọc tài khoản một lần. */}
          <div className="mb-4">
            <PortfolioCard
              state={portfolioState}
              stale={portfolioStale}
              futuresStale={futuresStale}
              onRetry={brief.refresh}
              scope={scope}
            />
          </div>

          <Card>
            <CardHeader>
              <CardTitle as="h2">{t.overview.decisionPerformanceTitle}</CardTitle>
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
              stale={performance.stale}
              onRetry={performance.refresh}
              historyState={performanceHistory.state}
              historyStale={performanceHistory.stale}
              onHistoryRetry={performanceHistory.refresh}
            />
          </div>

          <Card className="mt-4">
            <CardHeader>
              <CardTitle as="h2">{t.overview.recentOrdersTitle}</CardTitle>
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
