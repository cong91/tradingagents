// Một entry quyết định trong decision log — dùng chung cho /api/history
// và phần chi tiết /api/backtest (cùng shape server/history.py:48-73).

import { Badge } from "@/components/ui/badge";
import { cn } from "cn";
import type { HistoryItem } from "./types";
import { formatLoggedPercent, signedTextTone } from "./display";

function RatingBadge({ rating }: { rating: string }) {
  switch (rating) {
    case "Buy":
    case "Overweight":
      return (
        <Badge variant="outline" className="border-transparent bg-profit/10 text-profit">
          {rating}
        </Badge>
      );
    case "Sell":
    case "Underweight":
      return (
        <Badge variant="outline" className="border-transparent bg-loss/10 text-loss">
          {rating}
        </Badge>
      );
    case "Hold":
      return <Badge variant="secondary">{rating}</Badge>;
    case "REVIEW":
      return <Badge variant="destructive">{rating}</Badge>;
    default:
      return <Badge variant="outline">{rating}</Badge>;
  }
}

export function HistoryEntryRow({ item }: { item: HistoryItem }) {
  const rawPct = formatLoggedPercent(item.raw);
  const alphaPct = formatLoggedPercent(item.alpha);
  const artifact =
    item.run_artifacts.state_log ?? item.run_artifacts.saved_report;
  return (
    <li className="rounded-xl border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-muted-foreground">
          {item.date}
        </span>
        <span className="text-sm font-semibold">{item.ticker}</span>
        <RatingBadge rating={item.rating} />
        {item.pending && (
          <Badge
            variant="outline"
            className="border-amber-500/60 text-amber-900 dark:text-amber-100"
          >
            Chưa settle
          </Badge>
        )}
        {item.resolved && <Badge variant="secondary">Settle {item.resolved}</Badge>}
      </div>
      {(rawPct || alphaPct || item.holding) && (
        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {rawPct && (
            <span>
              Kết quả giá:{" "}
              <span className={cn("tabular-nums", signedTextTone(rawPct))}>
                {rawPct}
              </span>
            </span>
          )}
          {alphaPct && (
            <span>
              Alpha:{" "}
              <span className={cn("tabular-nums", signedTextTone(alphaPct))}>
                {alphaPct}
              </span>
            </span>
          )}
          {item.holding && <span>Giữ vị thế: {item.holding}</span>}
        </div>
      )}
      {item.decision_excerpt && (
        <p className="mt-2 line-clamp-3 text-sm">{item.decision_excerpt}</p>
      )}
      {item.reflection && (
        <p className="mt-1 line-clamp-2 text-xs italic text-muted-foreground">
          Suy ngẫm: {item.reflection}
        </p>
      )}
      {artifact && (
        <p
          className="mt-2 truncate font-mono text-[11px] text-muted-foreground"
          title={artifact}
        >
          Tệp run: {artifact}
        </p>
      )}
    </li>
  );
}
