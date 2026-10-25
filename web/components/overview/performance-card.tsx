"use client";

// Card "Hiệu suất giao dịch" của màn M1: GET /api/performance — replay FIFO
// trên nhật ký thực thi (server/performance.py). Card tự xử lý cả 4 trạng
// thái (hợp đồng §0.2) và không bao giờ throw ra ngoài: loading → skeleton,
// error → thông điệp + thử lại (404 do server chưa đăng ký router → trạng
// thái trung tính như PortfolioCard), ready/stale → tile + bảng; empty khi
// audit log chưa từng có fill nào. Badge "cũ" đo theo generated_at của server
// (mẫu WatchlistPanel — stale sau 60 giây).

import { useEffect, useState } from "react";
import {
  LoaderCircleIcon,
  RefreshCwIcon,
  TriangleAlertIcon,
} from "lucide-react";
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
import {
  formatAssetQuantity,
  formatDateTime,
  formatPercent,
  formatQuantityValue,
  formatSignedNumber,
  formatUsdPrice,
  pnlTextClass,
} from "@/lib/format";

import type {
  PerformanceOpenPosition,
  PerformanceOrder,
  PerformancePayload,
} from "./contract";
import { EmptyState } from "./empty-state";
import { formatHistoricalMoney, historicalCurrencies } from "./historical-currency";
import type { PanelState } from "./use-panel";
import { useT, type Messages } from "@/lib/i18n";

const STALE_AFTER_MS = 60_000; // badge "cũ" khi generated_at già hơn 60 giây

/** Backend chưa hiện thực GET /api/performance — trạng thái trung tính, không phải lỗi đỏ. */
function isMissingEndpoint(code: string): boolean {
  return code === "http_404" || code === "not_found";
}

function PerformanceSkeleton() {
  const t = useT();
  return (
    <div role="status" className="space-y-4">
      <span className="sr-only">{t.overview.performance.loading}</span>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4" aria-hidden="true">
        {Array.from({ length: 4 }, (_, index) => (
          <div key={index} className="h-20 animate-pulse rounded-lg bg-muted" />
        ))}
      </div>
      <div className="h-12 animate-pulse rounded-lg bg-muted" aria-hidden="true" />
      <div className="h-12 animate-pulse rounded-lg bg-muted" aria-hidden="true" />
    </div>
  );
}

function MissingEndpointState({ onRetry }: { onRetry: () => void }) {
  const t = useT();
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium">{t.overview.performance.missingTitle}</p>
      <p className="text-sm text-muted-foreground">{t.overview.performance.missingBody}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

function PerformanceError({
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
        {t.overview.performance.cannotRead(code)}
      </p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

/** Nhãn action của dòng execute — buy/sell và cả bracket (bridge.py:507,517,529
 *  log bracket_placed/bracket_failed với phase="execute"). Không thể map mù
 *  "buy → Mua, còn lại → Bán" vì bracket line sẽ thành "Bán" sai sự thật. */
function actionBadge(t: Messages, action: string | null): {
  label: string;
  variant: "outline" | "secondary" | "destructive";
} {
  if (action === "buy") return { label: t.common.buy, variant: "outline" };
  if (action === "sell") return { label: t.common.sell, variant: "outline" };
  if (action === "bracket_placed") return { label: t.overview.performance.bracketPlaced, variant: "secondary" };
  if (action === "bracket_failed") return { label: t.overview.performance.bracketFailed, variant: "destructive" };
  return { label: action ?? "—", variant: "outline" };
}

function modeBadge(mode: string | null): {
  label: string;
  variant: "secondary" | "destructive";
} {
  return mode === "live"
    ? { label: "Live", variant: "destructive" }
    : { label: "Dry", variant: "secondary" };
}

function Tile({
  label,
  children,
  note,
  ariaLabel,
}: {
  label: string;
  children: React.ReactNode;
  note?: string;
  ariaLabel?: string;
}) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums" aria-label={ariaLabel}>
          {children}
        </CardTitle>
        {note ? <CardDescription>{note}</CardDescription> : null}
      </CardHeader>
    </Card>
  );
}

function SummaryTiles({ data }: { data: PerformancePayload }) {
  const t = useT();
  const c = t.overview.performance;
  const { summary } = data;
  // Kỳ vọng/ván: màu xanh khi >0, đỏ khi <0 — dấu +/− trong số là tín hiệu
  // không dựa vào màu (WCAG 1.4.1, quy ước lib/format.ts).
  const expectancyLabel = c.expectancyAria(formatSignedNumber(summary.expectancy_pct, 2));
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Tile label={c.closedTrades} note={c.winsLosses(summary.wins, summary.losses)}>
        {summary.closed_trades}
      </Tile>
      <Tile label={c.winRate} note={c.onClosed(summary.closed_trades)}>
        {formatPercent(summary.win_rate_pct)}
      </Tile>
      <Tile label={c.expectancy} ariaLabel={expectancyLabel}>
        <span className={pnlTextClass(summary.expectancy_pct)}>
          {formatSignedNumber(summary.expectancy_pct, 2)}&nbsp;%
        </span>
      </Tile>
      <Tile label={c.avgWinLoss} note={c.avgWinLossNote}>
        <span className="text-profit">{formatSignedNumber(summary.avg_win_pct, 2)}&nbsp;%</span>
        <span className="text-muted-foreground"> / </span>
        <span className="text-loss">{formatSignedNumber(summary.avg_loss_pct, 2)}&nbsp;%</span>
      </Tile>
    </div>
  );
}

function OpenPositionsTable({ positions }: { positions: PerformanceOpenPosition[] }) {
  const t = useT();
  const c = t.overview.performance;
  if (positions.length === 0) {
    return (
      <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
        {c.noOpenPositions}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">{c.openCaption}</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.market}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.quantity}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.entryPrice}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.currentPrice}</th>
            <th scope="col" className="py-2 text-right font-medium">{c.pnlPct}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {positions.map((position, index) => {
            const isDerivatives = position.market === "derivatives";
            return (
              <tr key={`${position.ticker}-${position.entry_at ?? ""}-${index}`}>
                <td className="py-2 pr-4 font-mono font-medium">{position.ticker}</td>
                <td className="py-2 pr-4">
                  <Badge variant="secondary">{position.market === "derivatives" ? "Futures" : "Spot"}</Badge>
                  {position.leverage != null ? <span className="ml-2 text-xs text-muted-foreground">{formatQuantityValue(position.leverage)}x</span> : null}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {formatAssetQuantity(position.qty, position.ticker)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {isDerivatives
                    ? formatHistoricalMoney(position.entry_price, null)
                    : formatUsdPrice(position.entry_price)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {isDerivatives
                    ? formatHistoricalMoney(position.current_price, null)
                    : formatUsdPrice(position.current_price)}
                </td>
                <td
                  className={`py-2 text-right tabular-nums ${
                    position.unrealized_pct != null ? pnlTextClass(position.unrealized_pct) : ""
                  }`}
                >
                  {position.unrealized_pct != null
                    ? `${formatSignedNumber(position.unrealized_pct, 2)} %`
                    : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RecentOrdersTable({ orders }: { orders: PerformanceOrder[] }) {
  const t = useT();
  const c = t.overview.performance;
  if (orders.length === 0) {
    return (
      <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
        {c.noRecentOrders}
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">{c.recentCaption}</caption>
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 font-medium">{c.time}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.order}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.market}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.quantity}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.price}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.notionalMargin}</th>
            <th scope="col" className="py-2 font-medium">{c.mode}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {orders.map((order, index) => {
            const action = actionBadge(t, order.action);
            const mode = modeBadge(order.mode);
            const isDerivatives = order.market === "derivatives";
            const { quote, settle } = historicalCurrencies(order.ccxt_symbol);
            return (
              <tr key={`${order.timestamp ?? ""}-${order.order_id ?? index}-${index}`}>
                <td className="py-2 pr-4 whitespace-nowrap tabular-nums">
                  {order.timestamp ? formatDateTime(order.timestamp) : "—"}
                </td>
                <td className="py-2 pr-4 font-mono font-medium">{order.ticker ?? "—"}</td>
                <td className="py-2 pr-4">
                  <Badge variant={action.variant}>{action.label}</Badge>
                </td>
                <td className="py-2 pr-4">
                  <Badge variant="secondary">{order.market === "derivatives" ? "Futures" : "Spot"}</Badge>
                  {order.leverage != null ? <span className="ml-2 text-xs text-muted-foreground">{formatQuantityValue(order.leverage)}x</span> : null}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {order.qty != null ? formatQuantityValue(order.qty) : "—"}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {formatHistoricalMoney(order.price_est, quote)}
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">
                  {isDerivatives
                    ? `${order.qty != null && order.price_est != null ? formatHistoricalMoney(Math.abs(order.qty * order.price_est), quote) : "—"} notional${order.cost != null ? ` · margin ${formatHistoricalMoney(order.cost, settle)}` : ""}`
                    : "—"}
                </td>
                <td className="py-2">
                  <Badge variant={mode.variant}>{mode.label}</Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PerformanceBody({ data }: { data: PerformancePayload }) {
  const t = useT();
  const c = t.overview.performance;
  const { summary, open_positions, brackets, recent_orders } = data;

  // Empty theo yêu cầu: closed_trades = 0 → "chưa có giao dịch nào được thực
  // thi". Nhưng khi log ĐÃ có fill (vị thế mở / lệnh gần đây) thì thông báo đó
  // sai sự thật — chỉ coi là empty khi cả ba đều rỗng (cùng cách
  // isEmptyPortfolio của PortfolioCard).
  const hasExecutions =
    summary.closed_trades > 0 ||
    open_positions.length > 0 ||
    recent_orders.length > 0;
  if (!hasExecutions) {
    return (
      <EmptyState
        title={c.emptyTitle}
        hint={c.emptyHint}
      />
    );
  }

  return (
    <div className="space-y-4">
      <SummaryTiles data={data} />

      <section className="space-y-2">
        <h3 className="text-sm font-medium">{c.openPositions}</h3>
        <OpenPositionsTable positions={open_positions} />
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium">{c.recentOrders}</h3>
        <RecentOrdersTable orders={recent_orders} />
      </section>

      <p className="text-xs text-muted-foreground">
        {c.bracketsNote(brackets.placed, brackets.failed)}
      </p>
    </div>
  );
}

export function PerformanceCard({
  state,
  onRetry,
}: {
  state: PanelState<PerformancePayload>;
  onRetry: () => void;
}) {
  const t = useT();
  // Đồng hồ cho badge stale — Date.now() không được gọi trong render (purity),
  // mẫu WatchlistPanel.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const isStale =
    (state.status === "ready" || state.status === "stale") &&
    now - Date.parse(state.data.generated_at) > STALE_AFTER_MS;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.overview.performance.title}</CardTitle>
        <CardDescription>{t.overview.performance.description}</CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            {isStale ? (
              <Badge variant="outline" className="gap-1">
                <TriangleAlertIcon className="size-3" aria-hidden="true" />
                {t.common.staleData}
              </Badge>
            ) : null}
            <Button
              variant="outline"
              size="sm"
              className="min-touch"
              onClick={onRetry}
            >
              {state.status === "loading" ? (
                <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
              ) : (
                <RefreshCwIcon aria-hidden="true" />
              )}
              {t.common.refresh}
            </Button>
          </div>
        </CardAction>
      </CardHeader>
      <CardContent>
        {state.status === "loading" ? (
          <PerformanceSkeleton />
        ) : state.status === "error" ? (
          isMissingEndpoint(state.code) ? (
            <MissingEndpointState onRetry={onRetry} />
          ) : (
            <PerformanceError
              message={state.message}
              code={state.code}
              onRetry={onRetry}
            />
          )
        ) : (
          <>
            {state.status === "stale" ? (
              <p role="alert" className="mb-4 text-sm text-destructive">
                {t.overview.staleNote(state.code, state.message)}
              </p>
            ) : null}
            <PerformanceBody data={state.data} />
          </>
        )}
      </CardContent>
      {state.status === "ready" || state.status === "stale" ? (
        <CardContent className="pt-0">
          <p className="text-xs text-muted-foreground">
            {t.overview.performance.footer(formatDateTime(state.data.generated_at))}

          </p>
        </CardContent>
      ) : null}
    </Card>
  );
}
