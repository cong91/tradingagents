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
  FuturesPosition,
  PortfolioPayload,
  PortfolioPosition,
} from "./contract";
import type { PanelState } from "./use-panel";

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
    data.positions_source !== "unavailable" &&
    data.realized_pnl?.total === 0
  );
}

function PortfolioSkeleton() {
  return (
    <div
      className="flex h-40 items-center justify-center gap-2 rounded-lg border border-dashed text-sm text-muted-foreground"
      role="status"
    >
      <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
      Đang tải danh mục…
    </div>
  );
}

function MissingEndpointState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium">Chưa có dữ liệu danh mục</p>
      <p className="text-sm text-muted-foreground">
        Backend chưa hiện thực GET /api/portfolio (hợp đồng §6). Card này sẽ có
        dữ liệu sau khi server cập nhật.
      </p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        Thử lại
      </Button>
    </div>
  );
}

function PortfolioError({ message, code, onRetry }: { message: string; code: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed px-6 py-8">
      <p className="text-sm font-medium text-destructive">
        Không đọc được danh mục ({code})
      </p>
      <p className="text-sm text-muted-foreground">{message}</p>
      <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
        Thử lại
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

/** Số giờ đã giữ vị thế: "12.3h"; thiếu/không hợp lệ → null. */
function formatHeldHours(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `${value.toFixed(1)}h`;
}

/** Phí khứ hồi ước tính, luôn mang dấu trừ vì đây là chi phí: "-0.27 USDT". */
function formatRoundTripFee(value: number | null | undefined, currency: string | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `-${Math.abs(value).toFixed(2)} ${currency || "đơn vị chưa rõ"}`;
}

/** Funding ròng: "funding +0.54 USDT"; null = chưa có bằng chứng đủ để cộng. */
function formatFunding(value: number | null | undefined, currency: string | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `funding ${value > 0 ? "+" : value < 0 ? "-" : "+"}${Math.abs(value).toFixed(2)} ${currency || "đơn vị chưa rõ"}`;
}

/** Cột "Giữ / phí": thời gian giữ · phí khứ hồi · funding; thiếu hết → "—". */
function heldAndCostText(position: FuturesPosition): string {
  const parts = [
    formatHeldHours(position.held_hours),
    formatRoundTripFee(position.round_trip_fee, position.round_trip_fee_currency ?? null),
    formatFunding(position.funding, position.funding_currency ?? null),
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function PositionRow({ position, currency }: { position: PortfolioPosition; currency: string | null }) {
  return (
    <tr>
      <td className="py-2 pr-3 font-mono font-medium">{position.ticker}</td>
      <td className="py-2 pr-3 tabular-nums">
        {formatAssetQuantity(position.quantity, position.ticker)}
      </td>
      <td className="py-2 pr-3 tabular-nums">
        {position.average_price != null ? (
          formatMoney(position.average_price)
        ) : (
          <span title="Vị thế mở trước khi audit log bắt đầu — giá vốn không xác định">
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
          ? formatSignedCurrency(position.unrealized_pnl)
          : "—"}
      </td>
    </tr>
  );
}

function FuturesSection({
  futures,
  stale,
}: {
  futures: FuturesPortfolio | null | undefined;
  stale: boolean;
}) {
  if (!futures || futures.status === "unavailable") {
    return (
      <section className="space-y-2 border-t pt-4">
        <h3 className="text-sm font-semibold">Vị thế Futures</h3>
        <p className="text-sm text-muted-foreground">
          Chưa đọc snapshot Futures. Snapshot chỉ được đọc khi chế độ giao dịch live đang bật.
        </p>
      </section>
    );
  }

  if (futures.status === "error") {
    return (
      <section className="space-y-2 border-t pt-4">
        <h3 className="text-sm font-semibold">Vị thế Futures</h3>
        <p role="alert" className="text-sm text-destructive">
          Không đọc được tài khoản Futures: {futures.error ?? "lỗi không xác định"}. Dữ liệu này không phải danh mục rỗng.
        </p>
      </section>
    );
  }

  const positions: FuturesPosition[] | null = Array.isArray(futures.positions)
    ? futures.positions
    : null;
  const observedAt = futures.positions_read?.observed_at;
  const hasObservedAt = typeof observedAt === "string" && Number.isFinite(Date.parse(observedAt));
  const completePositionsRead =
    positions !== null &&
    futures.positions_read?.status === "complete" &&
    futures.positions_read.skipped === 0 &&
    Array.isArray(futures.positions_read.issues) &&
    futures.positions_read.issues.length === 0;

  return (
    <section className="space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">
          {futures.account_scope === "unified"
            ? "Tài khoản Unified (có gồm Futures)"
            : futures.account_scope === "trading"
              ? "Tài khoản giao dịch OKX (equity quy đổi USD)"
              : "Tài khoản Futures"}
        </h3>
        {futures.venue_profile ? (
          <Badge variant={futures.venue_profile === "demo" ? "secondary" : "destructive"}>
            {futures.venue_profile === "demo" ? "Demo Trading" : "Production"}
          </Badge>
        ) : null}
      </div>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label={`Số dư ví${futures.wallet_balance_currency ? ` (${futures.wallet_balance_currency})` : ""}`}>
          {formatMoney(futures.wallet_balance, futures.wallet_balance_currency)}
        </Kpi>
        <Kpi label={`Equity${futures.equity_currency ? ` (${futures.equity_currency})` : ""}`}>
          {formatMoney(futures.equity, futures.equity_currency)}
        </Kpi>
        <Kpi label={`Khả dụng${futures.available_balance_currency ? ` (${futures.available_balance_currency})` : ""}`}>
          {formatMoney(futures.available_balance, futures.available_balance_currency)}
        </Kpi>
        <Kpi label={`Lãi/lỗ chưa chốt${futures.unrealized_pnl_currency ? ` (${futures.unrealized_pnl_currency})` : ""}`}>
          {futures.unrealized_pnl != null ? (
            <span className={pnlTextClass(futures.unrealized_pnl)}>
              {formatSignedCurrency(futures.unrealized_pnl, futures.unrealized_pnl_currency ?? "")}
            </span>
          ) : "—"}
        </Kpi>
      </dl>
      {stale ? (
        <p role="alert" className="text-sm text-destructive">
          Dữ liệu Futures đã cũ hoặc chưa xác minh được thời điểm đọc; không thể xác nhận trạng thái Futures hiện tại.
        </p>
      ) : null}
      {!hasObservedAt ? (
        <p role="alert" className="text-sm text-destructive">
          Không xác định được thời điểm đọc vị thế Futures; không thể xác nhận dữ liệu hiện tại.
        </p>
      ) : null}
      {!completePositionsRead ? (
        <p role="alert" className="text-sm text-destructive">
          Danh sách vị thế Futures có thể không đầy đủ; không thể xác nhận toàn bộ vị thế đang mở.
        </p>
      ) : null}
      {positions !== null && positions.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Các vị thế Futures do sàn báo cáo</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-3 font-medium">Hợp đồng</th>
                <th scope="col" className="py-2 pr-3 font-medium">Hướng</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Contracts / qty</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Entry</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Mark</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Notional</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Margin</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Đòn bẩy</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Giữ / phí</th>
                <th scope="col" className="py-2 text-right font-medium">Lãi/lỗ chưa chốt</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {positions.map((position, index) => (
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
                  <td className="py-2 pr-3 text-right tabular-nums">{heldAndCostText(position)}</td>
                  <td className={`py-2 text-right tabular-nums ${position.unrealized_pnl != null ? pnlTextClass(position.unrealized_pnl) : ""}`}>
                    {position.unrealized_pnl != null ? formatSignedCurrency(position.unrealized_pnl, position.unrealized_pnl_currency ?? "") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : completePositionsRead && hasObservedAt && !stale ? (
        <p className="text-sm text-muted-foreground">Sàn không báo vị thế Futures đang mở.</p>
      ) : (
        <p className="text-sm text-muted-foreground">Chưa thể xác nhận có vị thế Futures đang mở hay không.</p>
      )}
      <p className="text-xs text-muted-foreground">
        Notional là giá trị danh nghĩa, margin là tài sản ký quỹ; cả hai không phải tiền lãi hay dòng tiền đã nhận.
      </p>
      <p className="text-xs text-muted-foreground">
        Cột “Giữ / phí” là chi phí ước tính để tham khảo — phí khứ hồi và funding
        không phải số tiền đã bị trừ khỏi số dư trong snapshot này.
      </p>
    </section>
  );
}

function PortfolioBody({ data, stale, futuresStale, staleMessage, staleCode }: {
  data: PortfolioPayload;
  stale: boolean;
  futuresStale: boolean;
  staleMessage: string | null;
  staleCode: string | null;
}) {
  const { realized_pnl: realized, currency } = data;
  const emptySpot = isEmptyPortfolio(data) && !stale;
  const staleEmptySpot = stale && isEmptyPortfolio(data);
  return (
    <div className="space-y-4">
      {staleMessage !== null ? (
        <p role="alert" className="text-sm text-destructive">
          Lần poll gần nhất lỗi ({staleCode}) — đang hiển thị dữ liệu cũ: {staleMessage}
        </p>
      ) : stale ? (
        <p role="alert" className="text-sm text-destructive">
          Snapshot đã cũ; trạng thái Spot và Futures hiện tại chưa được xác nhận.
        </p>
      ) : null}

      {data.halted_today === null ? (
        <p role="alert" className="text-sm text-destructive">
          Không xác định được trạng thái RiskGuard từ audit.
        </p>
      ) : null}
      {data.halted_today ? (
        <p className="flex items-center gap-2 rounded-lg border-l-4 border-red-600 bg-red-50 px-3 py-2 text-sm text-red-900 dark:bg-red-950 dark:text-red-100">
          <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
          RiskGuard đã dừng giao dịch hôm nay.
        </p>
      ) : null}

      {data.positions_source === "unavailable" ? (
        <p role="alert" className="text-sm text-destructive">
          Không xác định được số dư và vị thế Spot.
        </p>
      ) : null}
      {staleEmptySpot ? (
        <p className="rounded-lg border border-dashed px-6 py-8 text-sm text-muted-foreground">
          Snapshot Spot đã cũ; không thể xác nhận trạng thái Spot hiện tại.
        </p>
      ) : emptySpot ? (
        <div className="flex flex-col items-center gap-1.5 rounded-lg border border-dashed px-6 py-8 text-center">
          <InboxIcon className="size-6 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm font-medium">Chưa có dữ liệu Spot</p>
          <p className="max-w-md text-sm text-muted-foreground">
            Chưa có số dư Spot hoặc giao dịch Spot trong audit log. Phần Futures bên dưới được đọc độc lập từ sàn.
          </p>
        </div>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label="Tiền mặt Spot">{formatMoney(data.cash, currency)}</Kpi>
            <Kpi label="Giá trị Spot ước tính">{formatMoney(data.equity_est, currency)}</Kpi>
            <Kpi label="Spot audit đã chốt (tham khảo)">
              {realized ? (
                <span className={pnlTextClass(realized.total)}>
                  {formatSignedCurrency(realized.total)}
                </span>
              ) : "—"}
            </Kpi>
            <Kpi label="Spot audit hôm nay (tham khảo)">
              {realized ? (
                <span className={pnlTextClass(realized.today)}>
                  {formatSignedCurrency(realized.today)}
                </span>
              ) : "—"}
            </Kpi>
          </dl>

          {data.positions.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">Vị thế Spot đang mở theo từng mã</caption>
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th scope="col" className="py-2 pr-3 font-medium">Mã</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Khối lượng</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Giá vốn audit</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Giá thị trường</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Giá trị</th>
                    <th scope="col" className="py-2 font-medium">Lãi/lỗ theo audit</th>
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

      <FuturesSection futures={data.futures} stale={stale || futuresStale} />

      <div className="space-y-1 text-xs text-muted-foreground">
        {!staleEmptySpot && realized ? (
          <>
            <p>
              {realized.closed_trades} lượt đóng được ghi nhận trong audit log
              {realized.unknown_basis_closes > 0
                ? ` · ${realized.unknown_basis_closes} lượt đóng không rõ giá vốn (loại khỏi P&L)`
                : ""}
              {realized.consecutive_losses > 0
                ? ` · ${realized.consecutive_losses} lỗ liên tiếp (cảnh báo RiskGuard)`
                : ""}
              .
            </p>
            <p>
              P&amp;L Spot là phép tính từ audit log, không xác nhận khớp lệnh hoặc
              P&amp;L thực tế của tài khoản; đơn vị tiền tệ chưa được xác minh. Hôm nay
              tính theo ngày UTC và khác con số RiskGuard dùng (cộng thêm lãi/lỗ tạm
              tính của vị thế mở trong ngày). Giá vốn Spot cũng chỉ là tham khảo từ
              audit log, không xác minh theo sàn.
            </p>
          </>
        ) : null}
        {data.warnings.map((warning) => (
          <p key={warning}>Cảnh báo: {warning}</p>
        ))}
      </div>
    </div>
  );
}

export function PortfolioCard({
  state,
  stale,
  futuresStale,
  onRetry,
}: {
  state: PanelState<PortfolioPayload>;
  stale: boolean;
  futuresStale: boolean;
  onRetry: () => void;
}) {
  const isStale = stale || state.status === "stale";
  return (
    <Card>
      <CardHeader>
        <CardTitle>Danh mục</CardTitle>
        <CardDescription>
          Snapshot danh mục khi khả dụng; P&amp;L Spot là phép tính tham khảo từ
          audit log, chưa xác nhận khớp lệnh thực tế.
        </CardDescription>
        <CardAction>
          <div className="flex items-center gap-2">
            {stale ? (
              <Badge variant="outline" className="gap-1">
                <TriangleAlertIcon className="size-3" aria-hidden="true" />
                Dữ liệu cũ
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
              Làm mới
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
            stale={isStale}
            futuresStale={futuresStale}
            staleMessage={state.status === "stale" ? state.message : null}
            staleCode={state.status === "stale" ? state.code : null}
          />
        )}
      </CardContent>
      {state.status !== "loading" && state.status !== "error" ? (
        <CardContent className="pt-0">
          <p className="text-xs text-muted-foreground">
            Cập nhật {formatDateTime(state.data.generated_at)} · thời điểm tạo phản
            hồi HTTP, không phải thời điểm dữ liệu tại sàn; tự làm mới mỗi 30 giây.
          </p>
        </CardContent>
      ) : null}
    </Card>
  );
}
