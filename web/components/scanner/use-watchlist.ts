"use client";

// State màn M3 cho watchlist (contract §2): load đầu tiên → ready/error,
// add/remove qua POST/DELETE. Thao tác thêm/xóa là reversable nên không có
// confirm-dialog; lỗi hành động hiển thị ngay tại form.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiHttpError, scannerApi, toApiHttpError } from "./scanner-api";
import type { WatchlistSymbol } from "./scanner-types";

export type WatchlistStatus = "loading" | "ready" | "error";

export interface WatchlistController {
  status: WatchlistStatus;
  symbols: WatchlistSymbol[];
  generatedAt: string | null;
  warning: string | null;
  /** Lỗi tải/refetch danh sách. */
  loadError: ApiHttpError | null;
  /** Lỗi của hành động thêm/xóa gần nhất (null nếu thành công). */
  actionError: ApiHttpError | null;
  mutating: boolean;
  refresh: () => Promise<void>;
  /** No-op nếu input rỗng (panel tự báo); lỗi gọi API đặt vào actionError. */
  add: (raw: string) => Promise<void>;
  remove: (symbol: string) => Promise<void>;
}

export function useWatchlist(): WatchlistController {
  const [status, setStatus] = useState<WatchlistStatus>("loading");
  const [symbols, setSymbols] = useState<WatchlistSymbol[]>([]);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<ApiHttpError | null>(null);
  const [actionError, setActionError] = useState<ApiHttpError | null>(null);
  const [mutating, setMutating] = useState(false);
  const loadedOnce = useRef(false);

  const apply = useCallback((data: {
    generated_at: string;
    symbols: WatchlistSymbol[];
    warning?: string;
  }) => {
    setSymbols(data.symbols);
    setGeneratedAt(data.generated_at);
    setWarning(data.warning ?? null);
    setLoadError(null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const data = await scannerApi.watchlist();
      loadedOnce.current = true;
      apply(data);
      setStatus("ready");
    } catch (err) {
      const apiErr = toApiHttpError(err);
      setLoadError(apiErr);
      if (!loadedOnce.current) setStatus("error");
    }
  }, [apply]);

  // Tải ban đầu — setState chỉ chạy sau await, có cờ cancelled chống race.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await scannerApi.watchlist();
        if (cancelled) return;
        loadedOnce.current = true;
        apply(data);
        setStatus("ready");
      } catch (err) {
        if (cancelled) return;
        const apiErr = toApiHttpError(err);
        setLoadError(apiErr);
        if (!loadedOnce.current) setStatus("error");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [apply]);

  const add = useCallback(
    async (raw: string): Promise<void> => {
      const symbol = raw.trim().toUpperCase();
      if (!symbol) return;
      setMutating(true);
      setActionError(null);
      try {
        const data = await scannerApi.addSymbol(symbol);
        apply(data);
        setStatus("ready");
      } catch (err) {
        setActionError(toApiHttpError(err));
      } finally {
        setMutating(false);
      }
    },
    [apply],
  );

  const remove = useCallback(
    async (symbol: string): Promise<void> => {
      setMutating(true);
      setActionError(null);
      try {
        const data = await scannerApi.removeSymbol(symbol);
        apply(data);
      } catch (err) {
        setActionError(toApiHttpError(err));
      } finally {
        setMutating(false);
      }
    },
    [apply],
  );

  return {
    status,
    symbols,
    generatedAt,
    warning,
    loadError,
    actionError,
    mutating,
    refresh,
    add,
    remove,
  };
}
