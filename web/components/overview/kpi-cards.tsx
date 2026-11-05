import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatPercent, formatSignedNumber } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { outcomeOf, type DecisionSummary } from "./summary";

const TONE_TEXT_CLASS = { profit: "text-profit", loss: "text-loss", flat: "" } as const;

export function KpiCards({ summary }: { summary: DecisionSummary }) {
  const t = useT();
  const k = t.overview.kpi;
  const outcome = outcomeOf(summary.totalAlphaPct);
  const winRate =
    summary.directionalCount > 0
      ? (summary.winCount / summary.directionalCount) * 100
      : null;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Card size="sm">
        <CardHeader>
          <CardDescription>{k.cumulativeAlpha}</CardDescription>
          <CardTitle
            className={`text-2xl tabular-nums ${TONE_TEXT_CLASS[outcome]}`}
            aria-label={k.cumulativeAlphaAria(
              formatSignedNumber(summary.totalAlphaPct, 1),
              t.overview.outcome[outcome]
            )}
          >
            {formatSignedNumber(summary.totalAlphaPct, 1)}&nbsp;%
          </CardTitle>
          {/* Tín hiệu không chỉ dựa vào màu: dấu +/− trong số và chữ "Lãi/Lỗ" bên ngoài màu (WCAG 1.4.1). */}
          <Badge variant={outcome === "loss" ? "destructive" : "secondary"}>
            {t.overview.outcome[outcome]}
          </Badge>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>{k.meanAlpha}</CardDescription>
          <CardTitle
            className={`text-2xl tabular-nums ${TONE_TEXT_CLASS[outcomeOf(summary.meanAlphaPct)]}`}
          >
            {formatSignedNumber(summary.meanAlphaPct, 1)}&nbsp;%
          </CardTitle>
          <CardDescription>{k.onResolved(summary.resolvedCount)}</CardDescription>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>{k.winRate}</CardDescription>
          <CardTitle className="text-2xl tabular-nums">
            {winRate === null ? "—" : formatPercent(winRate)}
          </CardTitle>
          <CardDescription>
            {summary.directionalCount > 0
              ? k.directionalHits(summary.winCount, summary.directionalCount)
              : k.noDirectional}
          </CardDescription>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>{k.decisions}</CardDescription>
          <CardTitle className="text-2xl tabular-nums">{summary.resolvedCount}</CardTitle>
          <CardDescription>
            {k.pendingReview(summary.pendingCount, summary.unscoredCount)}

          </CardDescription>
        </CardHeader>
      </Card>
    </div>
  );
}
