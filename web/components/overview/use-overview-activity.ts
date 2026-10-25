"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { autoloopApi } from "@/components/settings/autoloop-api";
import type { AutoloopPayload } from "@/components/settings/autoloop-types";
import { scannerApi } from "@/components/scanner/scanner-api";
import type { DailyJobListResponse, DailyJobStatusResponse } from "@/components/scanner/scanner-types";
import type { RunPayload } from "@/components/pipeline/types";
import { getMessages } from "@/lib/i18n/messages";
import { fetchHealth } from "./contract";

const POLL_IDLE_MS = 15_000;
const POLL_RUNNING_MS = 5_000;

export type OverviewActivityData = {
  autoloop: AutoloopPayload | null;
  daily: DailyJobListResponse | null;
  dailyJob: DailyJobStatusResponse | null;
  activeRun: RunPayload | null;
  errors: Partial<Record<"autoloop" | "daily" | "run", string>>;
};

export type OverviewActivityState =
  | { status: "loading" }
  | { status: "ready"; data: OverviewActivityData; fetchedAt: number }
  | { status: "stale"; data: OverviewActivityData; fetchedAt: number | null; message: string }
  | { status: "error"; message: string };

async function fetchActiveRun(runId: string | null): Promise<RunPayload | null> {
  if (!runId) return null;
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
  if (!response.ok) throw new Error(getMessages().overview.activity.runReadError(response.status));
  return (await response.json()) as RunPayload;
}

export function useOverviewActivity() {
  const [state, setState] = useState<OverviewActivityState>({ status: "loading" });
  const requestSequence = useRef(0);
  const dataRef = useRef<OverviewActivityData | null>(null);
  const fetchedAtRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    const sequence = ++requestSequence.current;
    const previous = dataRef.current;
    const [autoloopResult, dailyResult, healthResult] = await Promise.allSettled([
      autoloopApi.get(),
      scannerApi.dailyJobs(1),
      fetchHealth(),
    ]);
    if (sequence !== requestSequence.current) return;

    const autoloop = autoloopResult.status === "fulfilled"
      ? autoloopResult.value
      : previous?.autoloop ?? null;
    const daily = dailyResult.status === "fulfilled"
      ? dailyResult.value
      : previous?.daily ?? null;
    const dailyJobId = dailyResult.status === "fulfilled"
      ? dailyResult.value.active_job_id
      : previous?.daily?.active_job_id ?? null;
    const activeRunId = healthResult.status === "fulfilled"
      ? healthResult.value.active_run_id
      : previous?.activeRun?.run_id ?? null;
    const [dailyJobResult, activeRunResult] = await Promise.allSettled([
      dailyJobId ? scannerApi.dailyJob(dailyJobId) : Promise.resolve(null),
      activeRunId ? fetchActiveRun(activeRunId) : Promise.resolve(null),
    ]);
    if (sequence !== requestSequence.current) return;

    const errors: OverviewActivityData["errors"] = {};
    if (autoloopResult.status === "rejected") {
      errors.autoloop = autoloopResult.reason instanceof Error
        ? autoloopResult.reason.message
        : getMessages().overview.activity.autoloopReadError;
    }
    if (dailyResult.status === "rejected" || dailyJobResult.status === "rejected") {
      const reason = dailyResult.status === "rejected"
        ? dailyResult.reason
        : dailyJobResult.status === "rejected"
          ? dailyJobResult.reason
          : null;
      errors.daily = reason instanceof Error
        ? reason.message
        : getMessages().overview.activity.dailyReadError;
    }
    if (healthResult.status === "rejected" || activeRunResult.status === "rejected") {
      const reason = healthResult.status === "rejected"
        ? healthResult.reason
        : activeRunResult.status === "rejected"
          ? activeRunResult.reason
          : null;
      errors.run = reason instanceof Error
        ? reason.message
        : getMessages().overview.activity.runFallbackError;
    }

    const data: OverviewActivityData = {
      autoloop,
      daily,
      dailyJob: dailyJobResult.status === "fulfilled" ? dailyJobResult.value : previous?.dailyJob ?? null,
      activeRun: activeRunResult.status === "fulfilled" ? activeRunResult.value : previous?.activeRun ?? null,
      errors,
    };
    dataRef.current = data;
    const hasData = Boolean(autoloop || daily || data.dailyJob || data.activeRun);
    if (Object.keys(errors).length > 0 && !hasData) {
      setState({ status: "error", message: Object.values(errors).join(" ") });
      return;
    }
    if (Object.keys(errors).length > 0) {
      setState({
        status: "stale",
        data,
        fetchedAt: fetchedAtRef.current,
        message: Object.values(errors).join(" "),
      });
      return;
    }
    fetchedAtRef.current = Date.now();
    setState({ status: "ready", data, fetchedAt: fetchedAtRef.current });
  }, []);

  const currentData = state.status === "ready" || state.status === "stale" ? state.data : null;
  const running = Boolean(currentData && (
    currentData.autoloop?.running_cycle === true ||
    currentData.dailyJob?.status === "running" ||
    currentData.dailyJob?.status === "queued" ||
    currentData.activeRun?.status === "running" ||
    currentData.activeRun?.status === "queued"
  ));
  const intervalMs = running ? POLL_RUNNING_MS : POLL_IDLE_MS;

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), intervalMs);
    return () => {
      requestSequence.current += 1;
      clearInterval(id);
    };
  }, [intervalMs, refresh]);

  return { state, refresh };
}
