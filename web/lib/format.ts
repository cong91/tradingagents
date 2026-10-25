// Định dạng hiển thị dùng chung toàn control panel — theo locale đang chọn
// (en-US / vi-VN, xem lib/i18n/locale.ts). Quy ước bắt buộc
// của UI: lãi luôn kèm dấu "+" (token màu `profit`/xanh), lỗ luôn kèm dấu "−"
// (token `loss`/đỏ). Màu không bao giờ là tín hiệu duy nhất (WCAG 1.4.1).
//
// Các helper dấu-theo-phía-lệnh (buy/sell) CÓ CHỦ ĐÍCH khác nhau giữa màn
// Scanner (tiền vào/ra) và Duyệt lệnh (dòng tiền của lệnh) — chúng nằm ở
// components/<màn>/ và gọi các primitive dưới đây, không tự parse lại.

import { getLocale, INTL_LOCALE } from "@/lib/i18n/locale";

// Intl formatters are costly to build — cache one per (locale, options).
const numberFormats = new Map<string, Intl.NumberFormat>();
const dateFormats = new Map<string, Intl.DateTimeFormat>();

function numberFormat(options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const tag = INTL_LOCALE[getLocale()];
  const key = `${tag}|${JSON.stringify(options)}`;
  let formatter = numberFormats.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(tag, options);
    numberFormats.set(key, formatter);
  }
  return formatter;
}

function dateFormat(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const tag = INTL_LOCALE[getLocale()];
  const key = `${tag}|${JSON.stringify(options)}`;
  let formatter = dateFormats.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(tag, options);
    dateFormats.set(key, formatter);
  }
  return formatter;
}

const TWO_DECIMALS: Intl.NumberFormatOptions = {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
};
const QTY_OPTIONS: Intl.NumberFormatOptions = { maximumFractionDigits: 8 };

/** Số thuần theo locale hiện hành (thay cho Intl.NumberFormat hard-code). */
export function formatNumber(value: number, options: Intl.NumberFormatOptions = {}): string {
  return numberFormat(options).format(value);
}

/** Ngày/giờ theo locale hiện hành; input sai → nguyên văn. */
export function formatDateWith(
  value: string | number | Date,
  options: Intl.DateTimeFormatOptions
): string {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return dateFormat(options).format(date);
}

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
  const formatter = numberFormat({
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
  const abs = formatter.format(Math.abs(value));
  if (value > 0) return `+${abs}`;
  if (value < 0) return `${MINUS_SIGN}${abs}`;
  return abs;
}

export function formatSignedCurrency(value: number, currency?: string | null): string {
  const amount = formatSignedNumber(value);
  return currency ? `${amount} ${currency}` : amount;
}

/** Phần trăm không dấu: "54,3%". */
export function formatPercent(value: number, fractionDigits = 1): string {
  return `${numberFormat({
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value)}%`;
}

/** Giá ước tính kèm đơn vị: "83.078,00 USD"; null/NaN → "—". */
export function formatUsdPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${numberFormat(TWO_DECIMALS).format(value)} USD`;
}

/** Tiền theo đơn vị danh mục server trả (vd "USDT"): "9.500,00 USDT"; null/NaN → "—". */
export function formatMoney(
  value: number | null | undefined,
  currency?: string | null
): string {
  if (value == null || Number.isNaN(value)) return "—";
  const amount = numberFormat(TWO_DECIMALS).format(value);
  return currency ? `${amount} ${currency}` : amount;
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

/** Khối lượng thuần, tối đa 8 chữ số thập phân: "0,0012". */
export function formatQuantityValue(value: number): string {
  return numberFormat(QTY_OPTIONS).format(value);
}

/** Khối lượng kèm tài sản gốc; null/NaN → "—", thiếu cặp → chỉ số. */
export function formatAssetQuantity(
  qty: number | null | undefined,
  ticker?: string | null
): string {
  if (qty == null || Number.isNaN(qty)) return "—";
  const unit = baseAssetOf(null, ticker ?? null);
  return unit ? `${numberFormat(QTY_OPTIONS).format(qty)} ${unit}` : numberFormat(QTY_OPTIONS).format(qty);
}

const CLOCK_OPTIONS: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
};

/** ISO-8601 UTC → giờ địa phương "HH:mm:ss"; input sai → nguyên văn. */
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return dateFormat(CLOCK_OPTIONS).format(date);
}

const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  dateStyle: "short",
  timeStyle: "medium",
  hour12: false,
};

/** ISO → "30/09/2026, 08:15:40" (vi) / "9/30/26, 08:15:40" (en); null/undefined → "—"; input sai → nguyên văn. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return dateFormat(DATE_TIME_OPTIONS).format(date);
}

const TIMESTAMP_OPTIONS: Intl.DateTimeFormatOptions = {
  dateStyle: "medium",
  timeStyle: "short",
};

/** ISO → "30 thg 9, 2026 08:15" / "Sep 30, 2026, 8:15 AM" (dạng mềm cho timestamp theo dõi); lỗi → nguyên văn. */
export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return dateFormat(TIMESTAMP_OPTIONS).format(date);
}
