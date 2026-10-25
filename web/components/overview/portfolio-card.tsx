"use client";

// Card "Danh mục" của màn M1: GET /api/portfolio (hợp đồng §6) — positions
// từ sync_portfolio + realized P&L bằng replay FIFO trên audit log.
// Card này KHÔNG được làm sập dashboard (yêu cầu tách rời): 404 (backend
// chưa hiện thực §6) → trạng thái trung tính "chưa có dữ liệu", 503 venue/
// audit log → lỗi đỏ có retry, danh sách rỗng → empty-state riêng (§0.2) —
// mỗi trạng thái một nhánh, không bao giờ throw ra ngoài card.

import {
  InboxIcon,
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
  formatMoney,
  formatQuantityValue,
  formatSignedCurrency,
  pnlTextClass,
} from "@/lib/format";

import type {
  FuturesPortfolio,
  PortfolioPayload,
  PortfolioPosition,
} from "./contract";
import type { PanelState } from "./use-panel";
import { useT } from "@/lib/i18n";

/** Backend chưa có GET /api/portfolio (đang hiện thực song song theo §6) —
 * trạng thái trung tính, không phải lỗi đỏ. */
function isMissingEndpoint(code: string): boolean {
  return code === "http_404" || code === "not_found";
}

/** Empty theo hợp đồng §6: cash null, không position, realized = 0. */
function isEmptyPortfolio(data: PortfolioPayload): boolean {
  return (
    data.cash === null &&
    data.positions.length === 0 &&
    data.realized_pnl.total === 0
  );
}

function PortfolioSkeleton() {
  const t = useT();
  return (
    <div
      className="flex h-40 items-center justify-center gap-2 rounded-lg border border-dashed text-sm text-muted-foreground"
      role="status"
    >
      <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
      {t.overview.portfolio.loading}
    </div>
  );
}

function MissingEndpointState({ onRetry }: { onRetry: () => void }) {
  const t = useT();
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium">{t.overview.portfolio.missingTitle}</p>
      <p className="text-sm text-muted-foreground">{t.overview.portfolio.missingBody}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

function PortfolioError({ message, code, onRetry }: { message: string; code: string; onRetry: () => void }) {
  const t = useT();
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium text-destructive">
        {t.overview.portfolio.cannotRead(code)}
      </p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

function Kpi({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="truncate text-base font-semibold tabular-nums">{children}</dd>
    </div>
  );
}

function PositionRow({ position, currency }: { position: PortfolioPosition; currency: string }) {
  const t = useT();
  return (
    <tr>
      <td className="py-2 pr-3 font-mono font-medium">{position.ticker}</td>
      <td className="py-2 pr-3 tabular-nums">
        {formatAssetQuantity(position.quantity, position.ticker)}
      </td>
      <td className="py-2 pr-3 tabular-nums">
        {position.average_price != null ? (
          formatMoney(position.average_price, currency)
        ) : (
          <span title={t.overview.portfolio.unknownBasis}>
            —
          </span>
        )}
      </td>
      <td className="py-2 pr-3 tabular-nums">
        {formatMoney(position.marked_price, currency)}
      </td>
      <td className="py-2 pr-3 tabular-nums">
        {formatMoney(position.market_value, currency)}
      </td>
      <td className={`py-2 tabular-nums ${position.unrealized_pnl != null ? pnlTextClass(position.unrealized_pnl) : ""}`}>
        {position.unrealized_pnl != null
          ? formatSignedCurrency(position.unrealized_pnl, currency)
          : "—"}
      </td>
    </tr>
  );
}

function FuturesSection({ futures }: { futures: FuturesPortfolio | null | undefined }) {
  const t = useT();
  const c = t.overview.portfolio;
  if (!futures || futures.status === "unavailable") {
    return (
      <section className="space-y-2 border-t pt-4">
        <h3 className="text-sm font-semibold">{c.futuresPositions}</h3>
        <p className="text-sm text-muted-foreground">{c.futuresUnavailable}</p>
      </section>
    );
  }

  if (futures.status === "error") {
    return (
      <section className="space-y-2 border-t pt-4">
        <h3 className="text-sm font-semibold">{c.futuresPositions}</h3>
        <p role="alert" className="text-sm text-destructive">
          {c.futuresError(futures.error ?? c.unknownErrorLower)}
        </p>
      </section>
    );
  }

  return (
    <section className="space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">
          {futures.account_scope === "unified"
            ? c.accountUnified
            : futures.account_scope === "trading"
              ? c.accountTrading
              : c.accountFutures}
        </h3>
        {futures.venue_profile ? (
          <Badge variant={futures.venue_profile === "demo" ? "secondary" : "destructive"}>
            {futures.venue_profile === "demo" ? "Demo Trading" : "Production"}
          </Badge>
        ) : null}
      </div>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label={`${c.walletBalance}${futures.wallet_balance_currency ? ` (${futures.wallet_balance_currency})` : ""}`}>
          {formatMoney(futures.wallet_balance, futures.wallet_balance_currency)}
        </Kpi>
        <Kpi label={`${c.equity}${futures.equity_currency ? ` (${futures.equity_currency})` : ""}`}>
          {formatMoney(futures.equity, futures.equity_currency)}
        </Kpi>
        <Kpi label={`${c.available}${futures.available_balance_currency ? ` (${futures.available_balance_currency})` : ""}`}>
          {formatMoney(futures.available_balance, futures.available_balance_currency)}
        </Kpi>
        <Kpi label={`${c.unrealizedPnl}${futures.unrealized_pnl_currency ? ` (${futures.unrealized_pnl_currency})` : ""}`}>
          {futures.unrealized_pnl != null ? (
            <span className={pnlTextClass(futures.unrealized_pnl)}>
              {formatSignedCurrency(futures.unrealized_pnl, futures.unrealized_pnl_currency ?? "")}
            </span>
          ) : "—"}
        </Kpi>
      </dl>
      {futures.positions.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{c.futuresCaption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">{c.contract}</th>
                <th scope="col" className="py-2 pr-3 font-medium">{c.side}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.contractsQty}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.entry}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.mark}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.notional}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.margin}</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">{c.leverage}</th>
                <th scope="col" className="py-2 text-right font-medium">{c.unrealizedPnl}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {futures.positions.map((position, index) => (
                <tr key={`${position.symbol ?? "unknown"}-${position.side ?? "unknown"}-${index}`}>
                  <td className="py-2 pr-3 font-mono font-medium">{position.symbol ?? "—"}</td>
                  <td className="py-2 pr-3">{position.side === "short" ? "Short" : position.side === "long" ? "Long" : "—"}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {position.contracts != null ? formatQuantityValue(position.contracts) : "—"}
                    {position.quantity != null ? ` / ${formatQuantityValue(position.quantity)}` : ""}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(position.entry_price, position.price_currency)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(position.mark_price, position.price_currency)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(position.notional, position.notional_currency)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{formatMoney(position.margin, position.margin_currency)}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{position.leverage != null ? `${formatQuantityValue(position.leverage)}x` : "—"}</td>
                  <td className={`py-2 text-right tabular-nums ${position.unrealized_pnl != null ? pnlTextClass(position.unrealized_pnl) : ""}`}>
                    {position.unrealized_pnl != null ? formatSignedCurrency(position.unrealized_pnl, position.unrealized_pnl_currency ?? "") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{c.noFuturesPositions}</p>
      )}
      <p className="text-xs text-muted-foreground">{c.notionalNote}</p>
    </section>
  );
}

function PortfolioBody({ data, staleMessage, staleCode }: {
  data: PortfolioPayload;
  staleMessage: string | null;
  staleCode: string | null;
}) {
  const t = useT();
  const c = t.overview.portfolio;
  const { realized_pnl: realized, currency } = data;
  const emptySpot = isEmptyPortfolio(data);
  return (
    <div className="space-y-4">
      {staleMessage !== null ? (
        <p role="alert" className="text-sm text-destructive">
          {t.overview.staleNote(staleCode ?? "", staleMessage)}
        </p>
      ) : null}

      {data.halted_today ? (
        <p className="flex items-center gap-2 rounded-lg border-l-4 border-red-600 bg-red-50 px-3 py-2 text-sm text-red-900 dark:bg-red-950 dark:text-red-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          {t.overview.haltedToday}
        </p>
      ) : null}

      {emptySpot ? (
        <div className="flex flex-col items-center gap-1.5 rounded-lg border border-dashed px-6 py-8 text-center">
          <InboxIcon className="size-6 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm font-medium">{c.noSpotTitle}</p>
          <p className="max-w-md text-sm text-muted-foreground">{c.noSpotBody}</p>
        </div>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label={c.spotCash}>{formatMoney(data.cash, currency)}</Kpi>
            <Kpi label={c.spotEquity}>{formatMoney(data.equity_est, currency)}</Kpi>
            <Kpi label={c.spotRealizedTotal}>
              <span className={pnlTextClass(realized.total)}>
                {formatSignedCurrency(realized.total, currency)}
              </span>
            </Kpi>
            <Kpi label={c.spotRealizedToday}>
              <span className={pnlTextClass(realized.today)}>
                {formatSignedCurrency(realized.today, currency)}
              </span>
            </Kpi>
          </dl>

          {data.positions.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">{c.spotCaption}</caption>
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th scope="col" className="py-2 pr-3 font-medium">{c.ticker}</th>
                    <th scope="col" className="py-2 pr-3 font-medium">{c.quantity}</th>
                    <th scope="col" className="py-2 pr-3 font-medium">{c.costBasis}</th>
                    <th scope="col" className="py-2 pr-3 font-medium">{c.marketPrice}</th>
                    <th scope="col" className="py-2 pr-3 font-medium">{c.value}</th>
                    <th scope="col" className="py-2 font-medium">{c.unrealizedEst}</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {data.positions.map((position) => (
                    <PositionRow key={position.ticker} position={position} currency={currency} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      )}

      <FuturesSection futures={data.futures} />

      <div className="space-y-1 text-xs text-muted-foreground">
        <p>
          {c.closedTrades(realized.closed_trades)}
          {realized.unknown_basis_closes > 0
            ? c.unknownBasisCloses(realized.unknown_basis_closes)
            : ""}
          {realized.consecutive_losses > 0
            ? c.consecutiveLosses(realized.consecutive_losses)
            : ""}
          .
        </p>
        <p>{c.todayNote}</p>
        {data.warnings.map((warning) => (
          <p key={warning}>{t.common.warning(warning)}</p>
        ))}
      </div>
    </div>
  );
}

export function PortfolioCard({
  state,
  stale,
  onRetry,
}: {
  state: PanelState<PortfolioPayload>;
  stale: boolean;
  onRetry: () => void;
}) {
  const t = useT();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.overview.portfolio.title}</CardTitle>
        <CardDescription>{t.overview.portfolio.description}</CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            {stale ? (
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
          <PortfolioSkeleton />
        ) : state.status === "error" ? (
          isMissingEndpoint(state.code) ? (
            <MissingEndpointState onRetry={onRetry} />
          ) : (
            <PortfolioError
              message={state.message}
              code={state.code}
              onRetry={onRetry}
            />
          )
        ) : (
          <PortfolioBody
            data={state.data}
            staleMessage={state.status === "stale" ? state.message : null}
            staleCode={state.status === "stale" ? state.code : null}
          />
        )}
      </CardContent>
      {state.status !== "loading" && state.status !== "error" ? (
        <CardContent className="pt-0">
          <p className="text-xs text-muted-foreground">
            {t.overview.portfolio.footer(formatDateTime(state.data.generated_at))}

          </p>
        </CardContent>
      ) : null}
    </Card>
  );
}
