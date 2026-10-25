import { Badge } from "@/components/ui/badge";
import { EmptyState } from "./empty-state";
import type { AuditItem } from "./contract";
import {
  formatHistoricalMoney,
  formatHistoricalSignedMoney,
  historicalCurrencies,
} from "./historical-currency";

const qtyFormat = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 8 });

function formatTime(timestamp: string | null): string {
  if (!timestamp) return "—";
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? timestamp
    : date.toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "medium" });
}

/** Lệnh thực thi = dòng audit action buy/sell ở phase "execute" (hợp đồng §6). */
function isExecutedOrder(item: AuditItem): boolean {
  return item.phase === "execute" && (item.action === "buy" || item.action === "sell");
}

export function OrderTable({ items }: { items: AuditItem[] }) {
  const orders = items.filter(isExecutedOrder);

  if (orders.length === 0) {
    return (
      <EmptyState
        title="Chưa có lệnh nào được thực thi"
        hint="Lệnh xuất hiện sau khi pipeline tạo kế hoạch và kế hoạch đó được duyệt ở màn Duyệt lệnh."
        action={{ label: "Mở màn Duyệt lệnh", href: "/approvals" }}
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th scope="col" className="py-2 pr-4 font-medium">Thời gian</th>
            <th scope="col" className="py-2 pr-4 font-medium">Cặp</th>
            <th scope="col" className="py-2 pr-4 font-medium">Lệnh</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">Khối lượng</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">Giá ước tính</th>
            <th scope="col" className="py-2 pr-4 text-right font-medium">Spot dòng tiền / Futures notional</th>
            <th scope="col" className="py-2 pr-4 font-medium">Chế độ</th>
            <th scope="col" className="py-2 font-medium">Order ID</th>
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
                    <Badge variant="outline">{isBuy ? "Mua" : "Bán"}</Badge>
                    <Badge variant="secondary">{isDerivatives ? "Futures" : "Spot"}</Badge>
                  </div>
                </td>
                <td className="py-2 pr-4 text-right tabular-nums">{qty != null ? qtyFormat.format(qty) : "—"}</td>
                <td className="py-2 pr-4 text-right tabular-nums">{formatHistoricalMoney(price, quote)}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${!isDerivatives && flow != null && flow !== 0 ? flow > 0 ? "text-profit" : "text-loss" : ""}`}>
                  {isDerivatives
                    ? `${formatHistoricalMoney(notional, quote)} notional${order.leverage != null ? ` · ${qtyFormat.format(order.leverage)}x · margin ${formatHistoricalMoney(order.cost, settle)}` : order.cost != null ? ` · margin ${formatHistoricalMoney(order.cost, settle)}` : ""}`
                    : flow != null ? formatHistoricalSignedMoney(flow, quote) : "—"}
                </td>
                <td className="py-2 pr-4">
                  <Badge variant={order.mode === "live" ? "destructive" : "secondary"}>
                    {order.mode === "live" ? "LIVE" : "Mô phỏng"}
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
