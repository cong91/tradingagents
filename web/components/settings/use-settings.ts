"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { NETWORK_HINT, parseErrorResponse } from "@/lib/api";

import type { SettingsPayload, SettingValue } from "./types";

// Hợp đồng §1 (docs/ui-api-contract.md:127): settings stale sau 30s — cùng
// chu kỳ tự làm mới mà mode bar đã dùng cho GET /api/settings.
const REFRESH_INTERVAL_MS = 30_000;
const STALE_TICK_MS = 10_000;

const NETWORK_ERROR_MESSAGE = NETWORK_HINT;

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
  | { kind: "saving" }
  | { kind: "saved"; appliedAt: string }
  | { kind: "failed"; message: string };

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
      else errors[key] = "Không được để trống. Dùng “Huỷ thay đổi” để quay về giá trị đã lưu.";
      continue;
    }
    const base = fieldTypeOf(row.type);
    if (base === "int") {
      const parsed = Number(raw);
      if (!Number.isInteger(parsed)) {
        errors[key] = `Phải là số nguyên, nhận được “${raw}”.`;
        continue;
      }
      values[key] = parsed;
    } else if (base === "float") {
      const parsed = Number(raw.replace(",", "."));
      if (!Number.isFinite(parsed)) {
        errors[key] = `Phải là số, nhận được “${raw}”.`;
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
  // Stale là state (không tính Date.now() lúc render — hàm impure): interval
  // dưới cập nhật theo fetchedAtRef mỗi STALE_TICK_MS.
  const [isStale, setIsStale] = useState(false);

  const hasDataRef = useRef(false);
  const dirtyRef = useRef(false);
  const editCounterRef = useRef(0);
  const fetchedAtRef = useRef(0);

  const applyFetch = useCallback((data: SettingsPayload) => {
    setPayload(data);
    fetchedAtRef.current = Date.now();
    setIsStale(false);
    // Không tự ghi đè form khi user đang tương tác (hợp đồng §0.2 stale rule):
    // có thay đổi chưa lưu → chỉ báo máy chủ có dữ liệu mới, giữ nguyên form.
    if (!hasDataRef.current || !dirtyRef.current) {
      const values = valuesFromPayload(data);
      setFormValues(values);
      setSavedValues(values);
      setFieldErrors({});
      setServerAhead(false);
      dirtyRef.current = false;
      hasDataRef.current = true;
    } else {
      setServerAhead(true);
    }
    setPhase("ready");
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/settings", { cache: "no-store" });
      if (!res.ok) throw await readError(res);
      applyFetch((await res.json()) as SettingsPayload);
    } catch (err) {
      setErrorMessage(
        err instanceof SettingsHttpError ? err.message : NETWORK_ERROR_MESSAGE
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
    dirtyRef.current = true;
    editCounterRef.current += 1;
    setFormValues((prev) => ({ ...prev, [key]: value }));
    setFieldErrors((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setSaveState((state) => (state.kind === "saved" ? { kind: "idle" } : state));
  }, []);

  const resetChanges = useCallback(() => {
    setFormValues(savedValues);
    setFieldErrors({});
    dirtyRef.current = false;
    setSaveState((state) => (state.kind === "failed" ? { kind: "idle" } : state));
  }, [savedValues]);

  const save = useCallback(async () => {
    if (!payload || saveState.kind === "saving") return;
    const { values, errors } = buildChangeSet(formValues, savedValues, payload.settings);
    if (Object.keys(errors).length > 0) {
      setFieldErrors(errors);
      setSaveState({
        kind: "failed",
        message: "Có trường chưa hợp lệ — sửa các ô được đánh dấu rồi lưu lại.",
      });
      return;
    }
    const sentKeys = Object.keys(values);
    if (sentKeys.length === 0) return;

    const editsBefore = editCounterRef.current;
    setSaveState({ kind: "saving" });
    try {
      // CSRF hợp đồng §0: PUT bắt buộc Content-Type application/json.
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: values }),
      });
      if (!res.ok) {
        const err = await readError(res);
        if (err.code === "coercion_error") {
          // Hợp đồng §1: 422 kèm details.env_var — map về key để tô đúng ô.
          const details = (err.details ?? {}) as { env_var?: string; message?: string };
          const row = payload.settings.find((candidate) => candidate.env_var === details.env_var);
          if (row) {
            setFieldErrors((prev) => ({
              ...prev,
              [row.key]: `Máy chủ từ chối: ${details.message ?? err.message}`,
            }));
            setSaveState({
              kind: "failed",
              message: "Có giá trị máy chủ không nhận — sửa lại ô được đánh dấu.",
            });
            return;
          }
        }
        const memoryNote =
          err.code === "internal_error"
            ? " Thay đổi có thể đã có hiệu lực tạm thời trong bộ nhớ nhưng chưa ghi vào .env."
            : "";
        setSaveState({ kind: "failed", message: `Lưu thất bại: ${err.message}${memoryNote}` });
        return;
      }
      const data = (await res.json()) as SettingsPayload;
      setPayload(data);
      fetchedAtRef.current = Date.now();
      setIsStale(false);
      const fresh = valuesFromPayload(data);
      setSavedValues(fresh);
      setFormValues((prev) => {
        const next = { ...prev };
        for (const key of sentKeys) {
          if (key in fresh) next[key] = fresh[key];
        }
        return next;
      });
      setFieldErrors({});
      setServerAhead(false);
      // Edit trong lúc await → vẫn còn thay đổi chưa lưu, không ghi đè bởi poll.
      dirtyRef.current = editCounterRef.current !== editsBefore;
      setSaveState({ kind: "saved", appliedAt: data.applied_at ?? data.generated_at });
    } catch {
      dirtyRef.current = true;
      setSaveState({
        kind: "failed",
        message: `Không gửi được yêu cầu lưu tới backend. ${NETWORK_ERROR_MESSAGE}`,
      });
    }
  }, [formValues, payload, saveState.kind, savedValues]);

  return {
    phase,
    errorMessage,
    payload,
    formValues,
    fieldErrors,
    dirtyCount: dirtyKeys.length,
    saveState,
    serverAhead,
    isStale,
    setValue,
    resetChanges,
    save,
    reload: load,
  };
}

export type SettingsController = ReturnType<typeof useSettings>;
