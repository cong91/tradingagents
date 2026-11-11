"use client";

// "Needs handling": the one block that answers the operator's first question.
//
// The server owns priority and dedup (one entry per kind), so this list never
// re-ranks or repeats. Each row states its urgency in words, not colour alone
// (WCAG 1.4.1), and links to the screen where the thing can actually be acted
// on — the brief itself carries no control that changes trading state.

import Link from "next/link";
import { TriangleAlertIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { useT } from "@/lib/i18n";
import type { Messages } from "@/lib/i18n";

import type { AttentionItem } from "./contract";

const PRIORITY_VARIANT: Record<number, "destructive" | "secondary" | "outline"> = {
  1: "destructive",
  2: "secondary",
  3: "outline",
};

/** Copy for one item, keyed on the server's stable `kind`. */
function attentionText(t: Messages, item: AttentionItem): string {
  const a = t.brief.attention;
  const params = item.message_params ?? {};
  const count = (key: string): string => {
    const raw = params[key];
    return raw === null || raw === undefined ? "—" : String(raw);
  };
  switch (item.kind) {
    case "server_degraded":
      return a.serverDegraded;
    case "risk_halted":
      return a.riskHalted;
    case "protection_unconfirmed":
      return a.protectionMissing(count("count"));
    case "approvals_pending":
      return a.approvalsPending(count("count"));
    case "coverage_error":
      return a.coverageError;
    case "coverage_partial":
      return a.coveragePartial;
    case "stale_snapshot":
      return a.staleSnapshot;
    case "pipeline_errors":
      return a.pipelineErrors(count("count"));
    default:
      // An unmapped kind must still be legible: show the server's code rather
      // than an empty row, so a new server kind is visibly unlabelled, not silent.
      return item.message_code;
  }
}

export function AttentionList({ items }: { items: AttentionItem[] }) {
  const t = useT();
  const a = t.brief.attention;

  if (items.length === 0) {
    return (
      <section className="space-y-2" aria-label={a.title}>
        <h3 className="text-sm font-semibold">{a.title}</h3>
        <p className="text-sm text-muted-foreground">{a.none}</p>
      </section>
    );
  }

  return (
    <section className="space-y-2" aria-label={a.title}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">{a.title}</h3>
        <Badge variant="secondary">{a.count(items.length)}</Badge>
      </div>      <ul className="space-y-2">
        {items.map((item) => (
          <li
            key={item.id}
            data-attention-kind={item.kind}
            className="flex flex-wrap items-start gap-2 rounded-lg border px-3 py-2 text-sm"
          >
            <TriangleAlertIcon
              className="mt-0.5 size-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1 space-y-0.5">
              <p className="font-medium">{attentionText(t, item)}</p>
              {item.detail ? (
                <p className="break-words text-xs text-muted-foreground">{item.detail}</p>
              ) : null}
            </div>
            <Badge variant={PRIORITY_VARIANT[item.priority] ?? "outline"}>
              {a.priority[item.priority] ?? item.priority}
            </Badge>
            <Link
              href={item.link}
              className="min-touch inline-flex items-center text-xs underline underline-offset-4"
            >
              {a.open}
              <span className="sr-only"> — {attentionText(t, item)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
