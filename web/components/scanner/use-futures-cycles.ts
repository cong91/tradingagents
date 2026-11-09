"use client";

import { useCallback, useEffect, useState } from "react";
import { getMessages } from "@/lib/i18n/messages";
import type { AutoloopLastCycle, AutoloopModes } from "@/components/settings/autoloop-types";
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
      (body: Awaited<ReturnType<typeof scannerApi.futuresCycles>> & {
        modes?: Partial<AutoloopModes> | null;
        last_cycle?: (Partial<AutoloopLastCycle> & Record<string, unknown>) | null;
      }) => {
        const lastCycle = body.last_cycle;
        const futuresCycle = lastCycle?.modes?.auto_futures;
        const activeMode = lastCycle?.active_mode;
        const modeStatus = futuresCycle?.status;
        const futuresRunning = body.modes?.auto_futures === false && modeStatus !== "running"
          ? false
          : lastCycle?.status != null && lastCycle.status !== "running"
            ? false
            : activeMode != null
              ? activeMode === "auto_futures" && (!modeStatus || modeStatus === "running")
              : modeStatus != null
                ? modeStatus === "running"
                : lastCycle?.modes == null && (body.running_cycle ?? false);
        setCycles(body.items);
        setSummary(body.summary ?? null);
        setSymbols(body.symbols ?? []);
        setRunningCycle(Boolean(body.running_cycle && futuresRunning));
        setEnabled((body.enabled ?? false) && (body.modes?.auto_futures ?? true));
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

  return {
    cycles, summary, symbols, runningCycle, enabled, intervalMinutes,
    loading, error, refresh: load,
  };
}
