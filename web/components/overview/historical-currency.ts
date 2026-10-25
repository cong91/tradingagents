import { formatMoney, formatSignedNumber } from "@/lib/format";
import { getMessages } from "@/lib/i18n/messages";

const unknownCurrency = () => getMessages().overview.unknownCurrency;

/** Units recorded on the row, never inferred from today's venue configuration. */
export function historicalCurrencies(ccxtSymbol: string | null | undefined): {
  quote: string | null;
  settle: string | null;
} {
  const match = ccxtSymbol?.trim().match(/^[^/:\s]+\/([^/:\s]+)(?::([^/:\s-]+)(?:-[^/:\s]+)?)?$/);
  return { quote: match?.[1] ?? null, settle: match?.[2] ?? null };
}

export function formatHistoricalMoney(
  value: number | null | undefined,
  currency: string | null,
): string {
  return formatMoney(value, currency ?? unknownCurrency());
}

export function formatHistoricalSignedMoney(
  value: number | null | undefined,
  currency: string | null,
): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${formatSignedNumber(value)} ${currency ?? unknownCurrency()}`;
}
