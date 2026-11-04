import { Badge } from "@/components/ui/badge";
import {
  decimalSeparator,
  formatDateTime,
  pnlTextClass,
} from "@/lib/format";
import { useT, type Messages } from "@/lib/i18n";

import type { FuturesPortfolio } from "./contract";

function quantityValue(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(value).replace(".", decimalSeparator());
}

function signedQuantity(value: number | null): string {
  const formatted = quantityValue(value);
  return value != null && Number.isFinite(value) && value > 0 ? `+${formatted}` : formatted;
}

function snapshotMoney(
  value: number | null,
  currency: string | null,
  unknownCurrency: string,
  signed = false,
): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const amount = quantityValue(Math.abs(value));
  const sign = value < 0 ? "−" : signed && value > 0 ? "+" : "";
  return `${sign}${amount} ${currency || unknownCurrency}`;
}

function venueLabel(t: Messages, profile: string | null): string {
  if (profile === "demo") return "Demo Trading";
  if (profile === "production") return "Production";
  return profile ? t.performance.venue.unverified(profile) : t.performance.venue.unknown;
}

export function FuturesPositions({
  futures,
  stale,
}: {
  futures: FuturesPortfolio | null | undefined;
  stale: boolean;
}) {
  const t = useT();
  const c = t.performance.futures;
  const unknownCurrency = t.performance.unknownCurrency;
  const read = futures?.positions_read;
  const observedAt =
    typeof read?.observed_at === "string" && Number.isFinite(Date.parse(read.observed_at))
      ? read.observed_at
      : null;
  const positions = Array.isArray(futures?.positions) ? futures.positions : null;
  const issues = Array.isArray(read?.issues) ? read.issues : null;
  const isComplete =
    observedAt !== null && positions !== null && read?.status === "complete" && read.skipped === 0 && issues !== null && issues.length === 0;

  return (
    <section className="min-w-0 space-y-2" aria-label={c.title}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">{c.title}</h3>
        <Badge variant={futures?.venue_profile === "production" ? "destructive" : "secondary"}>
          {venueLabel(t, futures?.venue_profile ?? null)}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        {c.source(observedAt ? formatDateTime(observedAt) : c.unknownTime)}
      </p>
      {!futures || futures.status === "unavailable" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.unavailable}
        </p>
      ) : futures.status === "error" ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-destructive">
          {c.error(futures.error ?? c.unknownError)}
        </p>
      ) : positions === null ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.listUnavailable}
        </p>
      ) : (
        <>
          {observedAt === null ? (
            <p role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
              {c.noObservedAt}
            </p>
          ) : null}
          {!isComplete ? (
            <div role="status" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
              {read?.status === "unavailable"
                ? c.readUnavailable
                : c.incomplete}
              {read?.skipped != null && read.skipped > 0 ? c.skipped(read.skipped) : ""}
              {read && read.skipped == null ? c.skippedUnknown : ""}
            </div>
          ) : null}
          {issues && issues.length > 0 ? (
            <ul className="list-inside list-disc space-y-1 break-words text-xs text-muted-foreground">
              {issues.map((issue, index) => (
                <li key={`${issue.symbol ?? "unknown"}-${index}`}>
                  {issue.symbol ?? c.unknownSymbol}: {c.issues[issue.reason] ?? c.issueUnknown}
                </li>
              ))}
            </ul>
          ) : null}
          {positions.length > 0 ? (
            <div
              role="region"
              aria-label={c.tableAria}
              tabIndex={0}
              className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            >
              <table className="w-full min-w-[700px] text-sm">
                <caption className="sr-only">{c.caption}</caption>
                <thead>
                  <tr className="border-b text-left text-xs text-muted-foreground">
                    <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
                    <th scope="col" className="py-2 pr-4 font-medium">{c.side}</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">{c.contracts}</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">{c.signedQty}</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">{c.entryPrice}</th>
                    <th scope="col" className="py-2 pr-4 text-right font-medium">{c.markPrice}</th>
                    <th scope="col" className="py-2 text-right font-medium">{c.unrealizedPnl}</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {positions.map((position, index) => (
                    <tr key={`${position.symbol ?? "unknown"}-${position.side ?? "unknown"}-${index}`}>
                      <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{position.symbol ?? "—"}</th>
                      <td className="py-2 pr-4 whitespace-nowrap">
                        {position.side === "long" ? "Long (+)" : position.side === "short" ? "Short (−)" : c.sideUnknown}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">
                        {quantityValue(position.contracts)}
                      </td>
                      <td className="py-2 pr-4 text-right tabular-nums">{signedQuantity(position.quantity)}</td>
                      <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.entry_price, position.price_currency, unknownCurrency)}</td>
                      <td className="py-2 pr-4 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.mark_price, position.price_currency, unknownCurrency)}</td>
                      <td className={`py-2 text-right whitespace-nowrap tabular-nums ${position.unrealized_pnl != null ? pnlTextClass(position.unrealized_pnl) : ""}`}>
                        {snapshotMoney(position.unrealized_pnl, position.unrealized_pnl_currency, unknownCurrency, true)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
              {isComplete && !stale
                ? c.noPositions
                : c.unconfirmed}
            </p>
          )}
        </>
      )}
    </section>
  );
}
