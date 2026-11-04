import { Badge } from "@/components/ui/badge";
import { formatDateTime, formatQuantityValue, MINUS_SIGN } from "@/lib/format";

import type { PerformanceOrder } from "./contract";
import { historicalCurrencies } from "./historical-currency";

function actionBadge(action: string | null): {
  label: string;
  variant: "outline" | "secondary" | "destructive";
} {
  switch (action) {
    case "buy":
      return { label: "Mua", variant: "outline" };
    case "sell":
      return { label: "Bán", variant: "outline" };
    case "bracket_placed":
      return { label: "Đặt lệnh bảo vệ", variant: "secondary" };
    case "bracket_failed":
      return { label: "Lỗi đặt lệnh bảo vệ", variant: "destructive" };
    case "rejected_by_risk":
      return { label: "Từ chối bởi kiểm soát rủi ro", variant: "destructive" };
    case "rejected_at_execute":
      return { label: "Từ chối khi thực thi", variant: "destructive" };
    case "execute_denied":
      return { label: "Không cho phép thực thi", variant: "destructive" };
    case "derivatives_denied":
      return { label: "Từ chối Futures", variant: "destructive" };
    case "time_stop_closed":
      return { label: "Đóng theo thời hạn", variant: "secondary" };
    case "time_stop_postponed":
      return { label: "Hoãn đóng theo thời hạn", variant: "secondary" };
    case "position_state_failed":
      return { label: "Lỗi lưu trạng thái vị thế", variant: "destructive" };
    case "scale_out_refused":
      return { label: "Từ chối chốt từng phần", variant: "destructive" };
    case "no_order":
      return { label: "Không tạo lệnh", variant: "secondary" };
    default:
      return { label: action || "—", variant: "outline" };
  }
}

function modeLabel(mode: string | null): string {
  if (mode === "dry") return "Dry (mô phỏng)";
  if (mode === "demo") return "Demo";
  if (mode === "live") return "Live (chế độ sàn)";
  return mode ? `Chưa xác định (${mode})` : "—";
}

function marketLabel(market: string | null | undefined): string {
  if (market === "derivatives") return "Futures";
  if (market === "spot") return "Spot";
  return market || "—";
}

function quantityValue(value: number | null | undefined, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  const formatted = magnitude !== 0 && (magnitude < 0.00000001 || magnitude >= 1e21)
    ? String(magnitude)
    : formatQuantityValue(magnitude);
  const sign = value < 0 ? MINUS_SIGN : signed && value > 0 ? "+" : "";
  return `${sign}${formatted}`;
}

function estimatedPrice(value: number | null, symbol: string | null | undefined): string {
  const amount = quantityValue(value);
  if (amount === "—") return amount;
  return `${amount} ${historicalCurrencies(symbol).quote ?? "đơn vị chưa rõ"}`;
}

export function PerformanceEvents({
  events,
  skippedLines,
}: {
  events: PerformanceOrder[];
  skippedLines: number | null;
}) {
  return (
    <section className="min-w-0 space-y-2" aria-label="Nhật ký sự kiện">
      <h3 className="text-sm font-medium">Nhật ký sự kiện</h3>
      <p className="text-xs text-muted-foreground">
        Dữ liệu nhật ký không xác nhận khớp lệnh. Live là chế độ thực thi qua sàn,
        không xác định môi trường demo hay production.
      </p>
      {skippedLines == null ? (
        <p role="status" className="text-sm text-destructive">
          Không xác minh được nhật ký có dòng bị bỏ qua hay không.
        </p>
      ) : skippedLines > 0 ? (
        <p role="status" className="text-sm text-destructive">
          Cảnh báo: {skippedLines} dòng nhật ký hỏng đã bị bỏ qua; dữ liệu có thể chưa đầy đủ.
        </p>
      ) : null}
      {events.length === 0 ? (
        <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
          Không có sự kiện trong dữ liệu nhật ký đã đọc.
        </p>
      ) : (
        <div
          role="region"
          aria-label="Bảng nhật ký sự kiện, có thể cuộn ngang"
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <table className="w-full min-w-[850px] text-sm">
            <caption className="sr-only">Các sự kiện trong nhật ký thực thi</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">Thời gian</th>
                <th scope="col" className="py-2 pr-4 font-medium">Mã</th>
                <th scope="col" className="py-2 pr-4 font-medium">Sự kiện</th>
                <th scope="col" className="py-2 pr-4 font-medium">Thị trường</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Khối lượng (dữ liệu nhật ký)</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Giá ước tính</th>
                <th scope="col" className="py-2 font-medium">Chế độ</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {events.map((event, index) => {
                const action = actionBadge(event.action);
                return (
                  <tr key={`${event.timestamp ?? ""}-${event.order_id ?? ""}-${index}`}>
                    <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatDateTime(event.timestamp)}</td>
                    <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{event.ticker || "—"}</th>
                    <td className="py-2 pr-4"><Badge variant={action.variant}>{action.label}</Badge></td>
                    <td className="py-2 pr-4">{marketLabel(event.market)}</td>
                    <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{quantityValue(event.qty, true)}</td>
                    <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{estimatedPrice(event.price_est, event.ccxt_symbol)}</td>
                    <td className="py-2"><Badge variant="secondary">{modeLabel(event.mode)}</Badge></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
