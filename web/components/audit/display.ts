// Định dạng hiển thị riêng cho màn Audit. Phần trăm lãi/lỗ luôn kèm dấu +/−;
// dấu trừ là U+2212 (tách khỏi gạch nối) qua lib/format.ts. Màu không bao giờ
// là tín hiệu duy nhất. Primitive giá/khối lượng/thời gian dùng chung ở
// lib/format.ts.

import {
  formatAssetQuantity,
  formatDateTime,
  formatSignedNumber,
  formatUsdPrice,
} from "@/lib/format";

const percentFormat = new Intl.NumberFormat("vi-VN", {
  style: "percent",
  maximumFractionDigits: 1,
});

/** Giá ước tính — luôn kèm đơn vị tiền: "83.078,00 USD". */
export function formatPriceEst(price: number | null | undefined): string {
  return formatUsdPrice(price);
}

/** Khối lượng theo tài sản gốc của cặp (BTC-USD → BTC): "0,0012 BTC". */
export function formatQty(
  qty: number | null | undefined,
  ticker?: string | null
): string {
  return formatAssetQuantity(qty, ticker);
}

/** Chuỗi % ghi trong log ("+1.2%") → hiển thị vi-VN có dấu ("+1,2%"). */
export function formatLoggedPercent(
  text: string | null | undefined
): string | null {
  if (!text) return null;
  const trimmed = text.trim();
  const match = /^([+-]?\d+(?:\.\d+)?)\s*%$/.exec(trimmed);
  if (!match) return trimmed; // không parse được — giữ nguyên văn, không bịa
  return `${formatSignedNumber(Number(match[1]))}%`;
}

/** Phân số alpha từ backend (0.012 = +1,2%): luôn kèm dấu và đơn vị %. */
export function formatFractionPercent(
  fraction: number | null | undefined
): string | null {
  if (fraction == null || Number.isNaN(fraction)) return null;
  return `${formatSignedNumber(fraction * 100)}%`;
}

/** Tỉ lệ trúng (0..1) → "50,0%" — không cần dấu vì không phải lãi/lỗ. */
export function formatHitRate(rate: number | null | undefined): string {
  if (rate == null || Number.isNaN(rate)) return "—";
  return percentFormat.format(rate);
}

export { formatDateTime };

/** Dấu hiệu hướng màu cho chuỗi đã có dấu: "+1,2%" → profit; "−0,4%" → loss. */
export function signedTextTone(text: string): string {
  if (text.startsWith("+")) return "text-profit";
  if (text.startsWith("-") || text.startsWith("\u2212")) return "text-loss";
  return "";
}
