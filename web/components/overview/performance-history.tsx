import { Badge } from "@/components/ui/badge";
import { formatDateTime, pnlTextClass } from "@/lib/format";

import { historicalCurrencies } from "./historical-currency";

const MAX_VISIBLE_FILLS = 100;

export type PerformanceHistoryData = {
  status: "ok" | "partial" | "error" | "unsupported" | "unavailable";
  source: string;
  venue_profile: string | null;
  start_at: string | null;
  end_at: string | null;
  observed_at: string | null;
  coverage: string;
  warnings: string[];
  fills: {
    trade_id: string;
    order_id: string | null;
    symbol: string;
    side: string | null;
    timestamp: string;
    contracts: string | null;
    price: string | null;
    fee: string | null;
    fee_currency: string | null;
    realized_pnl: string | null;
    realized_pnl_currency: string | null;
  }[];
};

function decimalText(value: string | null, signed = false): string | null {
  if (value == null) return null;
  const text = value.trim();
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)) return null;
  const numeric = Number(text);
  if (!Number.isFinite(numeric)) return null;
  const negative = text.startsWith("-");
  const unsigned = text.replace(/^[+-]/, "");
  const nonzero = /[1-9]/.test(unsigned.split(/e/i)[0]);
  const sign = negative && nonzero ? "−" : signed && nonzero ? "+" : "";
  return `${sign}${unsigned.replace(".", ",")}`;
}

function reportedMoney(value: string | null, currency: string | null, signed = false): string {
  const amount = decimalText(value, signed);
  return amount == null ? "—" : `${amount} ${currency || "đơn vị chưa rõ"}`;
}

function venueLabel(profile: string | null): string {
  if (profile === "demo") return "Demo Trading";
  if (profile === "production") return "Production";
  return profile ? `Chưa xác minh (${profile})` : "Môi trường chưa xác định";
}

function sideLabel(side: string | null): string {
  if (side?.toLowerCase() === "buy") return "Mua";
  if (side?.toLowerCase() === "sell") return "Bán";
  return side || "Không rõ";
}

function sourceLabel(source: string): string {
  if (source === "venue_fills") return "Giao dịch khớp do sàn cung cấp";
  if (source === "unavailable") return "Không có dữ liệu sàn";
  return "Nguồn chưa xác minh";
}

export function PerformanceHistory({ history }: { history: PerformanceHistoryData }) {
  const hasReadableFills =
    history.source === "venue_fills" && (history.status === "ok" || history.status === "partial");
  const visibleFills = hasReadableFills
    ? history.fills
        .map((fill) => ({ fill, time: Date.parse(fill.timestamp) }))
        .sort((a, b) => {
          if (Number.isNaN(a.time)) return Number.isNaN(b.time) ? 0 : 1;
          if (Number.isNaN(b.time)) return -1;
          return b.time - a.time;
        })
        .slice(0, MAX_VISIBLE_FILLS)
        .map(({ fill }) => fill)
    : [];

  return (
    <section className="min-w-0 space-y-3" aria-label="Lịch sử khớp lệnh Futures">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">Lịch sử khớp lệnh Futures</h3>
        <Badge variant={history.venue_profile === "production" ? "destructive" : "secondary"}>
          {venueLabel(history.venue_profile)}
        </Badge>
      </div>
      <div className="space-y-1 break-words text-xs text-muted-foreground">
        <p>Nguồn: {sourceLabel(history.source)} · Phạm vi: {history.coverage || "chưa xác định"}</p>
        <p>
          Khoảng thời gian: {formatDateTime(history.start_at)} – {formatDateTime(history.end_at)}
          {" · "}Ghi nhận: {formatDateTime(history.observed_at)}
        </p>
      </div>
      {history.status === "partial" ? (
        <p role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          Lịch sử chỉ đầy đủ một phần; có thể còn khớp lệnh chưa được ghi nhận.
        </p>
      ) : history.status === "error" ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-destructive">
          Không đọc được lịch sử khớp lệnh; không thể xác nhận dữ liệu.
        </p>
      ) : history.status === "unsupported" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Nguồn này không hỗ trợ lịch sử khớp lệnh Futures.
        </p>
      ) : history.status === "unavailable" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Lịch sử khớp lệnh Futures không khả dụng; không thể xác nhận dữ liệu.
        </p>
      ) : null}
      {hasReadableFills ? (
        <p className="text-xs text-muted-foreground">
          Lãi/lỗ là số sàn báo cho từng lần khớp, chưa trừ phí hay cộng funding.
          Khớp lệnh một phần không đồng nghĩa toàn bộ vị thế đã đóng.
        </p>
      ) : null}
      {history.warnings.length > 0 ? (
        <ul className="list-inside list-disc space-y-1 break-words text-xs text-muted-foreground">
          {history.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
        </ul>
      ) : null}
      {!hasReadableFills && (history.status === "ok" || history.status === "partial") ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Không thể xác nhận khớp lệnh do sàn cung cấp từ nguồn này.
        </p>
      ) : null}
      {hasReadableFills && history.fills.length > 0 ? (
        <div
          role="region"
          aria-label="Bảng lịch sử khớp lệnh Futures, có thể cuộn ngang"
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <p className="text-xs text-muted-foreground">
            Hiển thị {visibleFills.length} / {history.fills.length} khớp lệnh nhận được, mới nhất trước.
          </p>
          <table className="w-full min-w-[840px] text-sm">
            <caption className="sr-only">Các khớp lệnh Futures theo nguồn đã báo cáo</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">Thời gian</th>
                <th scope="col" className="py-2 pr-4 font-medium">Mã</th>
                <th scope="col" className="py-2 pr-4 font-medium">Lệnh</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Hợp đồng</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Giá</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Lãi/lỗ đã chốt</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Phí</th>
                <th scope="col" className="py-2 pr-4 font-medium">Mã khớp lệnh</th>
                <th scope="col" className="py-2 font-medium">Mã lệnh</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {visibleFills.map((fill, index) => (
                <tr key={`${fill.symbol}-${fill.trade_id}-${index}`}>
                  <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatDateTime(fill.timestamp)}</td>
                  <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{fill.symbol || "—"}</th>
                  <td className="py-2 pr-4 whitespace-nowrap">{sideLabel(fill.side)}</td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {decimalText(fill.contracts) ?? "—"}
                  </td>
                  <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{reportedMoney(fill.price, historicalCurrencies(fill.symbol).quote)}</td>
                  <td className={`py-2 pr-4 text-right whitespace-nowrap tabular-nums ${fill.realized_pnl != null && decimalText(fill.realized_pnl) !== null ? pnlTextClass(Number(fill.realized_pnl)) : ""}`}>
                    {reportedMoney(fill.realized_pnl, fill.realized_pnl_currency, true)}
                  </td>
                  <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{reportedMoney(fill.fee, fill.fee_currency)}</td>
                  <td className="py-2 pr-4 font-mono text-xs">{fill.trade_id || "—"}</td>
                  <td className="py-2 font-mono text-xs">{fill.order_id || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : hasReadableFills ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {history.status === "partial"
            ? "Chưa có khớp lệnh trong phần lịch sử đọc được; các giao dịch khác có thể bị thiếu."
            : "Không có khớp lệnh nào được báo cáo trong phạm vi này."}
        </p>
      ) : null}
    </section>
  );
}
