"use client";

import { useCallback, useEffect, useState } from "react";
import { getMessages } from "@/lib/i18n/messages";
import { scannerApi } from "./scanner-api";
import type { FuturesCycle } from "./scanner-types";

const POLL_INTERVAL_MS = 10_000;

export interface FuturesCycleState {
  cycles: FuturesCycle[];
  runningCycle: boolean;
  enabled: boolean;
  intervalMinutes: number | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useFuturesCycles(): FuturesCycleState {
  const [cycles, setCycles] = useState<FuturesCycle[]>([]);
  const [runningCycle, setRunningCycle] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    scannerApi.futuresCycles().then(
      (body) => {
        setCycles(body.items);
        setRunningCycle(body.running_cycle ?? false);
        setEnabled(body.enabled ?? false);
        setIntervalMinutes(body.interval_minutes ?? null);
        setError(null);
      },
      (err: unknown) => {
        setError(err instanceof Error ? err.message : getMessages().scanner.futures.loadFailed);
      },
    ).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [load]);

  return { cycles, runningCycle, enabled, intervalMinutes, loading, error, refresh: load };
}
