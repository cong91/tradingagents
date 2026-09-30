// Định dạng hiển thị riêng cho màn Duyệt lệnh.
// Khác lib/format.ts (PnL — dấu theo dấu của số), ở đây dấu +/− của số tiền
// theo PHÍA lệnh (buy/sell) — quy ước UI bắt buộc: số tiền luôn có đơn vị và
// dấu, màu không bao giờ là tín hiệu duy nhất.

const MINUS_SIGN = "\u2212"; // U+2212 — phân biệt với gạch nối

const moneyFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function formatNumber(value: number, maxFractionDigits = 8): string {
  return new Intl.NumberFormat("vi-VN", {
    maximumFractionDigits: maxFractionDigits,
  }).format(value);
}

/** Tài sản gốc từ "BTC/USDT" → "BTC", fallback "BTC-USD" → "BTC". */
export function baseAsset(
  ccxtSymbol: string | null,
  ticker: string | null
): string | null {
  if (ccxtSymbol && ccxtSymbol.includes("/")) return ccxtSymbol.split("/")[0];
  if (ticker && ticker.includes("-")) return ticker.split("-")[0];
  return ticker ?? null;
}

/**
 * Giá trị lệnh có dấu theo phía: buy → "+99,69 USD", sell → "−99,69 USD".
 * Phía lạ/không rõ → số tuyệt đối kèm đơn vị (không tự bịa dấu).
 */
export function formatOrderAmount(cost: number, side: string | null): string {
  const abs = `${moneyFormat.format(Math.abs(cost))} USD`;
  if (side === "buy") return `+${abs}`;
  if (side === "sell") return `${MINUS_SIGN}${abs}`;
  return abs;
}

/** Giá ước tính — mức giá, không phải biến động: luôn có đơn vị, không gắn dấu. */
export function formatPrice(value: number): string {
  return `${moneyFormat.format(value)} USD`;
}

/** Khối lượng kèm tài sản gốc: "0,0012 BTC". */
export function formatQuantity(
  quantity: number,
  ccxtSymbol: string | null,
  ticker: string | null
): string {
  const base = baseAsset(ccxtSymbol, ticker);
  return `${formatNumber(quantity)}${base ? ` ${base}` : ""}`;
}

/** ISO-8601 UTC → "30/09/2026 08:15:40" theo locale vi-VN. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("vi-VN", { hour12: false });
}
