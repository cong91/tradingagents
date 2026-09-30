// Định dạng hiển thị cho màn M3. Số tiền luôn kèm đơn vị; dòng tiền lệnh
// luôn kèm dấu +/− (bán = tiền vào "+", mua = tiền ra "−") và nhãn chữ —
// màu không bao giờ là tín hiệu duy nhất (WCAG 1.4.1).

const usdFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const qtyFormat = new Intl.NumberFormat("vi-VN", {
  maximumFractionDigits: 8,
});

const MINUS_SIGN = "\u2212";

/** "BTC-USD" → "BTC" (đơn vị của quantity). */
export function baseAsset(ticker: string): string {
  return ticker.split("-")[0] ?? ticker;
}

/** Giá ước tính: "83.078,00 USD" (không phải dòng tiền nên không có dấu). */
export function formatUsd(value: number): string {
  return `${usdFormat.format(value)} USD`;
}

/** Số lượng kèm tài sản gốc: "0,0012 BTC". */
export function formatQty(value: number, ticker: string): string {
  return `${qtyFormat.format(value)} ${baseAsset(ticker)}`;
}

/** Dòng tiền của lệnh: "−99,69 USD" (mua) / "+99,69 USD" (bán). */
export function formatCashFlow(side: string, cost: number): string {
  const abs = usdFormat.format(Math.abs(cost));
  if (side.toLowerCase() === "sell") return `+${abs} USD`;
  return `${MINUS_SIGN}${abs} USD`;
}

export function cashFlowLabel(side: string): string {
  return side.toLowerCase() === "sell" ? "Tiền vào" : "Tiền ra";
}

export function sideLabel(side: string): string {
  return side.toLowerCase() === "sell" ? "Bán" : "Mua";
}

const addedAtFormat = new Intl.DateTimeFormat("vi-VN", {
  dateStyle: "medium",
  timeStyle: "short",
});

export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return addedAtFormat.format(date);
}

export function elapsedLabel(fromIso: string, nowMs: number): string {
  const start = Date.parse(fromIso);
  if (Number.isNaN(start)) return "";
  const seconds = Math.max(0, Math.floor((nowMs - start) / 1000));
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 1) return `${minutes} phút ${seconds % 60} giây`;
  return `${seconds} giây`;
}
