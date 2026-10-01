"use client";

// Card watchlist của màn M3: thêm/xóa mã, 4 trạng thái loading/empty/error/stale
// theo contract §0.2 + §2 (stale sau 60 giây). Xóa là reversable → không confirm.
import { useEffect, useState, type FormEvent } from "react";
import { Plus, RefreshCw, TriangleAlert, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatTimestamp } from "./scanner-format";
import type { WatchlistController } from "./use-watchlist";

const STALE_AFTER_MS = 60_000;

export function WatchlistPanel({
  watchlist,
}: {
  watchlist: WatchlistController;
}) {
  const [input, setInput] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  // Đồng hồ cho badge stale — Date.now() không được gọi trong render (purity).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const isStale =
    watchlist.status === "ready" &&
    watchlist.generatedAt !== null &&
    now - Date.parse(watchlist.generatedAt) > STALE_AFTER_MS;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!input.trim()) {
      setInputError("Nhập một mã giao dịch, ví dụ BTC-USD.");
      return;
    }
    setInputError(null);
    setInput("");
    void watchlist.add(input);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Danh sách theo dõi</CardTitle>
        <CardDescription>
          Các mã sẽ được quét trong pipeline. Thêm hoặc xóa tại đây — server
          lưu và đồng bộ với engine.
        </CardDescription>
        <CardAction className="flex items-center gap-2">
          {isStale ? (
            <Badge variant="outline">Dữ liệu cũ — bấm Làm mới</Badge>
          ) : null}
          <Button
            variant="ghost"
            className="min-touch"
            onClick={() => void watchlist.refresh()}
            disabled={watchlist.status === "loading" || watchlist.mutating}
          >
            <RefreshCw data-icon="inline-start" aria-hidden />
            Làm mới
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        {watchlist.warning ? (
          <p
            role="status"
            className="flex items-start gap-2 rounded-lg bg-muted p-3 text-sm"
          >
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            {watchlist.warning}
          </p>
        ) : null}

        <form onSubmit={handleSubmit} className="space-y-1.5" noValidate>
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="scanner-symbol-input" className="sr-only">
              Mã giao dịch cần thêm
            </label>
            <input
              id="scanner-symbol-input"
              value={input}
              onChange={(event) => {
                setInput(event.target.value);
                if (inputError) setInputError(null);
              }}
              placeholder="BTC-USD"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={inputError !== null || watchlist.actionError !== null}
              className="h-11 w-56 rounded-lg border border-input bg-background px-3 text-sm uppercase placeholder:normal-case"
            />
            <Button
              type="submit"
              className="min-touch"
              disabled={watchlist.mutating || watchlist.status !== "ready"}
            >
              <Plus data-icon="inline-start" aria-hidden />
              Thêm mã
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Định dạng Yahoo: BTC-USD, SOL-USD, NVDA. Trùng mã sẽ bị từ chối.
          </p>
          {inputError ? (
            <p role="alert" className="text-sm text-destructive">
              {inputError}
            </p>
          ) : null}
          {watchlist.actionError ? (
            <p role="alert" className="text-sm text-destructive">
              {watchlist.actionError.message}
            </p>
          ) : null}
        </form>

        {watchlist.status === "loading" ? (
          <div role="status" className="space-y-2">
            <span className="sr-only">Đang tải danh sách theo dõi…</span>
            <div className="h-12 animate-pulse rounded-lg bg-muted" aria-hidden />
            <div className="h-12 animate-pulse rounded-lg bg-muted" aria-hidden />
          </div>
        ) : null}

        {watchlist.status === "error" ? (
          <div className="flex h-40 flex-col items-center justify-center gap-3 rounded-lg border border-dashed p-4 text-center">
            <p className="text-sm text-destructive">
              {watchlist.loadError?.message ?? "Không tải được danh sách theo dõi."}
            </p>
            <Button
              variant="outline"
              className="min-touch"
              onClick={() => void watchlist.refresh()}
            >
              Thử lại
            </Button>
          </div>
        ) : null}

        {watchlist.status === "ready" && watchlist.symbols.length === 0 ? (
          <div className="flex h-40 items-center justify-center rounded-lg border border-dashed p-4 text-center text-sm text-muted-foreground">
            Chưa có mã nào — nhập mã ở trên rồi bấm “Thêm mã” để bắt đầu quét.
          </div>
        ) : null}

        {watchlist.status === "ready" && watchlist.symbols.length > 0 ? (
          <ul className="divide-y rounded-lg border">
            {watchlist.symbols.map((item) => (
              <li
                key={item.symbol}
                className="flex items-center justify-between gap-3 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="font-mono text-sm font-medium">
                    {item.symbol}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Thêm lúc {formatTimestamp(item.added_at)}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="min-touch"
                  aria-label={`Xóa ${item.symbol} khỏi danh sách theo dõi`}
                  disabled={watchlist.mutating}
                  onClick={() => void watchlist.remove(item.symbol)}
                >
                  <X aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}
