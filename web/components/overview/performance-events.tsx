import { Badge } from "@/components/ui/badge";
import { formatDateTime, formatQuantityValue, MINUS_SIGN } from "@/lib/format";

import { useT, type Messages } from "@/lib/i18n";

import type { PerformanceOrder } from "./contract";
import { historicalCurrencies } from "./historical-currency";

const ACTION_VARIANTS: Record<string, "outline" | "secondary" | "destructive"> = {
  buy: "outline",
  sell: "outline",
  bracket_placed: "secondary",
  bracket_failed: "destructive",
  rejected_by_risk: "destructive",
  rejected_at_execute: "destructive",
  execute_denied: "destructive",
  derivatives_denied: "destructive",
  time_stop_closed: "secondary",
  time_stop_postponed: "secondary",
  position_state_failed: "destructive",
  scale_out_refused: "destructive",
  no_order: "secondary",
};

type EventsCopy = Messages["performance"]["events"];

function actionBadge(c: EventsCopy, action: string | null): {
  label: string;
  variant: "outline" | "secondary" | "destructive";
} {
  const known = action ? c.actions[action] : undefined;
  if (action && known) return { label: known, variant: ACTION_VARIANTS[action] ?? "outline" };
  return { label: action || "—", variant: "outline" };
}

function modeLabel(c: EventsCopy, mode: string | null): string {
  if (mode === "dry") return c.modeDry;
  if (mode === "demo") return c.modeDemo;
  if (mode === "live") return c.modeLive;
  return mode ? c.modeUnknown(mode) : "—";
}

function marketLabel(market: string | null | undefined): string {
  if (market === "derivatives") return "Futures";
  if (market === "spot") return "Spot";
  return market || "—";
}

function quantityValue(value: number | null | undefined, signed = false): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  const formatted = magnitude !== 0 && (magnitude < 0.00000001 || magnitude >= 1e21)
    ? String(magnitude)
    : formatQuantityValue(magnitude);
  const sign = value < 0 ? MINUS_SIGN : signed && value > 0 ? "+" : "";
  return `${sign}${formatted}`;
}

function estimatedPrice(
  value: number | null,
  symbol: string | null | undefined,
  unknownCurrency: string,
): string {
  const amount = quantityValue(value);
  if (amount === "—") return amount;
  return `${amount} ${historicalCurrencies(symbol).quote ?? unknownCurrency}`;
}

export function PerformanceEvents({
  events,
  skippedLines,
}: {
  events: PerformanceOrder[];
  skippedLines: number | null;
}) {
  const t = useT();
  const c = t.performance.events;
  return (
    <section className="min-w-0 space-y-2" aria-label={c.title}>
      <h3 className="text-sm font-medium">{c.title}</h3>
      <p className="text-xs text-muted-foreground">
        {c.note}
      </p>
      {skippedLines == null ? (
        <p role="status" className="text-sm text-destructive">
          {c.skippedUnknown}
        </p>
      ) : skippedLines > 0 ? (
        <p role="status" className="text-sm text-destructive">
          {c.skipped(skippedLines)}
        </p>
      ) : null}
      {events.length === 0 ? (
        <p className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
          {c.empty}
        </p>
      ) : (
        <div
          role="region"
          aria-label={c.tableAria}
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <table className="w-full min-w-[850px] text-sm">
            <caption className="sr-only">{c.caption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">{c.time}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.event}</th>
                <th scope="col" className="py-2 pr-4 font-medium">{c.market}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.quantity}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.priceEst}</th>
                <th scope="col" className="py-2 font-medium">{c.mode}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {events.map((event, index) => {
                const action = actionBadge(c, event.action);
                return (
                  <tr key={`${event.timestamp ?? ""}-${event.order_id ?? ""}-${index}`}>
                    <td className="py-2 pr-4 whitespace-nowrap tabular-nums">{formatDateTime(event.timestamp)}</td>
                    <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{event.ticker || "—"}</th>
                    <td className="py-2 pr-4"><Badge variant={action.variant}>{action.label}</Badge></td>
                    <td className="py-2 pr-4">{marketLabel(event.market)}</td>
                    <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{quantityValue(event.qty, true)}</td>
                    <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{estimatedPrice(event.price_est, event.ccxt_symbol, t.performance.unknownCurrency)}</td>
                    <td className="py-2"><Badge variant="secondary">{modeLabel(c, event.mode)}</Badge></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
