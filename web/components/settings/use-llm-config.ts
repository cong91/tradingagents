"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { networkHint, parseErrorResponse } from "@/lib/api";

export type LlmTier = "quick" | "deep";

export type LlmCredential = {
  supported: boolean;
  required: boolean;
  env_var: string | null;
  configured: boolean;
  source: string | null;
  masked: string | null;
  last4: string | null;
  updated_at: string | null;
};

type ModelOption = { label: string; id: string };

export type LlmProvider = {
  id: string;
  label: string;
  auth_mode: string;
  requires_backend_url: boolean;
  default_backend_url: string | null;
  credential: Record<LlmTier, LlmCredential>;
  models: Record<LlmTier, ModelOption[]>;
};

type LlmConfigPayload = {
  generated_at: string;
  providers: Record<string, LlmProvider>;
  tiers: Record<LlmTier, { provider: string; model: string; backend_url: string | null }>;
};

type CredentialResult = { removed: boolean; configured: boolean; source: string | null };
type Pair = { tier: LlmTier; provider: string };
type Feedback = Pair & {
  kind: "saved" | "removed" | "envAfterRemove" | "notRemoved" | "failed";
};
type KeyDrafts = Record<LlmTier, Map<string, string>>;

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new Error(networkHint());
  }
  if (!response.ok) {
    const body = await parseErrorResponse(response);
    throw new Error(body.message);
  }
  return (await response.json()) as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

export function useLlmConfig(onDirtyChange?: (dirty: boolean) => void) {
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [catalog, setCatalog] = useState<LlmConfigPayload | null>(null);
  const [keyDrafts, setKeyDrafts] = useState<KeyDrafts>(() => ({
    quick: new Map(),
    deep: new Map(),
  }));
  const [pending, setPending] = useState<Pair | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const hasDataRef = useRef(false);
  const pendingRef = useRef(false);
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const request = ++requestRef.current;
    try {
      const next = await requestJson<LlmConfigPayload>("/api/llm-config", { cache: "no-store" });
      if (request !== requestRef.current) return;
      setCatalog(next);
      setPhase("ready");
      setErrorMessage(null);
      hasDataRef.current = true;
    } catch (err) {
      if (request !== requestRef.current) return;
      setErrorMessage(err instanceof Error ? err.message : networkHint());
      if (!hasDataRef.current) setPhase("error");
    }
  }, []);

  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  const dirty = keyDrafts.quick.size > 0 || keyDrafts.deep.size > 0;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  const setKeyDraft = useCallback((tier: LlmTier, provider: string, value: string) => {
    if (pendingRef.current) return;
    setKeyDrafts((current) => {
      const tierDrafts = new Map(current[tier]);
      if (value) tierDrafts.set(provider, value);
      else tierDrafts.delete(provider);
      return { ...current, [tier]: tierDrafts };
    });
  }, []);

  const saveCredential = useCallback(async (tier: LlmTier, provider: string) => {
    const draft = keyDrafts[tier].get(provider) ?? "";
    const apiKey = draft.trim();
    if (pendingRef.current || !apiKey || !catalog?.providers[provider]?.credential[tier]?.supported) return;
    pendingRef.current = true;
    setPending({ tier, provider });
    setFeedback(null);
    try {
      await requestJson(`/api/llm-credentials/${tier}/${encodeURIComponent(provider)}`, {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({ api_key: apiKey }),
      });
      setKeyDrafts((current) => {
        if (current[tier].get(provider) !== draft) return current;
        const tierDrafts = new Map(current[tier]);
        tierDrafts.delete(provider);
        return { ...current, [tier]: tierDrafts };
      });
      setFeedback({ tier, provider, kind: "saved" });
      await load();
    } catch {
      // Do not display server errors from a request containing a secret.
      setFeedback({ tier, provider, kind: "failed" });
    } finally {
      pendingRef.current = false;
      setPending(null);
    }
  }, [catalog, keyDrafts, load]);

  const removeCredential = useCallback(async (tier: LlmTier, provider: string) => {
    const credential = catalog?.providers[provider]?.credential[tier];
    if (pendingRef.current || !credential?.supported || credential.source !== "vault") return;
    pendingRef.current = true;
    setPending({ tier, provider });
    setFeedback(null);
    try {
      const result = await requestJson<CredentialResult>(
        `/api/llm-credentials/${tier}/${encodeURIComponent(provider)}`,
        { method: "DELETE", headers: JSON_HEADERS, body: "{}" }
      );
      setFeedback({
        tier,
        provider,
        kind: !result.removed ? "notRemoved" : result.source === "env" && result.configured
          ? "envAfterRemove" : "removed",
      });
      await load();
    } catch {
      setFeedback({ tier, provider, kind: "failed" });
    } finally {
      pendingRef.current = false;
      setPending(null);
    }
  }, [catalog, load]);

  return {
    phase, catalog, keyDrafts, setKeyDraft, pending, feedback, errorMessage, load,
    saveCredential, removeCredential,
  };
}
