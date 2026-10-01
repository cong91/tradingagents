// Định dạng hiển thị dùng chung toàn control panel (vi-VN). Quy ước bắt buộc
// của UI: lãi luôn kèm dấu "+" (token màu `profit`/xanh), lỗ luôn kèm dấu "−"
// (token `loss`/đỏ). Màu không bao giờ là tín hiệu duy nhất (WCAG 1.4.1).
//
// Các helper dấu-theo-phía-lệnh (buy/sell) CÓ CHỦ ĐÍCH khác nhau giữa màn
// Scanner (tiền vào/ra) và Duyệt lệnh (dòng tiền của lệnh) — chúng nằm ở
// components/<màn>/ và gọi các primitive dưới đây, không tự parse lại.

const numberFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const qtyFormat = new Intl.NumberFormat("vi-VN", {
  maximumFractionDigits: 8,
});

// Dấu trừ toán học (U+2212), phân biệt với gạch nối.
export const MINUS_SIGN = "\u2212";

export type PnlTone = "profit" | "loss" | "neutral";

export function pnlTone(value: number): PnlTone {
  if (value > 0) return "profit";
  if (value < 0) return "loss";
  return "neutral";
}

const PNL_TEXT_CLASS: Record<PnlTone, string> = {
  profit: "text-profit",
  loss: "text-loss",
  neutral: "",
};

export function pnlTextClass(value: number): string {
  return PNL_TEXT_CLASS[pnlTone(value)];
}

/** Luôn kèm dấu: "+1.234,56" / "−312,40" / "0,00" (mặc định 2 chữ số thập phân). */
export function formatSignedNumber(value: number, fractionDigits = 2): string {
  const formatter =
    fractionDigits === 2
      ? numberFormat
      : new Intl.NumberFormat("vi-VN", {
          minimumFractionDigits: fractionDigits,
          maximumFractionDigits: fractionDigits,
        });
  const abs = formatter.format(Math.abs(value));
  if (value > 0) return `+${abs}`;
  if (value < 0) return `${MINUS_SIGN}${abs}`;
  return abs;
}

export function formatSignedCurrency(value: number, currency = "USD"): string {
  return `${formatSignedNumber(value)} ${currency}`;
}

/** Phần trăm không dấu: "54,3%". */
export function formatPercent(value: number, fractionDigits = 1): string {
  return `${new Intl.NumberFormat("vi-VN", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value)}%`;
}

/** Giá ước tính kèm đơn vị: "83.078,00 USD"; null/NaN → "—". */
export function formatUsdPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${usdFormat.format(value)} USD`;
}

/** Tiền theo đơn vị danh mục server trả (vd "USDT"): "9.500,00 USDT"; null/NaN → "—". */
export function formatMoney(
  value: number | null | undefined,
  currency: string
): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${usdFormat.format(value)} ${currency}`;
}

/** Tài sản gốc của cặp: "BTC/USDT" hoặc "BTC-USD" → "BTC"; không parse được → nguyên văn/null. */
export function baseAssetOf(
  ccxtSymbol: string | null,
  ticker: string | null
): string | null {
  if (ccxtSymbol && ccxtSymbol.includes("/")) return ccxtSymbol.split("/")[0];
  if (ticker && ticker.includes("-")) return ticker.split("-")[0];
  return ticker ?? null;
}

/** Khối lượng thuần, vi-VN tối đa 8 chữ số thập phân: "0,0012". */
export function formatQuantityValue(value: number): string {
  return qtyFormat.format(value);
}

/** Khối lượng kèm tài sản gốc; null/NaN → "—", thiếu cặp → chỉ số. */
export function formatAssetQuantity(
  qty: number | null | undefined,
  ticker?: string | null
): string {
  if (qty == null || Number.isNaN(qty)) return "—";
  const unit = baseAssetOf(null, ticker ?? null);
  return unit ? `${qtyFormat.format(qty)} ${unit}` : qtyFormat.format(qty);
}

const clockFormat = new Intl.DateTimeFormat("vi-VN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/** ISO-8601 UTC → giờ địa phương "HH:mm:ss"; input sai → nguyên văn. */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return clockFormat.format(date);
}

const dateTimeFormat = new Intl.DateTimeFormat("vi-VN", {
  dateStyle: "short",
  timeStyle: "medium",
  hour12: false,
});

/** ISO → "30/09/2026, 08:15:40"; null/undefined → "—"; input sai → nguyên văn. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return dateTimeFormat.format(date);
}

const timestampFormat = new Intl.DateTimeFormat("vi-VN", {
  dateStyle: "medium",
  timeStyle: "short",
});

/** ISO → "30 thg 9, 2026 08:15" (dạng mềm cho timestamp theo dõi); lỗi → nguyên văn. */
export function formatTimestampVi(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return timestampFormat.format(date);
}
