import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatPercent } from "@/lib/format";
import { outcomeOf, OUTCOME_LABEL, type DecisionSummary } from "./summary";

// Dấu trừ toán học (U+2212), đồng bộ với lib/format.ts:11.
const MINUS_SIGN = "−";
const percentFormat = new Intl.NumberFormat("vi-VN", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** Phần trăm luôn kèm dấu: "+1,2" / "−0,4" / "0,0" (điểm %). */
function signedPercent(value: number): string {
  const abs = percentFormat.format(Math.abs(value));
  if (value > 0) return `+${abs}`;
  if (value < 0) return `${MINUS_SIGN}${abs}`;
  return abs;
}

const TONE_TEXT_CLASS = { profit: "text-profit", loss: "text-loss", flat: "" } as const;

export function KpiCards({ summary }: { summary: DecisionSummary }) {
  const outcome = outcomeOf(summary.totalAlphaPct);
  const winRate =
    summary.directionalCount > 0
      ? (summary.winCount / summary.directionalCount) * 100
      : null;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Card size="sm">
        <CardHeader>
          <CardDescription>Alpha tích luỹ</CardDescription>
          <CardTitle
            className={`text-2xl tabular-nums ${TONE_TEXT_CLASS[outcome]}`}
            aria-label={`Alpha tích luỹ ${signedPercent(summary.totalAlphaPct)} phần trăm — ${OUTCOME_LABEL[outcome]}`}
          >
            {signedPercent(summary.totalAlphaPct)}&nbsp;%
          </CardTitle>
          {/* Tín hiệu không chỉ dựa vào màu: dấu +/− trong số và chữ "Lãi/Lỗ" bên ngoài màu (WCAG 1.4.1). */}
          <Badge variant={outcome === "loss" ? "destructive" : "secondary"}>
            {OUTCOME_LABEL[outcome]}
          </Badge>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>Alpha trung bình mỗi quyết định</CardDescription>
          <CardTitle
            className={`text-2xl tabular-nums ${TONE_TEXT_CLASS[outcomeOf(summary.meanAlphaPct)]}`}
          >
            {signedPercent(summary.meanAlphaPct)}&nbsp;%
          </CardTitle>
          <CardDescription>trên {summary.resolvedCount} quyết định đã chốt</CardDescription>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>Tỷ lệ thắng</CardDescription>
          <CardTitle className="text-2xl tabular-nums">
            {winRate === null ? "—" : formatPercent(winRate)}
          </CardTitle>
          <CardDescription>
            {summary.directionalCount > 0
              ? `${summary.winCount}/${summary.directionalCount} quyết định đi đúng hướng`
              : "chưa có quyết định có hướng để đánh giá"}
          </CardDescription>
        </CardHeader>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardDescription>Quyết định</CardDescription>
          <CardTitle className="text-2xl tabular-nums">{summary.resolvedCount}</CardTitle>
          <CardDescription>
            {summary.pendingCount} chờ chốt · {summary.unscoredCount} REVIEW
          </CardDescription>
        </CardHeader>
      </Card>
    </div>
  );
}
