"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  LoaderCircleIcon,
  OctagonXIcon,
  PlayIcon,
  TimerIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { cn } from "cn";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { autoloopApi, toAutoloopError } from "./autoloop-api";
import {
  AUTOLOOP_DEFAULT_INTERVAL,
  AUTOLOOP_MODE_ROWS,
  AUTOLOOP_MAX_INTERVAL,
  AUTOLOOP_MIN_INTERVAL,
  type AutoloopLastCycle,
  type AutoloopModeCycle,
  type AutoloopModeKey,
  type AutoloopPayload,
} from "./autoloop-types";
import { formatDateTimeVi } from "./format-time";
import { useT, type Messages } from "@/lib/i18n";

// Poll chậm khi rảnh, nhanh khi một chu kỳ đang chạy.
const POLL_IDLE_MS = 30_000;
const POLL_RUNNING_MS = 5_000;

type Feedback = { role: "status" | "alert"; text: string };
type HaltState = "loading" | "unknown" | "active" | "inactive";

type AutoloopCardProps = {
  hideHalt?: boolean;
  onDirtyChange?: (dirty: boolean) => void;
};

/** Khoảng cách phút hợp lệ: số nguyên trong [15, 1440] — cùng ràng buộc server. */
function parseInterval(raw: string): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const value = Number(raw.trim());
  if (!Number.isInteger(value)) return null;
  return AUTOLOOP_MIN_INTERVAL <= value && value <= AUTOLOOP_MAX_INTERVAL ? value : null;
}

/** Tóm tắt một dòng của chu kỳ gần nhất (thời gian + trạng thái + ứng viên). */
function lastCycleSummary(t: Messages, cycle: AutoloopLastCycle): string {
  const c = t.settings.autoloop;
  const when = formatDateTimeVi(cycle.finished_at ?? cycle.started_at);
  if (cycle.status === "skipped_busy") {
    return `${when} · ${c.skipped(cycle.reason)}`;
  }
  if (cycle.status === "failed") {
    return `${when} · ${c.failed(cycle.error)}`;
  }
  if (cycle.status === "running" || cycle.status === "timeout") {
    const telemetry = cycle.telemetry;
    const ticker = telemetry?.active_ticker ?? telemetry?.current_ticker;
    const stage = telemetry?.active_stage ?? telemetry?.last_stage;
    const progress = [
      ticker ? c.ticker(ticker) : null,
      telemetry?.active_node ? c.agent(telemetry.active_node) : null,
      stage ? c.stage(Boolean(telemetry?.active_stage), stage) : null,
    ].filter(Boolean).join(" · ");
    const status = cycle.status === "running" ? c.running : c.timeout(cycle.error);
    return `${when} · ${status}${progress ? ` · ${progress}` : ""}`;
  }
  const executed = cycle.results.filter((result) => result.executed).length;
  const candidates =
    cycle.candidates.length > 0 ? c.candidates(cycle.candidates.join(", ")) : c.noCandidates;
  return `${when} · ${c.completed} · ${candidates} · ${c.executed(executed, cycle.results.length)}`;
}

function modeCycleSummary(t: Messages, cycle: AutoloopModeCycle | null | undefined): string {
  const c = t.settings.autoloop;
  if (!cycle?.status) return c.modeCycleUnavailable;
  const status = c.modeCycleStatus[cycle.status] ?? cycle.status;
  const when = cycle.finished_at ?? cycle.started_at;
  return [status, when ? formatDateTimeVi(when) : null, cycle.error].filter(Boolean).join(" · ");
}

function ModeBadge({ open }: { open: boolean }) {
  const t = useT();
  return open ? (
    <Badge className="border-emerald-600 bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400">
      {t.settings.autoloop.modeEnabled}
    </Badge>
  ) : (
    <Badge variant="outline">{t.settings.autoloop.modeDisabled}</Badge>
  );
}

export function AutoloopHaltControl() {
  const t = useT();
  const c = t.settings.autoloop;
  const [haltPending, setHaltPending] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [verifyingReset, setVerifyingReset] = useState(false);
  const [haltState, setHaltState] = useState<HaltState>("loading");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const haltStateRef = useRef<HaltState>("loading");
  const haltPendingRef = useRef(false);
  const verifyingResetRef = useRef(false);
  const healthSequenceRef = useRef(0);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const loadHealth = useCallback(async () => {
    const sequence = ++healthSequenceRef.current;
    setConfirmReset(false);
    if (haltStateRef.current !== "loading") {
      haltStateRef.current = "loading";
      setHaltState("loading");
    }
    let next: HaltState = "unknown";
    try {
      const halted = await autoloopApi.getHaltState();
      if (halted !== null) next = halted ? "active" : "inactive";
    } catch {
      // An unavailable health response is never proof that trading is unhalted.
    }
    if (!aliveRef.current || sequence !== healthSequenceRef.current) return null;
    haltStateRef.current = next;
    setHaltState(next);
    return next;
  }, []);

  useEffect(() => {
    const initialId = setTimeout(() => void loadHealth(), 0);
    const pollId = setInterval(() => {
      if (!haltPendingRef.current && !verifyingResetRef.current) void loadHealth();
    }, POLL_IDLE_MS);
    return () => {
      clearTimeout(initialId);
      clearInterval(pollId);
      healthSequenceRef.current += 1;
    };
  }, [loadHealth]);

  const changeHalt = useCallback(async (reset: boolean) => {
    if (haltPendingRef.current || (reset && haltStateRef.current !== "active")) return;
    haltPendingRef.current = true;
    setConfirmReset(false);
    healthSequenceRef.current += 1;
    haltStateRef.current = "loading";
    setHaltState("loading");
    setHaltPending(true);
    setFeedback(null);
    try {
      await (reset ? autoloopApi.resetHalt() : autoloopApi.halt());
    } catch (err) {
      if (aliveRef.current) setFeedback({ role: "alert", text: toAutoloopError(err).message });
    } finally {
      haltPendingRef.current = false;
      if (aliveRef.current) {
        setHaltPending(false);
        void loadHealth();
      }
    }
  }, [loadHealth]);

  const confirmClearHalt = useCallback(async () => {
    if (verifyingResetRef.current || haltPendingRef.current || haltStateRef.current !== "active") return;
    verifyingResetRef.current = true;
    setVerifyingReset(true);
    setConfirmReset(false);
    try {
      const state = await loadHealth();
      if (state === "active" && !haltPendingRef.current) await changeHalt(true);
    } finally {
      verifyingResetRef.current = false;
      if (aliveRef.current) setVerifyingReset(false);
    }
  }, [changeHalt, loadHealth]);

  return (
    <div className="rounded-lg border-2 border-destructive/60 bg-destructive/5 p-3">
      <p className="flex items-center gap-2 text-sm font-semibold text-destructive">
        <OctagonXIcon className="size-4" aria-hidden="true" />
        {c.stopTitle}
      </p>
      <p className="mt-1 text-sm text-muted-foreground">{c.stopDescription}</p>
      <p role="status" className="mt-2 text-sm">
        {haltState === "loading" ? c.haltLoading :
          haltState === "unknown" ? c.haltUnknown :
            haltState === "active" ? c.haltActive : c.haltInactive}
      </p>
      {haltState === "unknown" ? (
        <Button variant="outline" className="mt-2 min-touch" onClick={() => void loadHealth()} disabled={haltPending}>
          {t.common.retry}
        </Button>
      ) : null}
      <div className="mt-3">
        {haltState === "active" ? (
          <Button variant="outline" className="min-touch" onClick={() => setConfirmReset(true)} disabled={haltPending || verifyingReset}>
            {c.resetHalt}
          </Button>
        ) : (
          <Button variant="destructive" className="min-touch" onClick={() => void changeHalt(false)} disabled={haltPending}>
            {haltPending ? <LoaderCircleIcon className="animate-spin" aria-hidden="true" /> : <OctagonXIcon aria-hidden="true" />}
            {c.haltAll}
          </Button>
        )}
      </div>
      <Dialog open={confirmReset && haltState === "active"} onOpenChange={setConfirmReset}>
        {confirmReset && haltState === "active" ? (
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{c.clearHaltConfirmTitle}</DialogTitle>
              <DialogDescription>{c.clearHaltConfirmDescription}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" className="min-touch" onClick={() => setConfirmReset(false)}>
                {t.common.cancel}
              </Button>
              <Button className="min-touch" onClick={() => void confirmClearHalt()} disabled={haltPending || verifyingReset}>
                {c.clearHaltConfirmAction}
              </Button>
            </DialogFooter>
          </DialogContent>
        ) : null}
      </Dialog>
      {feedback ? (
        <p role={feedback.role} className={cn("mt-2 text-sm", feedback.role === "alert" ? "text-destructive" : "text-profit")}>
          {feedback.text}
        </p>
      ) : null}
    </div>
  );
}

export function AutoloopCard({ hideHalt = false, onDirtyChange }: AutoloopCardProps) {
  const t = useT();
  const c = t.settings.autoloop;
  const [phase, setPhase] = useState<"loading" | "error" | "ready" | "stale">("loading");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [payload, setPayload] = useState<AutoloopPayload | null>(null);
  // Bản nháp khoảng cách phút: poll không ghi đè khi user đang sửa (§0.2 stale rule).
  const [intervalDraft, setIntervalDraft] = useState(String(AUTOLOOP_DEFAULT_INTERVAL));
  const [intervalError, setIntervalError] = useState<string | null>(null);
  const [gateWarning, setGateWarning] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [savePending, setSavePending] = useState(false);
  const [runPending, setRunPending] = useState(false);

  const hasDataRef = useRef(false);
  const draftDirtyRef = useRef(false);
  const intervalDraftRef = useRef(String(AUTOLOOP_DEFAULT_INTERVAL));
  const savedIntervalRef = useRef<string | null>(null);
  const loadSequenceRef = useRef(0);
  const savePendingRef = useRef(false);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const setDirty = useCallback((dirty: boolean) => {
    if (draftDirtyRef.current === dirty) return;
    draftDirtyRef.current = dirty;
    onDirtyChange?.(dirty);
  }, [onDirtyChange]);

  const applyPayload = useCallback((data: AutoloopPayload) => {
    savedIntervalRef.current = String(data.interval_minutes);
    setPayload(data);
    if (!draftDirtyRef.current) {
      intervalDraftRef.current = savedIntervalRef.current;
      setIntervalDraft(savedIntervalRef.current);
    }
  }, []);

  const load = useCallback(async () => {
    if (savePendingRef.current) return;
    const sequence = ++loadSequenceRef.current;
    try {
      const data = await autoloopApi.get();
      if (!aliveRef.current || sequence !== loadSequenceRef.current) return;
      if (draftDirtyRef.current && intervalDraftRef.current === String(data.interval_minutes)) {
        setDirty(false);
      }
      applyPayload(data);
      hasDataRef.current = true;
      setPhase("ready");
    } catch (err) {
      if (!aliveRef.current || sequence !== loadSequenceRef.current) return;
      setErrorMessage(toAutoloopError(err).message);
      setFeedback(null);
      setPhase(hasDataRef.current ? "stale" : "error");
    }
  }, [applyPayload, setDirty]);

  // Nạp lần đầu trong callback (không setState đồng bộ trong effect body),
  // rồi poll theo trạng thái running_cycle.
  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  const runningCycle = payload?.running_cycle ?? false;
  useEffect(() => {
    if (phase !== "ready" && phase !== "stale") return;
    const pollId = setInterval(() => void load(), phase === "ready" && runningCycle ? POLL_RUNNING_MS : POLL_IDLE_MS);
    return () => clearInterval(pollId);
  }, [load, phase, runningCycle]);

  const save = useCallback(
    async (enabled: boolean) => {
      const interval = parseInterval(intervalDraft);
      if (interval === null) {
        setIntervalError(c.intervalError(AUTOLOOP_MIN_INTERVAL, AUTOLOOP_MAX_INTERVAL));
        return;
      }
      if (savePendingRef.current || phase !== "ready" || (enabled !== true && payload?.modes.any_auto_enabled)) return;
      savePendingRef.current = true;
      loadSequenceRef.current += 1;
      setIntervalError(null);
      setGateWarning(null);
      setSavePending(true);
      try {
        const data = await autoloopApi.set(enabled, interval);
        setDirty(false);
        applyPayload(data);
        setFeedback({ role: "status", text: c.intervalSaved });
      } catch (err) {
        const error = toAutoloopError(err);
        if (error.code === "mode_closed") {
          setGateWarning(c.modeClosedWarning);
        } else {
          setFeedback({ role: "alert", text: error.message });
        }
      } finally {
        savePendingRef.current = false;
        if (aliveRef.current) setSavePending(false);
      }
    },
    [applyPayload, intervalDraft, c, payload, phase, setDirty],
  );

  const runNow = useCallback(async () => {
    if (phase !== "ready" || !payload?.modes.any_auto_enabled || payload.running_cycle || runPending) return;
    setRunPending(true);
    setFeedback(null);
    try {
      const res = await autoloopApi.runNow();
      setFeedback({
        role: "status",
        text: res.status === "started" ? c.runStarted : c.runStatus[res.status] ?? c.serverResponse(res.status),
      });
      // GET lại sau 1s để running_cycle kịp phản ánh worker vừa được spawn.
      setTimeout(() => {
        if (aliveRef.current) void load();
      }, 1_000);
    } catch (err) {
      setFeedback({ role: "alert", text: toAutoloopError(err).message });
    } finally {
      setRunPending(false);
    }
  }, [load, c, phase, payload, runPending]);

  const modes = payload?.modes;
  const lastCycle = payload?.last_cycle;
  const anyModeEnabled = Boolean(modes?.auto_spot || modes?.auto_futures);
  const enabled = payload?.enabled === true;
  const schedulerUnknown = Boolean(payload && !enabled && anyModeEnabled);
  const cycleBusy = (phase === "ready" && runningCycle) || runPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TimerIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.schedulerDescription}</CardDescription>
        {phase === "ready" && payload ? (
          <CardAction>
            {anyModeEnabled ? <Badge variant="default">{c.enabled}</Badge> : <Badge variant="outline">{c.disabled}</Badge>}
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-4">
        {phase === "loading" && !payload ? (
          <p role="status" className="text-sm text-muted-foreground">{c.loading}</p>
        ) : null}
        {phase === "error" && !payload ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
            <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
            <span>{errorMessage ?? c.loadFailed}</span>
            <Button variant="outline" className="min-touch" onClick={() => void load()}>
              {t.common.retry}
            </Button>
          </div>
        ) : null}
        {phase === "stale" ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive">
            <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
            <span>{c.statusStale}</span>
            <Button variant="outline" className="min-touch" onClick={() => void load()}>
              {t.common.retry}
            </Button>
          </div>
        ) : null}
        {payload && modes ? (
          <>
        {phase === "ready" ? <section aria-label={c.modesAria}>
          <ul className="space-y-2">
            {AUTOLOOP_MODE_ROWS.map((key: AutoloopModeKey) => {
              const modeCycle = lastCycle?.modes?.[key];
              const active = runningCycle && lastCycle?.status === "running" && (
                (lastCycle.active_mode === key && (!modeCycle?.status || modeCycle.status === "running")) ||
                (lastCycle.active_mode == null && modeCycle?.status === "running")
              );
              return (
                <li
                  key={key}
                  className="flex flex-wrap items-center justify-between gap-2 border-t py-2 first:border-t-0 first:pt-0"
                >
                  <span className="min-w-0 text-sm">
                    {c.modeRows[key]}
                    <span className="block text-xs text-muted-foreground">
                      {c.modeLastCycle(modeCycleSummary(t, modeCycle))}
                    </span>
                  </span>
                  <span className="flex flex-wrap items-center gap-2">
                    {active ? <Badge variant="secondary">{c.activeMode}</Badge> : null}
                    <ModeBadge open={modes[key]} />
                  </span>
                </li>
              );
            })}
          </ul>
          <p
            className={cn(
              "mt-2 text-sm font-medium",
              modes.any_auto_enabled ? "text-profit" : "text-amber-700 dark:text-amber-400",
            )}
          >
            {modes.any_auto_enabled ? c.modeAvailable : c.noModesEnabled}
          </p>
        </section> : null}

        <div className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-lg border p-3">
          {phase === "ready" ? (
            <span className="min-w-0 flex-1 text-sm text-muted-foreground">
              {enabled ? c.schedulerEnabled : c.schedulerInactive}
            </span>
          ) : null}
          {phase === "ready" && schedulerUnknown ? (
            <Button variant="outline" className="min-touch" onClick={() => void load()}>
              {t.common.retry}
            </Button>
          ) : null}
          <span className="flex flex-wrap items-center gap-2">
            <label htmlFor="autoloop-interval" className="text-sm text-muted-foreground">
              {c.intervalLabel}
            </label>
            <input
              id="autoloop-interval"
              type="text"
              inputMode="numeric"
              value={intervalDraft}
              onChange={(event) => {
                const nextDraft = event.target.value;
                intervalDraftRef.current = nextDraft;
                setDirty(phase !== "ready" || nextDraft !== savedIntervalRef.current);
                setIntervalDraft(nextDraft);
                setIntervalError(null);
              }}
              disabled={savePending}
              aria-invalid={intervalError ? true : undefined}
              aria-describedby={intervalError ? "autoloop-interval-error" : undefined}
              className="h-9 w-24 rounded-lg border border-input bg-background px-3 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-[invalid=true]:border-destructive"
            />
            <Button
              variant="outline"
              size="sm"
              className="min-touch"
              onClick={() => void save(enabled)}
              disabled={savePending || phase !== "ready" || schedulerUnknown}
            >
              {c.apply}
            </Button>
          </span>
        </div>
        {intervalError ? (
          <p id="autoloop-interval-error" role="alert" className="text-xs text-destructive">
            {intervalError}
          </p>
        ) : null}
        {gateWarning ? (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/50 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100"
          >
            <TriangleAlertIcon className="size-4 shrink-0" aria-hidden="true" />
            {gateWarning}
          </div>
        ) : null}

        <div className="flex flex-wrap items-center gap-3 rounded-lg border p-3">
          <Button className="min-touch" onClick={() => void runNow()} disabled={phase !== "ready" || cycleBusy || !anyModeEnabled}>
            {cycleBusy ? (
              <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
            ) : (
              <PlayIcon aria-hidden="true" />
            )}
            {cycleBusy ? c.cycleRunning : c.runNow}
          </Button>
          <span className="min-w-0 flex-1 text-sm text-muted-foreground">
            {c.runNowHint}
          </span>
        </div>

        {phase === "ready" && lastCycle ? (
          <p
            className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground"
            title={lastCycleSummary(t, lastCycle)}
          >
            <span className="font-medium text-foreground">{c.lastCycle}</span>
            {lastCycleSummary(t, lastCycle)}
          </p>
        ) : null}
          </>
        ) : null}

        {!hideHalt ? <AutoloopHaltControl /> : null}

        {feedback && phase === "ready" ? (
          <p
            role={feedback.role}
            className={cn("text-sm", feedback.role === "alert" ? "text-destructive" : "text-profit")}
          >
            {feedback.text}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
