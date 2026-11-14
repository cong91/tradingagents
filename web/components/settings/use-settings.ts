"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { networkHint, parseErrorResponse } from "@/lib/api";
import { getMessages } from "@/lib/i18n/messages";

import type { SettingsPayload, SettingValue } from "./types";

// Hợp đồng §1 (docs/ui-api-contract.md:127): settings stale sau 30s — cùng
// chu kỳ tự làm mới mà mode bar đã dùng cho GET /api/settings.
const REFRESH_INTERVAL_MS = 30_000;
const STALE_TICK_MS = 10_000;

// Thông điệp lỗi dựng lúc xảy ra lỗi, theo locale đang chọn.
const saveCopy = () => getMessages().settings.save;

export type FieldType = "bool" | "int" | "float" | "text";

export function fieldTypeOf(type: string): FieldType {
  const base = type.split("|")[0];
  if (base === "bool") return "bool";
  if (base === "int") return "int";
  if (base === "float") return "float";
  return "text";
}

export function isNullableType(type: string): boolean {
  return type.endsWith("|null");
}

export type SaveState =
  | { kind: "idle" }
  | { kind: "saving"; scope: string }
  | { kind: "saved"; appliedAt: string; scope: string }
  | { kind: "failed"; message: string; scope: string }
  | { kind: "uncertain"; scope: string };

type Phase = "loading" | "error" | "ready";

class SettingsHttpError extends Error {
  status: number;
  code: string | null;
  details: Record<string, unknown> | null;

  constructor(
    message: string,
    status: number,
    code: string | null,
    details: Record<string, unknown> | null
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function readError(res: Response): Promise<SettingsHttpError> {
  // Parse envelope §0.1/{detail} ở lib/api.ts — chung cho cả 6 màn.
  const body = await parseErrorResponse(res);
  return new SettingsHttpError(body.message, res.status, body.code, body.details);
}

function valuesFromPayload(data: SettingsPayload): Record<string, SettingValue> {
  const out: Record<string, SettingValue> = {};
  for (const row of data.settings) {
    out[row.key] =
      fieldTypeOf(row.type) === "bool"
        ? row.value === true
        : row.value === null || row.value === undefined
          ? ""
          : String(row.value);
  }
  return out;
}

function changedValueMap(
  values: Record<string, number | string | boolean | null>,
  rows: SettingsPayload["settings"],
): Record<string, SettingValue> {
  const byKey = new Map(rows.map((row) => [row.key, row]));
  const converted: Record<string, SettingValue> = {};
  for (const [key, value] of Object.entries(values)) {
    const row = byKey.get(key);
    converted[key] = row && fieldTypeOf(row.type) === "bool"
      ? value === true
      : value === null ? "" : String(value);
  }
  return converted;
}

function hasDirtyValues(
  formValues: Record<string, SettingValue>,
  savedValues: Record<string, SettingValue>,
): boolean {
  const keys = new Set([...Object.keys(formValues), ...Object.keys(savedValues)]);
  return [...keys].some((key) => formValues[key] !== savedValues[key]);
}

type MergeSnapshot = {
  formValues: Record<string, SettingValue>;
  conflictKeys: string[];
};

function mergeFreshValues({
  baseline,
  currentDraft,
  fresh,
  sentKeys,
  sentDraft,
  expectedSent,
}: {
  baseline: Record<string, SettingValue>;
  currentDraft: Record<string, SettingValue>;
  fresh: Record<string, SettingValue>;
  sentKeys: readonly string[];
  sentDraft: Record<string, SettingValue>;
  expectedSent: Record<string, SettingValue>;
}): MergeSnapshot {
  const next = { ...currentDraft };
  const conflicts = new Set<string>();
  const sent = new Set(sentKeys);
  const keys = new Set([
    ...Object.keys(baseline),
    ...Object.keys(currentDraft),
    ...Object.keys(fresh),
  ]);

  for (const key of keys) {
    const baselineValue = baseline[key];
    const currentValue = currentDraft[key];
    const freshValue = fresh[key];

    if (sent.has(key)) {
      if (freshValue !== expectedSent[key]) {
        conflicts.add(key);
        continue;
      }
      if (currentValue === baselineValue || currentValue === sentDraft[key]) {
        if (key in fresh) next[key] = freshValue;
        else delete next[key];
      }
      continue;
    }

    if (currentValue === baselineValue) {
      if (key in fresh) next[key] = freshValue;
      else delete next[key];
      continue;
    }
    if (freshValue !== baselineValue && freshValue !== currentValue) conflicts.add(key);
  }

  return { formValues: next, conflictKeys: [...conflicts] };
}

type ChangeSet = {
  values: Record<string, number | string | boolean | null>;
  errors: Record<string, string>;
};

/** Client-validate các ô dirty trước khi PUT; máy chủ vẫn là nguồn chân lý cuối. */
function buildChangeSet(
  formValues: Record<string, SettingValue>,
  savedValues: Record<string, SettingValue>,
  rows: SettingsPayload["settings"]
): ChangeSet {
  const values: ChangeSet["values"] = {};
  const errors: Record<string, string> = {};
  for (const [key, current] of Object.entries(formValues)) {
    if (current === savedValues[key]) continue;
    const row = rows.find((candidate) => candidate.key === key);
    if (!row) {
      // Key máy chủ trả nhưng không khai type: gửi nguyên văn, để server phán.
      values[key] = current;
      continue;
    }
    if (fieldTypeOf(row.type) === "bool") {
      values[key] = current === true;
      continue;
    }
    const raw = String(current).trim();
    if (raw === "") {
      if (isNullableType(row.type)) values[key] = null;
      else errors[key] = saveCopy().required;
      continue;
    }
    const base = fieldTypeOf(row.type);
    if (base === "int") {
      const parsed = Number(raw);
      if (!Number.isInteger(parsed)) {
        errors[key] = saveCopy().mustBeInteger(raw);
        continue;
      }
      values[key] = parsed;
    } else if (base === "float") {
      const parsed = Number(raw.replace(",", "."));
      if (!Number.isFinite(parsed)) {
        errors[key] = saveCopy().mustBeNumber(raw);
        continue;
      }
      values[key] = parsed;
    } else {
      values[key] = raw;
    }
  }
  return { values, errors };
}

export function useSettings() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [payload, setPayload] = useState<SettingsPayload | null>(null);
  const [formValues, setFormValues] = useState<Record<string, SettingValue>>({});
  const [savedValues, setSavedValues] = useState<Record<string, SettingValue>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saveState, setSaveState] = useState<SaveState>({ kind: "idle" });
  const [serverAhead, setServerAhead] = useState(false);
  const [conflictKeys, setConflictKeys] = useState<string[]>([]);
  const [serverValues, setServerValues] = useState<Record<string, SettingValue>>({});
  const [reviewing, setReviewing] = useState(false);
  // Stale là state (không tính Date.now() lúc render — hàm impure): interval
  // dưới cập nhật theo fetchedAtRef mỗi STALE_TICK_MS.
  const [isStale, setIsStale] = useState(false);

  const hasDataRef = useRef(false);
  const dirtyRef = useRef(false);
  const savingRef = useRef(false);
  const reviewingRef = useRef(false);
  const serverAheadRef = useRef(false);
  const retryBlockedRef = useRef(false);
  const payloadRef = useRef<SettingsPayload | null>(null);
  const formValuesRef = useRef<Record<string, SettingValue>>({});
  const savedValuesRef = useRef<Record<string, SettingValue>>({});
  const fetchSequenceRef = useRef(0);
  const fetchedAtRef = useRef(0);

  const applyFetch = useCallback((data: SettingsPayload) => {
    payloadRef.current = data;
    setPayload(data);
    fetchedAtRef.current = Date.now();
    setIsStale(false);
    // Không tự ghi đè form khi user đang tương tác (hợp đồng §0.2 stale rule):
    // có thay đổi chưa lưu → chỉ báo máy chủ có dữ liệu mới, giữ nguyên form.
    const values = valuesFromPayload(data);
    setServerValues(values);
    if (!hasDataRef.current || (!dirtyRef.current && !serverAheadRef.current && !retryBlockedRef.current)) {
      formValuesRef.current = values;
      savedValuesRef.current = values;
      setFormValues(values);
      setSavedValues(values);
      setFieldErrors({});
      serverAheadRef.current = false;
      setServerAhead(false);
      dirtyRef.current = false;
      hasDataRef.current = true;
    } else {
      const baseline = savedValuesRef.current;
      const changedKeys = [...new Set([...Object.keys(values), ...Object.keys(baseline)])]
        .filter((key) => values[key] !== baseline[key]);
      const ahead = serverAheadRef.current || changedKeys.length > 0;
      if (changedKeys.length > 0) {
        setConflictKeys((previous) => [...new Set([...previous, ...changedKeys])]);
      }
      serverAheadRef.current = ahead;
      setServerAhead(ahead);
    }
    setPhase("ready");
  }, []);

  const load = useCallback(async () => {
    if (savingRef.current || reviewingRef.current) return;
    const sequence = ++fetchSequenceRef.current;
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw await readError(res);
      const data = (await res.json()) as SettingsPayload;
      if (sequence === fetchSequenceRef.current) applyFetch(data);
    } catch (err) {
      if (sequence !== fetchSequenceRef.current) return;
      setErrorMessage(
        err instanceof SettingsHttpError ? err.message : networkHint()
      );
      // Đã có dữ liệu: giữ nguyên UI (badge "cũ" tự hiện vì fetchedAt ngừng cập nhật).
      if (!hasDataRef.current) setPhase("error");
    }
  }, [applyFetch]);

  useEffect(() => {
    const clockId = setInterval(() => {
      const fetchedAt = fetchedAtRef.current;
      setIsStale(fetchedAt > 0 && Date.now() - fetchedAt > REFRESH_INTERVAL_MS);
    }, STALE_TICK_MS);
    return () => clearInterval(clockId);
  }, []);

  useEffect(() => {
    // Nạp lần đầu trong callback (không setState đồng bộ trong effect body —
    // cùng pattern mà mode bar dùng cho GET /api/settings).
    const initialId = setTimeout(() => {
      void load();
    }, 0);
    const pollId = setInterval(() => {
      void load();
    }, REFRESH_INTERVAL_MS);
    return () => {
      clearTimeout(initialId);
      clearInterval(pollId);
    };
  }, [load]);

  const dirtyKeys = useMemo(
    () =>
      Object.keys(formValues).filter((key) => formValues[key] !== savedValues[key]),
    [formValues, savedValues]
  );

  const setValue = useCallback((key: string, value: SettingValue) => {
    const next = { ...formValuesRef.current, [key]: value };
    formValuesRef.current = next;
    dirtyRef.current = Object.keys(next).some((name) => next[name] !== savedValuesRef.current[name]);
    setFormValues(next);
    setFieldErrors((prev) => {
      if (!(key in prev)) return prev;
      const updated = { ...prev };
      delete updated[key];
      return updated;
    });
    setSaveState((state) => (state.kind === "saving" || state.kind === "uncertain" ? state : { kind: "idle" }));
  }, []);

  const resetChanges = useCallback((keys?: readonly string[]) => {
    const selected = keys ?? Object.keys(savedValuesRef.current);
    const next = { ...formValuesRef.current };
    for (const key of selected) {
      if (key in savedValuesRef.current) next[key] = savedValuesRef.current[key];
    }
    formValuesRef.current = next;
    dirtyRef.current = Object.keys(next).some((key) => next[key] !== savedValuesRef.current[key]);
    setFormValues(next);
    setFieldErrors((prev) => Object.fromEntries(
      Object.entries(prev).filter(([key]) => !selected.includes(key))
    ));
    setSaveState((state) => (state.kind === "saving" || state.kind === "uncertain" ? state : { kind: "idle" }));
  }, []);

  const save = useCallback(async (keys?: readonly string[], scope = "settings") => {
    const currentPayload = payloadRef.current;
    if (!currentPayload || savingRef.current || reviewingRef.current || serverAheadRef.current || retryBlockedRef.current) return;
    const baselineBefore = { ...savedValuesRef.current };
    const draftBefore = { ...formValuesRef.current };
    const allowed = keys ? new Set(keys) : null;
    const selectedValues = allowed
      ? Object.fromEntries(Object.entries(draftBefore).filter(([key]) => allowed.has(key)))
      : draftBefore;
    const { values, errors } = buildChangeSet(selectedValues, baselineBefore, currentPayload.settings);
    if (Object.keys(errors).length > 0) {
      setFieldErrors((prev) => ({ ...prev, ...errors }));
      setSaveState({ kind: "failed", message: saveCopy().invalidFields, scope });
      return;
    }
    const sentKeys = Object.keys(values);
    if (sentKeys.length === 0) return;

    const sentDraft = { ...selectedValues };
    const expectedSent = changedValueMap(values, currentPayload.settings);
    savingRef.current = true;
    ++fetchSequenceRef.current;
    setSaveState({ kind: "saving", scope });
    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: values }),
      });
      if (!res.ok) {
        const err = await readError(res);
        if (err.code === "coercion_error") {
          const details = (err.details ?? {}) as { env_var?: string; message?: string };
          const row = currentPayload.settings.find((candidate) => candidate.env_var === details.env_var);
          if (row) {
            setFieldErrors((prev) => ({
              ...prev,
              [row.key]: saveCopy().serverRejected(details.message ?? err.message),
            }));
            setSaveState({ kind: "failed", message: saveCopy().serverRejectedSummary, scope });
            return;
          }
        }
        if (err.code === "validation_error") {
          const fields = err.details?.fields;
          if (fields !== null && typeof fields === "object" && !Array.isArray(fields)) {
            const knownKeys = new Set(currentPayload.settings.map((row) => row.key));
            const fieldErrors = Object.fromEntries(
              Object.entries(fields).filter(([key, message]) =>
                knownKeys.has(key) && typeof message === "string"
              ).map(([key, message]) => [key, saveCopy().serverRejected(message as string)])
            );
            if (Object.keys(fieldErrors).length > 0) {
              setFieldErrors((previous) => ({ ...previous, ...fieldErrors }));
              setSaveState({ kind: "failed", message: saveCopy().serverRejectedSummary, scope });
              return;
            }
          }
        }
        const memoryNote = err.code === "internal_error" ? saveCopy().memoryNote : "";
        setSaveState({ kind: "failed", message: saveCopy().failed(`${err.message}${memoryNote}`), scope });
        return;
      }
      const data = (await res.json()) as SettingsPayload;
      ++fetchSequenceRef.current;
      const fresh = valuesFromPayload(data);
      const merged = mergeFreshValues({
        baseline: baselineBefore,
        currentDraft: formValuesRef.current,
        fresh,
        sentKeys,
        sentDraft,
        expectedSent,
      });
      payloadRef.current = data;
      setPayload(data);
      fetchedAtRef.current = Date.now();
      setIsStale(false);
      savedValuesRef.current = fresh;
      setSavedValues(fresh);
      formValuesRef.current = merged.formValues;
      setFormValues(merged.formValues);
      setFieldErrors((prev) => Object.fromEntries(
        Object.entries(prev).filter(([key]) => !sentKeys.includes(key))
      ));
      const ahead = merged.conflictKeys.length > 0;
      setServerValues(fresh);
      setConflictKeys(merged.conflictKeys);
      serverAheadRef.current = ahead;
      setServerAhead(ahead);
      dirtyRef.current = hasDirtyValues(merged.formValues, fresh);
      retryBlockedRef.current = false;
      setSaveState({ kind: "saved", appliedAt: data.applied_at ?? data.generated_at, scope });
    } catch {
      try {
        const readback = await fetch("/api/settings", { cache: "no-store" });
        if (!readback.ok) throw await readError(readback);
        const data = (await readback.json()) as SettingsPayload;
        ++fetchSequenceRef.current;
        const fresh = valuesFromPayload(data);
        const merged = mergeFreshValues({
          baseline: baselineBefore,
          currentDraft: formValuesRef.current,
          fresh,
          sentKeys,
          sentDraft,
          expectedSent,
        });
        payloadRef.current = data;
        setPayload(data);
        savedValuesRef.current = fresh;
        setSavedValues(fresh);
        formValuesRef.current = merged.formValues;
        setFormValues(merged.formValues);
        setFieldErrors((prev) => Object.fromEntries(
          Object.entries(prev).filter(([key]) => !sentKeys.includes(key))
        ));
        fetchedAtRef.current = Date.now();
        setIsStale(false);
        const ahead = merged.conflictKeys.length > 0;
        setServerValues(fresh);
        setConflictKeys(merged.conflictKeys);
        serverAheadRef.current = ahead;
        setServerAhead(ahead);
        dirtyRef.current = hasDirtyValues(merged.formValues, fresh);
        const verified = sentKeys.every((key) => fresh[key] === expectedSent[key]);
        retryBlockedRef.current = !verified;
        setSaveState(verified
          ? { kind: "saved", appliedAt: data.applied_at ?? data.generated_at, scope }
          : { kind: "uncertain", scope });
      } catch {
        retryBlockedRef.current = true;
        setSaveState({ kind: "uncertain", scope });
      }
    } finally {
      savingRef.current = false;
    }
  }, []);

  const reviewLatest = useCallback(async () => {
    if (savingRef.current || reviewingRef.current) return;
    reviewingRef.current = true;
    setReviewing(true);
    const sequence = ++fetchSequenceRef.current;
    const baselineBefore = { ...savedValuesRef.current };
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw await readError(res);
      const data = (await res.json()) as SettingsPayload;
      if (sequence !== fetchSequenceRef.current) return;
      const fresh = valuesFromPayload(data);
      const merged = mergeFreshValues({
        baseline: baselineBefore,
        currentDraft: formValuesRef.current,
        fresh,
        sentKeys: [],
        sentDraft: {},
        expectedSent: {},
      });
      payloadRef.current = data;
      setPayload(data);
      savedValuesRef.current = fresh;
      setSavedValues(fresh);
      formValuesRef.current = merged.formValues;
      setFormValues(merged.formValues);
      setFieldErrors({});
      fetchedAtRef.current = Date.now();
      setIsStale(false);
      setServerValues(fresh);
      setConflictKeys([]);
      serverAheadRef.current = false;
      setServerAhead(false);
      retryBlockedRef.current = false;
      dirtyRef.current = hasDirtyValues(merged.formValues, fresh);
      setSaveState({ kind: "idle" });
    } catch {
      if (sequence !== fetchSequenceRef.current) return;
      retryBlockedRef.current = true;
      setSaveState((state) => ({
        kind: "uncertain",
        scope: state.kind === "idle" ? "settings" : state.scope,
      }));
    } finally {
      reviewingRef.current = false;
      setReviewing(false);
    }
  }, []);

  const discardAndReload = useCallback(async (): Promise<boolean> => {
    if (savingRef.current || reviewingRef.current) return false;
    reviewingRef.current = true;
    setReviewing(true);
    const sequence = ++fetchSequenceRef.current;
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw await readError(res);
      const data = (await res.json()) as SettingsPayload;
      if (sequence !== fetchSequenceRef.current) return false;
      const values = valuesFromPayload(data);
      payloadRef.current = data;
      formValuesRef.current = values;
      savedValuesRef.current = values;
      setPayload(data);
      setFormValues(values);
      setSavedValues(values);
      setServerValues(values);
      setConflictKeys([]);
      setFieldErrors({});
      fetchedAtRef.current = Date.now();
      setIsStale(false);
      serverAheadRef.current = false;
      setServerAhead(false);
      retryBlockedRef.current = false;
      dirtyRef.current = false;
      hasDataRef.current = true;
      setPhase("ready");
      setSaveState({ kind: "idle" });
      return true;
    } catch {
      if (sequence !== fetchSequenceRef.current) return false;
      retryBlockedRef.current = true;
      setSaveState((state) => ({
        kind: "uncertain",
        scope: state.kind === "idle" ? "settings" : state.scope,
      }));
      return false;
    } finally {
      reviewingRef.current = false;
      setReviewing(false);
    }
  }, []);

  const conflictValues = useMemo<Record<string, { server: SettingValue | undefined; draft: SettingValue | undefined }>>(
    () => Object.fromEntries(conflictKeys.map((key) => [
      key,
      { server: serverValues[key], draft: formValues[key] },
    ])),
    [conflictKeys, serverValues, formValues]
  );

  return {
    phase,
    errorMessage,
    payload,
    formValues,
    fieldErrors,
    dirtyCount: dirtyKeys.length,
    dirtyKeys,
    saveState,
    serverAhead,
    conflictValues,
    reviewing,
    isStale,
    setValue,
    resetChanges,
    save,
    reviewLatest,
    discardAndReload,
    reload: load,
  };
}

export type SettingsController = ReturnType<typeof useSettings>;
