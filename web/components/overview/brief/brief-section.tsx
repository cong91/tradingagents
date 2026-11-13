// Shared shells for the brief's blocks: a section with a real heading level and
// a label/value row that can say "not enough data" instead of printing a zero.
//
// CardTitle in components/ui/card.tsx is a div, so a brief full of cards would
// leave the page with headings only at h3 and no h2 to hang them under
// (measured: h2=0, h3=9 before this change). Every block here renders its own
// h3 inside the card's h2, which restores heading navigation for screen readers.

import type { ReactNode } from "react";

import { cn } from "cn";

export function BriefSection({
  title,
  children,
  note,
}: {
  title: string;
  children: ReactNode;
  note?: string | null;
}) {
  return (
    <section className="space-y-2" aria-label={title}>
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
      {note ? <p className="text-xs text-muted-foreground">{note}</p> : null}
    </section>
  );
}

/** One label/value pair. `value === null` renders the explicit unknown; a
 * `label === null` row (a field the server did not report) renders nothing —
 * a nameless row is noise, not honesty. */
export function ScalarRow({
  label,
  value,
  unknownLabel,
  tone,
  reason,
}: {
  label: string | null;
  value: string | null;
  unknownLabel: string;
  tone?: "profit" | "loss" | null;
  reason?: string | null;
}) {
  if (label === null) return null;
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          "min-w-0 text-right text-sm font-medium tabular-nums",
          tone === "profit" ? "text-profit" : tone === "loss" ? "text-loss" : ""
        )}
      >
        {value ?? <span className="text-muted-foreground">{unknownLabel}</span>}
      </dd>
      {value === null && reason ? (
        <span className="sr-only">{reason}</span>
      ) : null}
    </div>
  );
}

/** A block whose data is unavailable: says so, with the server's reason. */
export function UnavailableNote({ label, reason }: { label: string; reason: string | null }) {
  return (
    <p className="text-sm text-muted-foreground">
      {label}
      {reason ? <span className="block text-xs">{reason}</span> : null}
    </p>
  );
}

/**
 * Progressive disclosure for detail that would otherwise flood the fold:
 * native <details> keeps it keyboard-operable and needs no client state.
 */
export function BriefDetails({
  summary,
  children,
}: {
  summary: string;
  children: ReactNode;
}) {
  return (
    <details className="rounded-lg border border-dashed px-3 py-2">
      <summary className="min-touch flex cursor-pointer items-center text-xs font-medium">
        {summary}
      </summary>
      <div className="space-y-2 pt-2">{children}</div>
    </details>
  );
}
