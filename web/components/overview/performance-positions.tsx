import { LoaderCircleIcon, RefreshCwIcon, TriangleAlertIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";

import type { PortfolioPayload } from "./contract";
import { FuturesPositions } from "./performance-futures-positions";
import { SpotPositions } from "./performance-spot-positions";
import type { PanelState } from "./use-panel";

export function PerformancePositions({
  portfolioState,
  stale,
  futuresStale,
  onRetry,
}: {
  portfolioState: PanelState<PortfolioPayload>;
  stale: boolean;
  futuresStale: boolean;
  onRetry: () => void;
}) {
  const t = useT();
  const c = t.performance.positions;
  if (portfolioState.status === "loading") {
    return (
      <p role="status" className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
        <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
        {c.loading}
      </p>
    );
  }

  if (portfolioState.status === "error") {
    return (
      <div role="alert" className="space-y-2 rounded-lg border border-dashed px-4 py-4 text-sm">
        <p className="text-destructive">{c.cannotRead(portfolioState.code, portfolioState.message)}</p>
        <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
          <RefreshCwIcon aria-hidden="true" /> {t.common.retry}
        </Button>
      </div>
    );
  }

  const data = portfolioState.data;
  const isStale = stale || portfolioState.status === "stale";

  return (
    <div className="min-w-0 space-y-4">
      {isStale ? (
        <div role="alert" className="space-y-2 rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          <p className="flex items-start gap-2">
            <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              {c.stale}
              {portfolioState.status === "stale" ? c.lastUpdateFailed(portfolioState.code, portfolioState.message) : ""}
            </span>
          </p>
          <Button variant="outline" size="sm" className="min-touch" onClick={onRetry}>
            <RefreshCwIcon aria-hidden="true" /> {t.common.retry}
          </Button>
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">{c.generatedAt(formatDateTime(data.generated_at))}</p>
      {(isStale || futuresStale) && data.futures?.status === "ok" ? (
        <p role="alert" className="rounded-lg border border-amber-600/40 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:bg-amber-950 dark:text-amber-100">
          {c.futuresStale}
        </p>
      ) : null}
      <FuturesPositions futures={data.futures} stale={isStale || futuresStale} />
      <SpotPositions data={data} stale={isStale} />
      {data.warnings.length > 0 ? (
        <ul className="list-inside list-disc space-y-1 break-words text-xs text-muted-foreground">
          {data.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}
        </ul>
      ) : null}
    </div>
  );
}
