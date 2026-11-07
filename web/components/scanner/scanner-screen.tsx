"use client";

// Section "Quét & danh sách theo dõi" của trang /cycles (trước đây là màn
// /scanner): quản lý watchlist (contract §2) + chạy quét pipeline và xem kết
// quả (contract §5) — theo dõi job nằm ở active-job-card.tsx, lịch sử ở
// job-history-panel.tsx.
import { useWatchlist } from "./use-watchlist";
import { WatchlistPanel } from "./watchlist-panel";
import { ScanPanel } from "./scan-panel";
import { useT } from "@/lib/i18n";

export function ScannerScreen() {
  const watchlist = useWatchlist();
  const t = useT();
  return (
    <section aria-labelledby="scan-watchlist-title" className="space-y-4">
      <h2 id="scan-watchlist-title" className="text-xl font-semibold tracking-tight">
        {t.scanner.section.title}
      </h2>
      <WatchlistPanel watchlist={watchlist} />
      <ScanPanel symbolsCount={watchlist.symbols.length} />
    </section>
  );
}
