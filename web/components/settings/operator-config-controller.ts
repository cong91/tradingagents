"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { operatorConfigApi, toOperatorError } from "./operator-config-api";
import {
  changedKeys,
  draftFrom,
  isOperatorGateKey,
  operatorSection,
  parseOperatorNumber,
  type OperatorConfigPayload,
  type OperatorDraft,
  type OperatorSection,
  type OperatorValue,
} from "./operator-config-types";

type Feedback = { role: "status" | "alert"; text: string };

type DraftChange = { draft: OperatorDraft; changed: string[]; dirtyCount: number };

const REFRESH_INTERVAL_MS = 30_000;
const POSITIVE_RISK_KEYS = new Set([
  "risk_derivatives_break_even_r",
  "risk_derivatives_lock_profit_r",
  "risk_derivatives_trail_start_r",
  "risk_derivatives_trail_distance_r",
  "risk_derivatives_time_stop_min_r",
]);
const ORDERED_RISK_KEYS = [
  "risk_derivatives_break_even_r",
  "risk_derivatives_lock_profit_r",
  "risk_derivatives_trail_start_r",
] as const;

function sameServerConfig(left: OperatorConfigPayload, right: OperatorConfigPayload): boolean {
  const pick = (data: OperatorConfigPayload) => ({
    settings: [...data.settings].sort((a, b) => a.key.localeCompare(b.key)),
    allowlist: [...data.allowlist].sort(),
    gates: data.gates,
    effective_exec_mode: data.effective_exec_mode,
  });
  return JSON.stringify(pick(left)) === JSON.stringify(pick(right));
}

function changedServerKeys(left: OperatorConfigPayload, right: OperatorConfigPayload): Set<string> {
  const oldRows = new Map(left.settings.map((row) => [row.key, row]));
  const newRows = new Map(right.settings.map((row) => [row.key, row]));
  const keys = new Set([...oldRows.keys(), ...newRows.keys()]);
  for (const key of keys) {
    const oldRow = oldRows.get(key);
    const newRow = newRows.get(key);
    if (
      !oldRow || !newRow || oldRow.type !== newRow.type || oldRow.gate !== newRow.gate ||
      oldRow.value !== newRow.value || oldRow.default !== newRow.default ||
      oldRow.override !== newRow.override || oldRow.stored_value !== newRow.stored_value
    ) continue;
    keys.delete(key);
  }
  return keys;
}

function hasChangedGate(left: OperatorConfigPayload, right: OperatorConfigPayload): boolean {
  return [...changedServerKeys(left, right)].some(isOperatorGateKey);
}

function clearGateAuthorization(
  setConfirmGate: (value: boolean) => void,
  setReason: (value: string) => void,
  confirmGateRef: { current: boolean },
  reasonRef: { current: string }
): void {
  confirmGateRef.current = false;
  reasonRef.current = "";
  setConfirmGate(false);
  setReason("");
}

export function useOperatorConfig(options?: { onDraftChange?: (state: DraftChange) => void }) {
  const t = useT();
  const [payload, setPayload] = useState<OperatorConfigPayload | null>(null);
  const [draft, setDraft] = useState<OperatorDraft>({});
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [errorMessage, setErrorMessage] = useState("");
  const [feedback, setFeedback] = useState<Partial<Record<OperatorSection, Feedback>>>({});
  const [saving, setSaving] = useState(false);
  const [confirmGate, setConfirmGate] = useState(false);
  const [reason, setReason] = useState("");
  const confirmGateRef = useRef(false);
  const reasonRef = useRef("");
  const [serverAhead, setServerAhead] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [refreshingState, setRefreshingState] = useState(false);
  const inFlight = useRef(false);
  const refreshing = useRef(false);
  const refreshFailedRef = useRef(false);
  const serverAheadRef = useRef(false);
  const queuedRefresh = useRef(false);
  const payloadRef = useRef<OperatorConfigPayload | null>(null);
  const draftRef = useRef<OperatorDraft>({});
  const pendingRef = useRef<OperatorConfigPayload | null>(null);
  const loadRef = useRef<() => Promise<void>>(async () => {});

  const applySnapshot = useCallback((next: OperatorConfigPayload, values: OperatorDraft) => {
    payloadRef.current = next;
    draftRef.current = values;
    pendingRef.current = null;
    serverAheadRef.current = false;
    refreshFailedRef.current = false;
    setPayload(next);
    setDraft(values);
    setServerAhead(false);
    setRefreshFailed(false);
    setPhase("ready");
    setErrorMessage("");
  }, []);

  const load = useCallback(async () => {
    if (inFlight.current || refreshing.current) {
      queuedRefresh.current = true;
      return;
    }
    refreshing.current = true;
    setRefreshingState(true);
    try {
      const next = await operatorConfigApi.get();
      const previous = payloadRef.current;
      const currentDraft = draftRef.current;
      if (serverAheadRef.current && pendingRef.current) {
        if (!sameServerConfig(pendingRef.current, next)) {
          if (hasChangedGate(pendingRef.current, next)) {
            clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
          }
          pendingRef.current = next;
        }
        refreshFailedRef.current = false;
        setRefreshFailed(false);
        setServerAhead(true);
        return;
      }
      if (previous && changedKeys(previous, currentDraft).length > 0 && !sameServerConfig(previous, next)) {
        pendingRef.current = next;
        serverAheadRef.current = true;
        refreshFailedRef.current = false;
        setServerAhead(true);
        setRefreshFailed(false);
        if (hasChangedGate(previous, next)) {
          clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
        }
      } else if (previous && changedKeys(previous, currentDraft).length > 0) {
        pendingRef.current = null;
        serverAheadRef.current = false;
        refreshFailedRef.current = false;
        setServerAhead(false);
        setRefreshFailed(false);
      } else {
        applySnapshot(next, draftFrom(next));
      }
    } catch (err) {
      if (!payloadRef.current) {
        setPhase("error");
        setErrorMessage(toOperatorError(err).message);
      } else {
        refreshFailedRef.current = true;
        setRefreshFailed(true);
      }
    } finally {
      refreshing.current = false;
      setRefreshingState(false);
      if (queuedRefresh.current && !inFlight.current) {
        queuedRefresh.current = false;
        void loadRef.current();
      }
    }
  }, [applySnapshot]);
  loadRef.current = load;

  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    const pollId = setInterval(() => void load(), REFRESH_INTERVAL_MS);
    const source = typeof EventSource === "undefined" ? null : new EventSource("/api/settings/events");
    const onChange = () => void load();
    const onWake = () => {
      if (document.visibilityState === "visible") void load();
    };
    source?.addEventListener("changed", onChange);
    source?.addEventListener("reset", onChange);
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onWake);
    return () => {
      clearTimeout(initialId);
      clearInterval(pollId);
      source?.close();
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onWake);
    };
  }, [load]);

  const changed = useMemo(
    () => (payload ? changedKeys(payload, draft) : []),
    [payload, draft]
  );
  const onDraftChange = options?.onDraftChange;
  useEffect(() => {
    onDraftChange?.({ draft, changed, dirtyCount: changed.length });
  }, [draft, changed, onDraftChange]);

  const sectionChanges = useMemo(() => {
    const result: Record<OperatorSection, string[]> = { modes: [], risk: [] };
    for (const row of payload?.settings ?? []) {
      if (changed.includes(row.key)) result[operatorSection(row)].push(row.key);
    }
    return result;
  }, [payload, changed]);
  const invalidMessages = useMemo(() => {
    const invalid: Record<string, string> = {};
    const changedSet = new Set(changed);
    const values = new Map<string, number>();
    for (const row of payload?.settings ?? []) {
      const value = parseOperatorNumber(draft[row.key]);
      if (value !== null) values.set(row.key, value);
      if (!changedSet.has(row.key) || row.type !== "float") continue;
      if (value === null) invalid[row.key] = t.settings.operator.invalidNumber;
      else if (POSITIVE_RISK_KEYS.has(row.key) && value <= 0) {
        invalid[row.key] = t.settings.operator.invalidPositiveNumber;
      }
    }
    const [breakEven, lockProfit, trailStart] = ORDERED_RISK_KEYS;
    const breakEvenValue = values.get(breakEven);
    const lockProfitValue = values.get(lockProfit);
    const trailStartValue = values.get(trailStart);
    const markOrderError = (keys: readonly string[]) => {
      for (const key of keys) {
        if (changedSet.has(key)) invalid[key] = t.settings.operator.invalidRiskOrder;
      }
    };
    if (breakEvenValue !== undefined && lockProfitValue !== undefined && breakEvenValue >= lockProfitValue) {
      markOrderError([breakEven, lockProfit]);
    }
    if (lockProfitValue !== undefined && trailStartValue !== undefined && lockProfitValue >= trailStartValue) {
      markOrderError([lockProfit, trailStart]);
    }
    return invalid;
  }, [payload, draft, changed, t]);
  const invalidKeys = useMemo(() => Object.keys(invalidMessages), [invalidMessages]);

  const reviewLatest = useCallback(() => {
    const previous = payloadRef.current;
    const next = pendingRef.current;
    if (!previous || !next || inFlight.current || refreshing.current || refreshFailedRef.current) return;
    const current = draftRef.current;
    const dirtyKeys = changedKeys(previous, current);
    const nextRows = new Map(next.settings.map((row) => [row.key, row]));
    if (dirtyKeys.some((key) => {
      const oldRow = previous.settings.find((row) => row.key === key);
      const newRow = nextRows.get(key);
      return !oldRow || !newRow || oldRow.type !== newRow.type || oldRow.gate !== newRow.gate;
    })) return;
    const merged = draftFrom(next);
    for (const key of dirtyKeys) merged[key] = current[key];
    applySnapshot(next, merged);
    clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
  }, [applySnapshot]);


  const setValue = useCallback((key: string, value: OperatorValue) => {
    if (inFlight.current) return;
    if (!payload) return;
    const row = payload.settings.find((item) => item.key === key);
    if (!row) return;
    const values = { ...draftRef.current, [key]: value };
    draftRef.current = values;
    setDraft(values);
    setFeedback((current) => ({ ...current, [operatorSection(row)]: undefined }));
    if (!refreshFailedRef.current && pendingRef.current && changedKeys(payload, values).length === 0) {
      applySnapshot(pendingRef.current, draftFrom(pendingRef.current));
      clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
    }
    if (row.gate) {
      clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
    }
  }, [payload, applySnapshot]);

  const setConfirmation = useCallback((value: boolean) => {
    if (!inFlight.current) {
      confirmGateRef.current = value;
      setConfirmGate(value);
    }
  }, []);
  const setGateReason = useCallback((value: string) => {
    if (!inFlight.current) {
      reasonRef.current = value;
      setReason(value);
    }
  }, []);

  const undo = useCallback((section: OperatorSection) => {
    if (!payload || inFlight.current) return;
    const saved = draftFrom(payload);
    const next = { ...draftRef.current };
    for (const row of payload.settings) {
      if (operatorSection(row) === section) next[row.key] = saved[row.key];
    }
    draftRef.current = next;
    setDraft(next);
    if (!refreshFailedRef.current && pendingRef.current && changedKeys(payload, next).length === 0) {
      applySnapshot(pendingRef.current, draftFrom(pendingRef.current));
    }
    clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
    setFeedback((current) => ({ ...current, [section]: undefined }));
  }, [payload, applySnapshot]);

  const save = useCallback(async (section: OperatorSection) => {
    if (!payload || inFlight.current || refreshing.current || serverAheadRef.current || refreshFailedRef.current) return;
    const keys = sectionChanges[section];
    if (keys.length === 0 || keys.some((key) => invalidKeys.includes(key))) return;
    if (section === "modes" && (!confirmGateRef.current || !reasonRef.current.trim())) return;

    const settings: Record<string, number | boolean> = {};
    for (const row of payload.settings) {
      if (!keys.includes(row.key)) continue;
      if (row.type === "bool") {
        settings[row.key] = draft[row.key] === true;
      } else {
        const value = parseOperatorNumber(draft[row.key]);
        if (value === null) return;
        settings[row.key] = value;
      }
    }

    inFlight.current = true;
    setSaving(true);
    setFeedback((current) => ({ ...current, [section]: undefined }));
    try {
      const next = await operatorConfigApi.update(settings, {
        confirm: section === "modes" ? confirmGateRef.current : undefined,
        reason: section === "modes" ? reasonRef.current.trim() : undefined,
      });
      const concurrentKeys = [...changedServerKeys(payload, next)].filter((key) => !keys.includes(key));
      const concurrentGateChange = concurrentKeys.some(isOperatorGateKey);
      const fresh = draftFrom(next);
      for (const key of changedKeys(payload, draftRef.current)) {
        if (!keys.includes(key)) fresh[key] = draftRef.current[key];
      }
      applySnapshot(next, fresh);
      if (concurrentKeys.length > 0 && changedKeys(next, fresh).length > 0) {
        pendingRef.current = next;
        serverAheadRef.current = true;
        setServerAhead(true);
      }
      if (concurrentGateChange) {
        clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
      }
      if (section === "modes") {
        clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
      }
      setFeedback((current) => ({
        ...current,
        [section]: { role: "status", text: t.settings.operator.saved(keys.length) },
      }));
    } catch (err) {
      setFeedback((current) => ({
        ...current,
        [section]: { role: "alert", text: toOperatorError(err).message },
      }));
    } finally {
      inFlight.current = false;
      setSaving(false);
      if (queuedRefresh.current) {
        queuedRefresh.current = false;
        void loadRef.current();
      }
    }
  }, [payload, sectionChanges, invalidKeys, draft, t, applySnapshot]);

  const reset = useCallback(async (key: string) => {
    if (!payload || inFlight.current || refreshing.current || serverAheadRef.current || refreshFailedRef.current || changedKeys(payload, draftRef.current).length > 0) return;
    const row = payload.settings.find((item) => item.key === key);
    if (!row?.override || row.gate || isOperatorGateKey(key)) return;
    const section = operatorSection(row);
    inFlight.current = true;
    setSaving(true);
    setFeedback((current) => ({ ...current, [section]: undefined }));
    try {
      const next = await operatorConfigApi.reset(key);
      applySnapshot(next, draftFrom(next));
      clearGateAuthorization(setConfirmGate, setReason, confirmGateRef, reasonRef);
      setFeedback((current) => ({
        ...current,
        [section]: { role: "status", text: t.settings.operator.resetDone(key) },
      }));
    } catch (err) {
      setFeedback((current) => ({
        ...current,
        [section]: { role: "alert", text: toOperatorError(err).message },
      }));
    } finally {
      inFlight.current = false;
      setSaving(false);
      if (queuedRefresh.current) {
        queuedRefresh.current = false;
        void loadRef.current();
      }
    }
  }, [payload, t, applySnapshot]);

  return {
    payload,
    draft,
    phase,
    errorMessage,
    changed,
    dirtyCount: changed.length,
    sectionChanges,
    invalidKeys,
    invalidMessages,
    feedback,
    saving,
    refreshing: refreshingState,
    refreshFailed,
    serverAhead,
    confirmGate,
    reason,
    setValue,
    setConfirmation,
    setGateReason,
    save,
    undo,
    reset,
    reviewLatest,
    reload: load,
  };
}

export type OperatorConfigController = ReturnType<typeof useOperatorConfig>;
