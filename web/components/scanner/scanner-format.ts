// Định dạng hiển thị riêng cho màn Scanner. Dòng tiền lệnh luôn kèm dấu +/−
// (bán = tiền vào "+", mua = tiền ra "−") và nhãn chữ — màu không bao giờ là
// tín hiệu duy nhất (WCAG 1.4.1). Primitive số/giá/thời gian dùng chung ở
// lib/format.ts.

import {
  baseAssetOf,
  formatQuantityValue,
  formatTimestamp,
  formatUsdPrice,
} from "@/lib/format";

import { getMessages } from "@/lib/i18n/messages";

export { formatTimestamp };

/** "BTC-USD" → "BTC" (đơn vị của quantity). */
export function baseAsset(ticker: string): string {
  return baseAssetOf(null, ticker) ?? ticker;
}

/** Giá ước tính: "83.078,00 USD" (không phải dòng tiền nên không có dấu). */
export function formatUsd(value: number): string {
  return formatUsdPrice(value);
}

/** Số lượng kèm tài sản gốc: "0,0012 BTC". */
export function formatQty(value: number, ticker: string): string {
  return `${formatQuantityValue(value)} ${baseAsset(ticker)}`;
}

/** Dòng tiền của lệnh: "−99,69 USD" (mua) / "+99,69 USD" (bán). */
export function formatCashFlow(side: string, cost: number): string {
  const abs = formatUsdPrice(Math.abs(cost));
  if (side.toLowerCase() === "sell") return `+${abs}`;
  return `\u2212${abs}`;
}

export function cashFlowLabel(side: string): string {
  const m = getMessages().scanner;
  return side.toLowerCase() === "sell" ? m.cashIn : m.cashOut;
}

export function sideLabel(side: string): string {
  const m = getMessages().common;
  return side.toLowerCase() === "sell" ? m.sell : m.buy;
}

export function elapsedLabel(fromIso: string, nowMs: number): string {
  const start = Date.parse(fromIso);
  if (Number.isNaN(start)) return "";
  const seconds = Math.max(0, Math.floor((nowMs - start) / 1000));
  const minutes = Math.floor(seconds / 60);
  const m = getMessages().common;
  if (minutes >= 1) return m.minutesSeconds(minutes, seconds % 60);
  return m.seconds(seconds);
}

/** Nhãn trạng thái job theo locale (contract §5 rev 7); trạng thái lạ → nguyên văn. */
export function jobStatusLabel(status: string): string {
  return getMessages().scanner.jobStatus[status] ?? status;
}

/** Job đã kết thúc (mọi trạng thái không còn chiếm worker). */
export function isTerminalStatus(status: string): boolean {
  return !["queued", "running"].includes(status);
}
