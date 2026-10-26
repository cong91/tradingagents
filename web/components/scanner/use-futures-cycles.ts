"use client";

import { useCallback, useEffect, useState } from "react";
import { scannerApi } from "./scanner-api";
import type { FuturesCycle, FuturesCycleSummary, FuturesCycleSymbolRollup } from "./scanner-types";

const POLL_INTERVAL_MS = 10_000;

export interface FuturesCycleState {
  cycles: FuturesCycle[];
  summary: FuturesCycleSummary | null;
  symbols: FuturesCycleSymbolRollup[];
  runningCycle: boolean;
  enabled: boolean;
  intervalMinutes: number | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useFuturesCycles(): FuturesCycleState {
  const [cycles, setCycles] = useState<FuturesCycle[]>([]);
  const [summary, setSummary] = useState<FuturesCycleSummary | null>(null);
  const [symbols, setSymbols] = useState<FuturesCycleSymbolRollup[]>([]);
  const [runningCycle, setRunningCycle] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    scannerApi.futuresCycles().then(
      (body) => {
        setCycles(body.items);
        setSummary(body.summary ?? null);
        setSymbols(body.symbols ?? []);
        setRunningCycle(body.running_cycle ?? false);
        setEnabled(body.enabled ?? false);
        setIntervalMinutes(body.interval_minutes ?? null);
        setError(null);
      },
      (err: unknown) => {
        setError(err instanceof Error ? err.message : "Không tải được trạng thái futures");
      },
    ).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [load]);

  return {
    cycles, summary, symbols, runningCycle, enabled, intervalMinutes,
    loading, error, refresh: load,
  };
}
