"use client";

// The brief's poll hook: one endpoint, the same four states every other panel
// on this screen uses (use-panel.ts owns them). Kept in the brief's own seam so
// the dashboard shell only wires hooks together.

import { useCallback, useMemo } from "react";

import { usePoll, type PanelState } from "@/components/overview/use-panel";
import { fetchBrief, type BriefPayload } from "./contract";

export const BRIEF_INTERVAL_MS = 30_000;

// The brief aggregates the account read as well, so it must not be polled
// faster than the portfolio snapshot it carries.
const briefTimestamp = (data: BriefPayload): string => data.generated_at;

export function useBrief(clock: number, enabled = true): {
  state: PanelState<BriefPayload>;
  refresh: () => void;
  stale: boolean;
} {
  const fetcher = useMemo(() => () => fetchBrief(), []);
  const load = useCallback(() => (enabled ? fetcher() : new Promise<BriefPayload>(() => {})), [
    enabled,
    fetcher,
  ]);
  return usePoll(
    load,
    BRIEF_INTERVAL_MS,
    BRIEF_INTERVAL_MS,
    clock,
    true,
    briefTimestamp
  );
}
