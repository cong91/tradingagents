// Định dạng hiển thị riêng cho màn Duyệt lệnh.
// Khác lib/format.ts (PnL — dấu theo dấu của số), ở đây dấu +/− của số tiền
// theo PHÍA lệnh (buy/sell) — quy ước UI bắt buộc: số tiền luôn có đơn vị và
// dấu, màu không bao giờ là tín hiệu duy nhất. Primitive số/giá/thời gian
// dùng chung ở lib/format.ts.

import {
  baseAssetOf,
  formatDateTime,
  formatQuantityValue,
  formatUsdPrice,
} from "@/lib/format";

export { baseAssetOf as baseAsset };

/**
 * Giá trị lệnh có dấu theo phía: buy → "+99,69 USD", sell → "−99,69 USD".
 * Phía lạ/không rõ → số tuyệt đối kèm đơn vị (không tự bịa dấu).
 */
export function formatOrderAmount(cost: number, side: string | null): string {
  const abs = `${formatUsdPrice(Math.abs(cost))}`;
  if (side === "buy") return `+${abs}`;
  if (side === "sell") return `\u2212${abs}`;
  return abs;
}

/** Giá ước tính — mức giá, không phải biến động: luôn có đơn vị, không gắn dấu. */
export function formatPrice(value: number): string {
  return formatUsdPrice(value);
}

/** Khối lượng kèm tài sản gốc: "0,0012 BTC". */
export function formatQuantity(
  quantity: number,
  ccxtSymbol: string | null,
  ticker: string | null
): string {
  const base = baseAssetOf(ccxtSymbol, ticker);
  return `${formatQuantityValue(quantity)}${base ? ` ${base}` : ""}`;
}

export { formatDateTime };
