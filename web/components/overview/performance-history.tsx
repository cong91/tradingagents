import { Badge } from "@/components/ui/badge";
import { decimalSeparator, formatDateTime, pnlTextClass } from "@/lib/format";
import { useT, type Messages } from "@/lib/i18n";

import { historicalCurrencies } from "./historical-currency";

const MAX_VISIBLE_FILLS = 100;

export type PerformanceHistoryData = {
  status: "ok" | "partial" | "error" | "unsupported" | "unavailable";
  source: string;
  venue_profile: string | null;
  start_at: string | null;
  end_at: string | null;
  observed_at: string | null;
  coverage: string;
  warnings: string[];
  fills: {
    trade_id: string;
    order_id: string | null;
    symbol: string;
    side: string | null;
    timestamp: string;
    contracts: string | null;
    price: string | null;
    fee: string | null;
    fee_currency: string | null;
    realized_pnl: string | null;
    realized_pnl_currency: string | null;
  }[];
};

function decimalText(value: string | null, signed = false): string | null {
  if (value == null) return null;
  const text = value.trim();
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)) return null;
  const numeric = Number(text);
  if (!Number.isFinite(numeric)) return null;
  const negative = text.startsWith("-");
  const unsigned = text.replace(/^[+-]/, "");
  const nonzero = /[1-9]/.test(unsigned.split(/e/i)[0]);
  const sign = negative && nonzero ? "−" : signed && nonzero ? "+" : "";
  return `${sign}${unsigned.replace(".", decimalSeparator())}`;
}

function reportedMoney(
  value: string | null,
  currency: string | null,
  unknownCurrency: string,
  signed = false,
): string {
  const amount = decimalText(value, signed);
  return amount == null ? "—" : `${amount} ${currency || unknownCurrency}`;
}

function venueLabel(t: Messages, profile: string | null): string {
  if (profile === "demo") return "Demo Trading";
  if (profile === "production") return "Production";
  return profile ? t.performance.venue.unverified(profile) : t.performance.venue.unknown;
}

function sideLabel(c: Messages["performance"]["history"], side: string | null): string {
  if (side?.toLowerCase() === "buy") return c.buy;
  if (side?.toLowerCase() === "sell") return c.sell;
  return side || c.sideUnknown;
}

function sourceLabel(c: Messages["performance"]["history"], source: string): string {
  if (source === "venue_fills") return c.sourceVenueFills;
  if (source === "unavailable") return c.sourceUnavailable;
  return c.sourceUnverified;
}

export function PerformanceHistory({ history }: { history: PerformanceHistoryData }) {
  const t = useT();
  const c = t.performance.history;
  const unknownCurrency = t.performance.unknownCurrency;
  const hasReadableFills =
    history.source === "venue_fills" && (history.status === "ok" || history.status === "partial");
  const visibleFills = hasReadableFills
    ? history.fills
        .map((fill) => ({ fill, time: Date.parse(fill.timestamp) }))
        .sort((a, b) => {
          if (Number.isNaN(a.time)) return Number.isNaN(b.time) ? 0 : 1;
          if (Number.isNaN(b.time)) return -1;
          return b.time - a.time;
        })
        .slice(0, MAX_VISIBLE_FILLS)
        .map(({ fill }) => fill)
    : [];

  return (
    <section className="min-w-0 space-y-3" aria-label={c.title}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">{c.title}</h3>
        <Badge variant={history.venue_profile === "production" ? "destructive" : "secondary"}>
          {venueLabel(t, history.venue_profile)}
        </Badge>
      </div>
      <div className="space-y-1 break-words text-xs text-muted-foreground">
        <p>{c.sourceLine(sourceLabel(c, history.source), history.coverage || c.coverageUnknown)}</p>
        <p>
          {c.period(formatDateTime(history.start_at), formatDateTime(history.end_at))}
          {" · "}{c.observed(formatDateTime(history.observed_at))}
        </p>
      </div>
      {history.status === "partial" ? (
        <p role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          {c.partial}
        </p>
      ) : history.status === "error" ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-destructive">
          {c.error}
        </p>
      ) : history.status === "unsupported" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.unsupported}
        </p>
      ) : history.status === "unavailable" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.unavailable}
        </p>
      ) : null}
      {hasReadableFills ? (
        <p className="text-xs text-muted-foreground">
          {c.pnlNote}
        </p>
      ) : null}
      {history.warnings.length > 0 ? (
        <ul className="list-inside list-disc space-y-1 break-words text-xs text-muted-foreground">
          {history.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
        </ul>
      ) : null}
      {!hasReadableFills && (history.status === "ok" || history.status === "partial") ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.unconfirmedFills}
        </p>
      ) : null}
      {hasReadableFills && history.fills.length > 0 ? (
        <div
          role="region"
          aria-label={c.tableAria}
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <p className="text-xs text-muted-foreground">
            {c.showing(visibleFills.length, history.fills.length)}
          </p>
          <table className="w-full min-w-[840px] text-sm">
            <caption className="sr-only">{c.caption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">{c.time}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.order}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.contracts}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.price}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.realizedPnl}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.fee}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.tradeId}</th>
                <th scope="col" className="py-2 font-medium">{c.orderId}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {visibleFills.map((fill, index) => (
                <tr key={`${fill.symbol}-${fill.trade_id}-${index}`}>
                  <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatDateTime(fill.timestamp)}</td>
                  <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{fill.symbol || "—"}</th>
                  <td className="py-2 pr-4 whitespace-nowrap">{sideLabel(c, fill.side)}</td>
                  <td className="py-2 pr-4 text-right tabular-nums">
                    {decimalText(fill.contracts) ?? "—"}
                  </td>
                  <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{reportedMoney(fill.price, historicalCurrencies(fill.symbol).quote, unknownCurrency)}</td>
                  <td className={`py-2 pr-4 text-right whitespace-nowrap tabular-nums ${fill.realized_pnl != null && decimalText(fill.realized_pnl) !== null ? pnlTextClass(Number(fill.realized_pnl)) : ""}`}>
                    {reportedMoney(fill.realized_pnl, fill.realized_pnl_currency, unknownCurrency, true)}
                  </td>
                  <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{reportedMoney(fill.fee, fill.fee_currency, unknownCurrency)}</td>
                  <td className="py-2 pr-4 font-mono text-xs">{fill.trade_id || "—"}</td>
                  <td className="py-2 font-mono text-xs">{fill.order_id || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : hasReadableFills ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {history.status === "partial"
            ? c.noFillsPartial
            : c.noFills}
        </p>
      ) : null}
    </section>
  );
}
