import { Badge } from "@/components/ui/badge";
import { formatDateWith, formatQuantityValue } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { EmptyState } from "./empty-state";
import type { AuditItem } from "./contract";
import {
  formatHistoricalMoney,
  formatHistoricalSignedMoney,
  historicalCurrencies,
} from "./historical-currency";

function formatTime(timestamp: string | null): string {
  if (!timestamp) return "—";
  return formatDateWith(timestamp, { dateStyle: "short", timeStyle: "medium" });
}

/** Lệnh thực thi = dòng audit action buy/sell ở phase "execute" (hợp đồng §6). */
function isExecutedOrder(item: AuditItem): boolean {
  return item.phase === "execute" && (item.action === "buy" || item.action === "sell");
}

export function OrderTable({ items }: { items: AuditItem[] }) {
  const t = useT();
  const c = t.overview.orders;
  const orders = items.filter(isExecutedOrder);

  if (orders.length === 0) {
    return (
      <EmptyState
        title={c.emptyTitle}
        hint={c.emptyHint}
        action={{ label: t.overview.openApprovals, href: "/approvals" }}
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 font-medium">{c.time}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.pair}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.order}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.quantity}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.estPrice}</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">{c.flowNotional}</th>
            <th scope="col" className="py-2 pr-4 font-medium">{c.mode}</th>
            <th scope="col" className="py-2 font-medium">{c.orderId}</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order, index) => {
            const isBuy = order.action === "buy";
            const qty = order.qty;
            const price = order.price_est;
            const isDerivatives = order.market === "derivatives";
            const { quote, settle } = historicalCurrencies(order.ccxt_symbol);
            const notional = qty != null && price != null ? Math.abs(qty * price) : null;
            const flow = notional == null ? null : isBuy ? -notional : notional;
            return (
              <tr key={`${order.timestamp ?? ""}-${order.order_id ?? index}`} className="border-b last:border-0">
                <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatTime(order.timestamp)}</td>
                <td className="py-2 pr-4 font-medium">{order.ticker ?? "—"}</td>
                <td className="py-2 pr-4">
                  <div className="flex flex-wrap items-center gap-1">
                    <Badge variant="outline">{isBuy ? t.common.buy : t.common.sell}</Badge>
                    <Badge variant="secondary">{isDerivatives ? "Futures" : "Spot"}</Badge>
                  </div>
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">{qty != null ? formatQuantityValue(qty) : "—"}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{formatHistoricalMoney(price, quote)}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${!isDerivatives && flow != null && flow !== 0 ? flow > 0 ? "text-profit" : "text-loss" : ""}`}>
                  {isDerivatives
                    ? `${formatHistoricalMoney(notional, quote)} notional${order.leverage != null ? ` · ${formatQuantityValue(order.leverage)}x · margin ${formatHistoricalMoney(order.cost, settle)}` : order.cost != null ? ` · margin ${formatHistoricalMoney(order.cost, settle)}` : ""}`
                    : flow != null ? formatHistoricalSignedMoney(flow, quote) : "—"}
                </td>
                <td className="py-2 pr-4">
                  <Badge variant={order.mode === "live" ? "destructive" : "secondary"}>
                    {order.mode === "live" ? "LIVE" : t.common.simulated}

                  </Badge>
                </td>
                <td className="py-2 font-mono text-xs">{order.order_id ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
