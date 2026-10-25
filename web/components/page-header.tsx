"use client";

import { useEffect } from "react";

import { useT } from "@/lib/i18n";
import type { PageKey } from "@/lib/i18n/messages/pages";

/**
 * Translated page heading. Static `metadata` titles are English (the server
 * cannot see the stored locale), so the document title is synced here too.
 */
export function PageHeader({
  page,
  description,
}: {
  page: PageKey;
  /** Overrides the default lead paragraph (e.g. loading copy). */
  description?: string;
}) {
  const t = useT();
  const copy = t.pages[page];

  useEffect(() => {
    document.title = `${copy.title} — TradingAgents`;
  }, [copy.title]);

  return (
    <header className="mb-6 space-y-1">
      <h1 className="text-2xl font-semibold tracking-tight">{copy.title}</h1>
      <p className="max-w-2xl text-sm text-muted-foreground">
        {description ?? copy.description}
      </p>
    </header>
  );
}
