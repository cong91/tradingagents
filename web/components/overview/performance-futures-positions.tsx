import { Badge } from "@/components/ui/badge";
import {
  formatDateTime,
  pnlTextClass,
} from "@/lib/format";

import type { FuturesPortfolio } from "./contract";

function quantityValue(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(value).replace(".", ",");
}

function signedQuantity(value: number | null): string {
  const formatted = quantityValue(value);
  return value != null && Number.isFinite(value) && value > 0 ? `+${formatted}` : formatted;
}

function snapshotMoney(value: number | null, currency: string | null, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const amount = quantityValue(Math.abs(value));
  const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
  return `${sign}${amount} ${currency || "đơn vị chưa rõ"}`;
}

function venueLabel(profile: string | null): string {
  if (profile === "demo") return "Demo Trading";
  if (profile === "production") return "Production";
  return profile ? `Chưa xác minh (${profile})` : "Môi trường chưa xác định";
}

function positionIssueLabel(reason: string): string {
  switch (reason) {
    case "read_failed": return "Không đọc được danh sách vị thế từ sàn";
    case "scope_unverified": return "Chưa xác minh đầy đủ phạm vi hoặc phân trang của sàn";
    case "invalid_response": return "Phản hồi danh sách vị thế không hợp lệ";
    case "invalid_row": return "Dữ liệu vị thế không hợp lệ";
    case "missing_symbol": return "Thiếu mã vị thế";
    case "missing_size": return "Thiếu số lượng hợp đồng";
    case "invalid_size": return "Số lượng hợp đồng không hợp lệ";
    case "inconsistent_size": return "Số lượng hợp đồng không nhất quán";
    case "missing_side": return "Thiếu hướng vị thế";
    case "invalid_side": return "Hướng vị thế không hợp lệ";
    case "inconsistent_side": return "Hướng vị thế không nhất quán";
    default: return "Không xác định được lý do bỏ qua";
  }
}

export function FuturesPositions({
  futures,
  stale,
}: {
  futures: FuturesPortfolio | null | undefined;
  stale: boolean;
}) {
  const read = futures?.positions_read;
  const observedAt =
    typeof read?.observed_at === "string" && Number.isFinite(Date.parse(read.observed_at))
      ? read.observed_at
      : null;
  const positions = Array.isArray(futures?.positions) ? futures.positions : null;
  const issues = Array.isArray(read?.issues) ? read.issues : null;
  const isComplete =
    observedAt !== null && positions !== null && read?.status === "complete" && read.skipped === 0 && issues !== null && issues.length === 0;

  return (
    <section className="min-w-0 space-y-2" aria-label="Vị thế Futures">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">Vị thế Futures</h3>
        <Badge variant={futures?.venue_profile === "production" ? "destructive" : "secondary"}>
          {venueLabel(futures?.venue_profile ?? null)}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        Nguồn Futures: snapshot tài khoản sàn. Thời điểm đọc vị thế: {observedAt ? formatDateTime(observedAt) : "không xác định"}.
      </p>
      {!futures || futures.status === "unavailable" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Snapshot Futures không khả dụng; chưa xác định được các vị thế đang mở.
        </p>
      ) : futures.status === "error" ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-destructive">
          Không đọc được vị thế Futures: {futures.error ?? "lỗi không xác định"}. Không thể xác nhận danh sách vị thế.
        </p>
      ) : positions === null ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Danh sách vị thế Futures không khả dụng; không thể xác nhận vị thế đang mở.
        </p>
      ) : (
        <>
          {observedAt === null ? (
            <p role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
              Không xác định được thời điểm đọc vị thế Futures; không thể xác nhận dữ liệu hiện tại.
            </p>
          ) : null}
          {!isComplete ? (
            <div role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
              {read?.status === "unavailable"
                ? "Không đọc được danh sách Futures; không thể xác nhận vị thế đang mở."
                : "Chưa xác minh được danh sách Futures đầy đủ; có thể còn vị thế chưa hiển thị."}
              {read?.skipped != null && read.skipped > 0 ? ` Đã bỏ qua ${read.skipped} mục.` : ""}
              {read && read.skipped == null ? " Không xác định được số mục đã bỏ qua." : ""}
            </div>
          ) : null}
          {issues && issues.length > 0 ? (
            <ul className="list-inside list-disc space-y-1 break-words text-xs text-muted-foreground">
              {issues.map((issue, index) => (
                <li key={`${issue.symbol ?? "unknown"}-${index}`}>
                  {issue.symbol ?? "Mã không xác định"}: {positionIssueLabel(issue.reason)}
                </li>
              ))}
            </ul>
          ) : null}
          {positions.length > 0 ? (
            <div
              role="region"
              aria-label="Bảng vị thế Futures, có thể cuộn ngang"
              tabIndex={0}
              className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <table className="w-full min-w-[700px] text-sm">
                <caption className="sr-only">Vị thế Futures được sàn báo cáo trong snapshot</caption>
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th scope="col" className="py-2 pr-4 font-medium">Mã</th>
                    <th scope="col" className="py-2 pr-4 font-medium">Hướng</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">Hợp đồng</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">Số lượng có dấu</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">Giá vào</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">Giá mark</th>
                    <th scope="col" className="py-2 text-right font-medium">Lãi/lỗ chưa chốt</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {positions.map((position, index) => (
                    <tr key={`${position.symbol ?? "unknown"}-${position.side ?? "unknown"}-${index}`}>
                      <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{position.symbol ?? "—"}</th>
                      <td className="py-2 pr-4 whitespace-nowrap">
                        {position.side === "long" ? "Long (+)" : position.side === "short" ? "Short (−)" : "Không rõ"}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">
                        {quantityValue(position.contracts)}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">{signedQuantity(position.quantity)}</td>
                      <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.entry_price, position.price_currency)}</td>
                      <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.mark_price, position.price_currency)}</td>
                      <td className={`py-2 text-right whitespace-nowrap tabular-nums ${position.unrealized_pnl != null ? pnlTextClass(position.unrealized_pnl) : ""}`}>
                        {snapshotMoney(position.unrealized_pnl, position.unrealized_pnl_currency, true)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
              {isComplete && !stale
                ? "Sàn không báo vị thế Futures đang mở trong snapshot này."
                : "Chưa thể xác nhận có vị thế Futures đang mở hay không."}
            </p>
          )}
        </>
      )}
    </section>
  );
}
