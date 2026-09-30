// Định dạng số hiển thị cho PnL. Quy ước bắt buộc của UI:
// lãi luôn kèm dấu "+" (token màu `profit`/xanh), lỗ luôn kèm dấu "−" (token `loss`/đỏ).
// Màu không bao giờ là tín hiệu duy nhất (WCAG 1.4.1 Use of Color).

const numberFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// Dấu trừ toán học (U+2212), phân biệt với gạch nối.
const MINUS_SIGN = "\u2212";

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

/** Luôn kèm dấu: "+1.234,56" / "−312,40" / "0,00". */
export function formatSignedNumber(value: number): string {
  const abs = numberFormat.format(Math.abs(value));
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
