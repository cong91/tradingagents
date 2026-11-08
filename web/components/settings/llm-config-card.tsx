"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  KeyRoundIcon,
  LoaderCircleIcon,
  RefreshCwIcon,
  SaveIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { networkHint, parseErrorResponse } from "@/lib/api";
import { useT, type Messages } from "@/lib/i18n";
import type { SettingValue } from "./types";
import type { SettingsController } from "./use-settings";

export const LLM_SETTING_KEYS = [
  "llm_provider",
  "quick_think_provider",
  "deep_think_provider",
  "quick_think_llm",
  "deep_think_llm",
  "backend_url",
  "quick_think_backend_url",
  "deep_think_backend_url",
  "google_thinking_level",
  "openai_reasoning_effort",
  "anthropic_effort",
] as const;

export type LlmTier = "quick" | "deep";

type ModelOption = {
  label: string;
  id: string;
};

type LlmCredential = {
  supported: boolean;
  required: boolean;
  env_var: string | null;
  configured: boolean;
  source: string | null;
  masked: string | null;
  last4: string | null;
  updated_at: string | null;
};

type LlmProvider = {
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

type Feedback = {
  role: "status" | "alert";
  text: string;
};

class LlmConfigApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "LlmConfigApiError";
    this.status = status;
    this.code = code;
  }
}

function toLlmConfigError(err: unknown): LlmConfigApiError {
  if (err instanceof LlmConfigApiError) return err;
  return new LlmConfigApiError(0, "network", networkHint());
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    throw toLlmConfigError(err);
  }
  if (!response.ok) {
    const body = await parseErrorResponse(response);
    throw new LlmConfigApiError(response.status, body.code, body.message);
  }
  return (await response.json()) as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

const llmConfigApi = {
  get(): Promise<LlmConfigPayload> {
    return requestJson<LlmConfigPayload>("/api/llm-config", { cache: "no-store" });
  },
  setCredential(tier: LlmTier, provider: string, apiKey: string): Promise<unknown> {
    return requestJson(`/api/llm-credentials/${tier}/${encodeURIComponent(provider)}`, {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({ api_key: apiKey }),
    });
  },
  deleteCredential(tier: LlmTier, provider: string): Promise<unknown> {
    return requestJson(`/api/llm-credentials/${tier}/${encodeURIComponent(provider)}`, {
      method: "DELETE",
      headers: JSON_HEADERS,
      body: "{}",
    });
  },
};

function textValue(values: Record<string, SettingValue>, key: string): string {
  const value = values[key];
  return typeof value === "string" ? value : "";
}

function setText(
  onChange: (key: string, value: SettingValue) => void,
  key: string,
  value: string
) {
  onChange(key, value);
}

function reasoningKey(provider: string): string | null {
  if (provider === "google") return "google_thinking_level";
  if (provider === "openai" || provider === "chatgpt") return "openai_reasoning_effort";
  if (provider === "anthropic") return "anthropic_effort";
  return null;
}

function settingKey(tier: LlmTier, field: "provider" | "model" | "backend_url"): string {
  if (field === "model") return `${tier}_think_llm`;
  return `${tier}_think_${field}`;
}

function providerLabel(
  providers: LlmProvider[],
  id: string,
  fallback: string
): string {
  return providers.find((provider) => provider.id === id)?.label ?? fallback;
}

function formatCredentialSource(t: Messages, credential: LlmCredential): string | null {
  return credential.source ? t.settings.llm.source(credential.source) : null;
}

function KeyStatus({ credential }: { credential: LlmCredential }) {
  const t = useT();
  const c = t.settings.llm;
  if (!credential.supported) {
    return <Badge variant="outline">{c.keyManagedElsewhere}</Badge>;
  }
  if (!credential.configured) {
    return <Badge variant="outline">{c.notConfigured}</Badge>;
  }
  return (
    <Badge variant="secondary">
      {c.configured} {credential.masked ?? credential.last4 ?? ""}
    </Badge>
  );
}

function SelectField({
  id,
  label,
  value,
  options,
  onChange,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  options: readonly { value: string; label: string }[];
  onChange: (value: string) => void;
  hint?: string;
}) {
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function TextField({
  id,
  label,
  value,
  placeholder,
  onChange,
  hint,
  type = "text",
}: {
  id: string;
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
  hint?: string;
  type?: "text" | "password";
}) {
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
      <input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        autoComplete={type === "password" ? "new-password" : "off"}
        onChange={(event) => onChange(event.target.value)}
        className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm outline-none placeholder:text-muted-foreground/70 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
      />
    </label>
  );
}

export function LlmConfigCard({ controller }: { controller: SettingsController }) {
  const t = useT();
  const c = t.settings.llm;
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");
  const [catalog, setCatalog] = useState<LlmConfigPayload | null>(null);
  const [selectedTier, setSelectedTier] = useState<LlmTier>("quick");
  const [keyDrafts, setKeyDrafts] = useState<Record<LlmTier, string>>({ quick: "", deep: "" });
  const [pendingKeyTier, setPendingKeyTier] = useState<LlmTier | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const hasDataRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const next = await llmConfigApi.get();
      setCatalog(next);
      setPhase("ready");
      setErrorMessage(null);
      hasDataRef.current = true;
    } catch (err) {
      const apiError = toLlmConfigError(err);
      setErrorMessage(apiError.message);
      if (!hasDataRef.current) setPhase("error");
    }
  }, []);

  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  useEffect(() => {
    if (controller.saveState.kind !== "saved") return;
    const refreshId = setTimeout(() => void load(), 0);
    return () => clearTimeout(refreshId);
  }, [controller.saveState.kind, load]);

  const providers = useMemo(
    () =>
      Object.values(catalog?.providers ?? {}).sort((left, right) =>
        left.label.localeCompare(right.label)
      ),
    [catalog]
  );
  const values = controller.formValues;
  const defaultProviderId = textValue(values, "llm_provider") || catalog?.tiers.quick.provider || "";
  const defaultProvider = catalog?.providers[defaultProviderId];
  const tierProviderOverride = textValue(values, settingKey(selectedTier, "provider"));
  const effectiveProviderId =
    tierProviderOverride || defaultProviderId || catalog?.tiers[selectedTier].provider || "";
  const effectiveProvider = catalog?.providers[effectiveProviderId];
  const tierModelKey = settingKey(selectedTier, "model");
  const tierBackendUrlKey = settingKey(selectedTier, "backend_url");
  const currentModel = textValue(values, tierModelKey);
  const modelOptions = effectiveProvider?.models[selectedTier] ?? [];
  const modelSelectValue =
    modelOptions.some((option) => option.id === currentModel) && currentModel !== "custom"
      ? currentModel
      : "custom";
  const showCustomModel = modelSelectValue === "custom";
  const currentTierUrl = textValue(values, tierBackendUrlKey);
  const currentDefaultUrl = textValue(values, "backend_url");
  const defaultProviderNeedsUrl = Boolean(defaultProvider);
  const tierProviderNeedsUrl = Boolean(tierProviderOverride || currentTierUrl);
  const selectedReasoningKey = reasoningKey(effectiveProviderId);
  const selectedCredential = effectiveProvider?.credential[selectedTier];
  const sourceLabel = selectedCredential
    ? formatCredentialSource(t, selectedCredential)
    : null;

  const providerOptions = useMemo(() => {
    const known = new Set(providers.map((provider) => provider.id));
    const options = providers.map((provider) => ({ value: provider.id, label: provider.label }));
    for (const id of [defaultProviderId, effectiveProviderId]) {
      if (id && !known.has(id)) options.push({ value: id, label: id });
    }
    return options;
  }, [defaultProviderId, effectiveProviderId, providers]);

  const tierProviderOptions = useMemo(
    () => [
      {
        value: "",
        label: c.useDefaultProvider(providerLabel(providers, defaultProviderId, defaultProviderId)),
      },
      ...providerOptions,
    ],
    [c, defaultProviderId, providerOptions, providers]
  );

  const saveCredential = async () => {
    const apiKey = keyDrafts[selectedTier].trim();
    if (!apiKey || !effectiveProviderId || !selectedCredential?.supported) return;
    setPendingKeyTier(selectedTier);
    setFeedback(null);
    try {
      await llmConfigApi.setCredential(selectedTier, effectiveProviderId, apiKey);
      setKeyDrafts((current) => ({ ...current, [selectedTier]: "" }));
      setFeedback({ role: "status", text: c.keySaved });
      await load();
    } catch (err) {
      setFeedback({ role: "alert", text: toLlmConfigError(err).message });
    } finally {
      setPendingKeyTier(null);
    }
  };

  const removeCredential = async () => {
    if (!effectiveProviderId || !selectedCredential?.supported || !selectedCredential.configured) return;
    if (!window.confirm(c.apiKeyRemoveConfirm)) return;
    setPendingKeyTier(selectedTier);
    setFeedback(null);
    try {
      await llmConfigApi.deleteCredential(selectedTier, effectiveProviderId);
      setFeedback({ role: "status", text: c.keyRemoved });
      await load();
    } catch (err) {
      setFeedback({ role: "alert", text: toLlmConfigError(err).message });
    } finally {
      setPendingKeyTier(null);
    }
  };

  if (phase === "loading") {
    return (
      <Card aria-busy="true" aria-live="polite">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRoundIcon className="size-4" aria-hidden="true" />
            {c.title}
          </CardTitle>
          <CardDescription>{c.loading}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-48 animate-pulse rounded-lg bg-muted" />
        </CardContent>
      </Card>
    );
  }

  if (phase === "error" || !catalog) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TriangleAlertIcon className="size-4 shrink-0 text-destructive" aria-hidden="true" />
            {c.loadFailed}
          </CardTitle>
          <CardDescription>{errorMessage ?? c.loadFailed}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button className="min-touch" onClick={() => void load()}>
            <RefreshCwIcon aria-hidden="true" />
            {t.common.retry}
          </Button>
        </CardContent>
      </Card>
    );
  }

  const selectedTierLabel = selectedTier === "quick" ? c.quickTier : c.deepTier;
  const reasoningLabel = selectedReasoningKey ? c.reasoningLabel(effectiveProviderId) : null;
  const canWriteCredential = Boolean(selectedCredential?.supported && effectiveProviderId);
  const keyBusy = pendingKeyTier === selectedTier;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRoundIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex flex-wrap gap-2" role="tablist" aria-label={c.tierSelector}>
          {(["quick", "deep"] as const).map((tier) => {
            const active = tier === selectedTier;
            return (
              <Button
                key={tier}
                type="button"
                role="tab"
                aria-selected={active}
                variant={active ? "default" : "outline"}
                className="min-touch"
                onClick={() => setSelectedTier(tier)}
              >
                {tier === "quick" ? c.quickTier : c.deepTier}
              </Button>
            );
          })}
        </div>

        <section className="space-y-4" aria-labelledby="llm-tier-heading">
          <div>
            <h3 id="llm-tier-heading" className="text-sm font-semibold">
              {selectedTierLabel}
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {selectedTier === "quick" ? c.quickTierHint : c.deepTierHint}
            </p>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              id="llm-default-provider"
              label={c.defaultProvider}
              value={defaultProviderId}
              options={providerOptions}
              onChange={(value) => setText(controller.setValue, "llm_provider", value)}
              hint={c.defaultProviderHint}
            />
            <SelectField
              id={`llm-${selectedTier}-provider`}
              label={c.tierProvider(selectedTierLabel)}
              value={tierProviderOverride}
              options={tierProviderOptions}
              onChange={(value) =>
                setText(controller.setValue, settingKey(selectedTier, "provider"), value)
              }
              hint={c.tierProviderHint}
            />
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              id={`llm-${selectedTier}-model`}
              label={c.model}
              value={modelSelectValue}
              options={[
                ...modelOptions.map((option) => ({ value: option.id, label: option.label })),
                ...(modelOptions.some((option) => option.id === "custom")
                  ? []
                  : [{ value: "custom", label: c.customModel }]),
              ]}
              onChange={(value) => {
                setText(controller.setValue, tierModelKey, value === "custom" ? "" : value);
              }}
              hint={effectiveProvider?.label ?? effectiveProviderId}
            />
            {showCustomModel ? (
              <TextField
                id={`llm-${selectedTier}-custom-model`}
                label={c.customModel}
                value={currentModel}
                placeholder={c.customModelPlaceholder}
                onChange={(value) => setText(controller.setValue, tierModelKey, value)}
              />
            ) : null}
          </div>

          {defaultProviderNeedsUrl ? (
            <TextField
              id="llm-default-backend-url"
              label={c.defaultBackendUrl}
              value={currentDefaultUrl}
              placeholder={defaultProvider?.default_backend_url ?? c.backendUrlPlaceholder}
              onChange={(value) => setText(controller.setValue, "backend_url", value)}
              hint={c.backendUrlHint}
            />
          ) : null}

          {tierProviderNeedsUrl ? (
            <TextField
              id={`llm-${selectedTier}-backend-url`}
              label={c.tierBackendUrl(selectedTierLabel)}
              value={currentTierUrl}
              placeholder={effectiveProvider?.default_backend_url ?? c.backendUrlPlaceholder}
              onChange={(value) => setText(controller.setValue, tierBackendUrlKey, value)}
              hint={c.tierBackendUrlHint}
            />
          ) : null}

          {selectedReasoningKey ? (
            <TextField
              id={`llm-${selectedTier}-reasoning`}
              label={reasoningLabel ?? c.reasoning}
              value={textValue(values, selectedReasoningKey)}
              placeholder={c.reasoningPlaceholder}
              onChange={(value) => setText(controller.setValue, selectedReasoningKey, value)}
              hint={c.reasoningHint}
            />
          ) : null}

          <div className="rounded-lg border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">{c.apiKeyTitle}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">{c.apiKeyDescription}</p>
              </div>
              {selectedCredential ? <KeyStatus credential={selectedCredential} /> : null}
            </div>

            {selectedCredential && canWriteCredential ? (
              <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-end">
                <TextField
                  id={`llm-${selectedTier}-api-key`}
                  label={c.apiKeyInput}
                  value={keyDrafts[selectedTier]}
                  placeholder={
                    selectedCredential.configured ? c.apiKeyReplacePlaceholder : c.apiKeyPlaceholder
                  }
                  onChange={(value) =>
                    setKeyDrafts((current) => ({ ...current, [selectedTier]: value }))
                  }
                  type="password"
                  hint={sourceLabel ?? undefined}
                />
                <Button
                  type="button"
                  className="min-touch"
                  disabled={keyBusy || keyDrafts[selectedTier].trim().length === 0}
                  onClick={() => void saveCredential()}
                >
                  {keyBusy ? (
                    <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
                  ) : (
                    <SaveIcon aria-hidden="true" />
                  )}
                  {c.apiKeySave}
                </Button>
                {selectedCredential.configured ? (
                  <Button
                    type="button"
                    variant="destructive"
                    className="min-touch"
                    disabled={keyBusy}
                    onClick={() => void removeCredential()}
                    aria-label={c.apiKeyRemove}
                  >
                    <Trash2Icon aria-hidden="true" />
                    {c.apiKeyRemove}
                  </Button>
                ) : null}
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted-foreground">
                {selectedCredential?.required ? c.keyRequiredElsewhere : c.keyManagedElsewhere}
              </p>
            )}
          </div>
        </section>

        {feedback ? (
          <p
            role={feedback.role}
            className={feedback.role === "alert" ? "text-sm text-destructive" : "text-sm text-profit"}
          >
            {feedback.text}
          </p>
        ) : null}

        <p className="text-xs text-muted-foreground">{c.saveHint}</p>
      </CardContent>
    </Card>
  );
}
