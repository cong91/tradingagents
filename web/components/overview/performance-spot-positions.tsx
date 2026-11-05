import { formatQuantityValue } from "@/lib/format";
import { useT } from "@/lib/i18n";

import type { PortfolioPayload } from "./contract";

function quantityValue(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  return magnitude !== 0 && (magnitude < 0.00000001 || magnitude >= 1e21)
    ? String(value)
    : formatQuantityValue(value);
}

function spotQuantity(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value > 0 ? `+${value}` : String(value);
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

export function SpotPositions({ data, stale }: { data: PortfolioPayload; stale: boolean }) {
  const t = useT();
  const c = t.performance.spot;
  const source = data.positions_source;
  const positions = Array.isArray(data.positions) ? data.positions : null;
  return (
    <section className="min-w-0 space-y-2" aria-label={c.title}>
      <h3 className="text-sm font-medium">{c.title}</h3>
      <p className="text-xs text-muted-foreground">
        {source === "venue"
          ? c.sourceVenue
          : source === "audit"
            ? c.sourceAudit
            : c.sourceUnverified}
      </p>
      {stale ? (
        <p role="alert" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          {c.stale}
        </p>
      ) : source !== "venue" ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.notVenue}
        </p>
      ) : positions === null ? (
        <p role="alert" className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.listUnavailable}
        </p>
      ) : positions.length === 0 ? (
        <p className="rounded-lg border border-dashed px-4 py-4 text-sm text-muted-foreground">
          {c.empty}
        </p>
      ) : (
        <div
          role="region"
          aria-label={c.tableAria}
          tabIndex={0}
          className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <table className="w-full min-w-[420px] text-sm">
            <caption className="sr-only">{c.caption}</caption>
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th scope="col" className="py-2 pr-4 font-medium">{c.ticker}</th>
                <th scope="col" className="py-2 pr-4 text-right font-medium">{c.signedQty}</th>
                <th scope="col" className="py-2 text-right font-medium">{c.marketPrice}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {positions.map((position, index) => (
                <tr key={`${position.ticker}-${index}`}>
                  <th scope="row" className="py-2 pr-4 text-left font-mono font-medium">{position.ticker}</th>
                  <td className="py-2 pr-4 text-right tabular-nums">{spotQuantity(position.quantity)}</td>
                  <td className="py-2 text-right whitespace-nowrap tabular-nums">{snapshotMoney(position.marked_price, null, t.performance.unknownCurrency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
