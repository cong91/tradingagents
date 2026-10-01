"use client";

// Form khởi chạy phiên phân tích — hành động chính duy nhất của màn M2.
// Gửi POST /api/runs (hợp đồng §3): ngày không được ở tương lai (UTC),
// asset_type bắt buộc tường minh, mode "mock" cho phép demo không cần khoá LLM.

import { useState, type FormEvent } from "react";
import { PlayIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { AssetType, RunMode, StartRunInput } from "./types";

// Server so ngày với "hôm nay UTC" (server/runs.py:116-123) — mặc định theo UTC.
function utcToday(): string {
  return new Date().toISOString().slice(0, 10);
}

const FIELD_CLASS =
  "h-10 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring";

export function RunForm({
  disabled,
  onSubmit,
}: {
  disabled: boolean;
  onSubmit: (input: StartRunInput) => void;
}) {
  const [ticker, setTicker] = useState("BTC-USD");
  const [tradeDate, setTradeDate] = useState(utcToday);
  const [assetType, setAssetType] = useState<AssetType>("crypto");
  const [mode, setMode] = useState<RunMode>("mock");
  const [checkpoint, setCheckpoint] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleaned = ticker.trim().toUpperCase();
    if (cleaned === "") {
      setLocalError("Nhập mã tài sản, ví dụ BTC-USD hoặc NVDA.");
      return;
    }
    if (tradeDate === "") {
      setLocalError("Chọn ngày phân tích.");
      return;
    }
    setLocalError(null);
    onSubmit({
      ticker: cleaned,
      trade_date: tradeDate,
      asset_type: assetType,
      mode,
      // Không tích → bỏ field, server dùng config checkpoint_enabled (hợp đồng §3).
      checkpoint: checkpoint || undefined,
    });
  };

  return (
    <form onSubmit={handleSubmit} noValidate className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Mã tài sản</span>
        <input
          className={FIELD_CLASS}
          value={ticker}
          onChange={(e) => {
            setTicker(e.target.value);
            setLocalError(null);
          }}
          placeholder="BTC-USD hoặc NVDA"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Ngày phân tích</span>
        <input
          type="date"
          className={FIELD_CLASS}
          value={tradeDate}
          max={utcToday()}
          onChange={(e) => setTradeDate(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Loại tài sản</span>
        <select
          className={FIELD_CLASS}
          value={assetType}
          onChange={(e) => setAssetType(e.target.value as AssetType)}
        >
          <option value="crypto">Crypto (tiền mã hoá)</option>
          <option value="stock">Cổ phiếu (stock)</option>
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="font-medium">Chế độ</span>
        <select
          className={FIELD_CLASS}
          value={mode}
          onChange={(e) => setMode(e.target.value as RunMode)}
        >
          <option value="mock">Mock — không gọi LLM</option>
          <option value="live">Live — gọi LLM thật</option>
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm sm:col-span-2">
        <input
          type="checkbox"
          className="size-4 accent-primary"
          checked={checkpoint}
          onChange={(e) => setCheckpoint(e.target.checked)}
        />
        Bật checkpoint (tiếp tục phiên bị gián đoạn)
      </label>
      <div className="flex items-end sm:col-span-2 lg:justify-end">
        <Button type="submit" className="min-touch" disabled={disabled}>
          <PlayIcon data-icon="inline-start" aria-hidden="true" />
          Chạy phân tích
        </Button>
      </div>
      {localError && (
        <p role="alert" className="text-sm text-destructive sm:col-span-2 lg:col-span-4">
          {localError}
        </p>
      )}
    </form>
  );
}
