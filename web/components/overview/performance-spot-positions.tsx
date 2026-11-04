import { formatQuantityValue } from "@/lib/format";

import type { PortfolioPayload } from "./contract";

function quantityValue(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  return magnitude !== 0 && (magnitude < 0.00000001 || magnitude >= 1e21)
    ? String(value)
    : formatQuantityValue(value);
}

function spotQuantity(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value > 0 ? `+${value}` : String(value);
}

function snapshotMoney(value: number | null, currency: string | null, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const amount = quantityValue(Math.abs(value));
  const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
  return `${sign}${amount} ${currency || "đơn vị chưa rõ"}`;
}

export function SpotPositions({ data, stale }: { data: PortfolioPayload; stale: boolean }) {
  const source = data.positions_source;
  const positions = Array.isArray(data.positions) ? data.positions : null;
  return (
    <section className="min-w-0 space-y-2" aria-label="Vị thế Spot">
      <h3 className="text-sm font-medium">Vị thế Spot</h3>
      <p className="text-xs text-muted-foreground">
        {source === "venue"
          ? "Nguồn Spot: snapshot số dư từ sàn, chỉ gồm các tài sản Spot được hệ thống nhận diện; có thể còn tài sản khác không được hiển thị. Đơn vị giá thị trường chưa được xác minh."
          : source === "audit"
            ? "Dữ liệu nhật ký có thể gồm giao dịch mô phỏng và không xác nhận vị thế hiện tại."
            : "Nguồn dữ liệu Spot chưa được xác minh."}
      </p>
      {stale ? (
        <p role="alert" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          Dữ liệu Spot đã cũ; không thể xác nhận trạng thái Spot hiện tại.
        </p>
      ) : source !== "venue" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Vị thế Spot hiện tại không khả dụng vì không có snapshot được xác minh từ sàn.
        </p>
      ) : positions === null ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Danh sách tài sản Spot được hệ thống nhận diện không khả dụng; không thể xác nhận vị thế hiện tại.
        </p>
      ) : positions.length === 0 ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          Không có tài sản Spot được hệ thống nhận diện trong snapshot này; có thể còn tài sản khác không được hiển thị.
        </p>
      ) : (
        <div
          role="region"
          aria-label="Bảng vị thế Spot, có thể cuộn ngang"
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <table className="w-full min-w-[420px] text-sm">
            <caption className="sr-only">Các tài sản Spot được hệ thống nhận diện trong snapshot sàn</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">Mã</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">Số lượng có dấu</th>
                <th scope="col" className="py-2 text-right font-medium">Giá thị trường</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {positions.map((position, index) => (
                <tr key={`${position.ticker}-${index}`}>
                  <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{position.ticker}</th>
                  <td className="py-2 pr-4 text-right tabular-nums">{spotQuantity(position.quantity)}</td>
                  <td className="py-2 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.marked_price, null)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
