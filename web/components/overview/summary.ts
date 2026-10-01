// M1 — suy ra số liệu hiệu suất từ các quyết định của decision log.
// Ngữ nghĩa đếm soi theo engine để UI không tự chế số khác hệ thống:
//   resolved  = entry !pending, rating ≠ REVIEW, alpha parse được (backtest.py:189-191)
//   pending   = tổng − resolved − unscored                                       (backtest.py:205-207)
//   hit       = alpha * direction > 0, direction theo _DIRECTION (backtest.py:89,198)
import { parseAlphaPercent, type HistoryItem } from "./contract";

// Hướng kỳ vọng của từng rating — bản sao _DIRECTION (tradingagents/backtest.py:89).
const DIRECTION: Record<string, number> = {
  Buy: 1,
  Overweight: 1,
  Hold: 0,
  Underweight: -1,
  Sell: -1,
};
const RATING_REVIEW = "REVIEW";

export type AlphaPoint = { date: string; value: number };

export type DecisionSummary = {
  /** Tổng alpha tích luỹ, đơn vị điểm % (mỗi quyết định làm tròn 0.1). */
  totalAlphaPct: number;
  /** Alpha trung bình mỗi quyết định đã chốt (điểm %). */
  meanAlphaPct: number;
  /** Số quyết định có hướng đi đúng chiều (lưu ý: Underweight/Sell thắng khi alpha âm). */
  winCount: number;
  /** Số quyết định đã chốt có thể đánh giá thắng/thua (direction ≠ 0). */
  directionalCount: number;
  resolvedCount: number;
  pendingCount: number;
  unscoredCount: number;
  /** Alpha tích luỹ theo ngày, tăng dần, mỗi ngày một điểm (điểm %). */
  series: AlphaPoint[];
};

export function summarizeHistory(items: HistoryItem[]): DecisionSummary {
  const unscoredCount = items.filter((it) => it.rating === RATING_REVIEW).length;
  const resolved = items
    .filter((it) => !it.pending && it.rating !== RATING_REVIEW)
    .map((it) => ({ item: it, alpha: parseAlphaPercent(it.alpha) }))
    .filter((x): x is { item: HistoryItem; alpha: number } => x.alpha !== null);

  const totalAlphaPct = resolved.reduce((sum, x) => sum + x.alpha, 0);
  const directional = resolved.filter((x) => (DIRECTION[x.item.rating] ?? 0) !== 0);
  const winCount = directional.filter(
    (x) => x.alpha * (DIRECTION[x.item.rating] ?? 0) > 0
  ).length;

  // Đường tích luỹ: tăng dần theo ngày; nhiều quyết định cùng ngày gộp về
  // điểm cuối ngày (lightweight-charts yêu cầu thời gian duy nhất, tăng dần).
  const runningByDay = new Map<string, number>();
  let running = 0;
  const ordered = [...resolved].sort((a, b) => a.item.date.localeCompare(b.item.date));
  for (const { item, alpha } of ordered) {
    running += alpha;
    runningByDay.set(item.date, running);
  }
  const series = [...runningByDay.entries()].map(([date, value]) => ({ date, value }));

  return {
    totalAlphaPct,
    meanAlphaPct: resolved.length > 0 ? totalAlphaPct / resolved.length : 0,
    winCount,
    directionalCount: directional.length,
    resolvedCount: resolved.length,
    pendingCount: items.length - resolved.length - unscoredCount,
    unscoredCount,
    series,
  };
}

export type DecisionOutcome = "profit" | "loss" | "flat";

export function outcomeOf(alphaPct: number): DecisionOutcome {
  if (alphaPct > 0) return "profit";
  if (alphaPct < 0) return "loss";
  return "flat";
}

export const OUTCOME_LABEL: Record<DecisionOutcome, string> = {
  profit: "Lãi",
  loss: "Lỗ",
  flat: "Hoà",
};
