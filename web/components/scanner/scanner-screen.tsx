"use client";

// Màn M3 "Tìm cặp giao dịch": quản lý watchlist (contract §2) + chạy quét
// pipeline và xem kết quả (contract §5). Nav tới /scanner đã có sẵn trong
// sidebar nên màn này không đụng app shell.
import { useWatchlist } from "./use-watchlist";
import { WatchlistPanel } from "./watchlist-panel";
import { ScanPanel } from "./scan-panel";

export function ScannerScreen() {
  const watchlist = useWatchlist();
  return (
    <div className="space-y-4">
      <WatchlistPanel watchlist={watchlist} />
      <ScanPanel symbolsCount={watchlist.symbols.length} />
    </div>
  );
}
