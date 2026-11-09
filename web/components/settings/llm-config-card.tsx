"use client";

import { useEffect, useMemo, useState } from "react";
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
import { useT } from "@/lib/i18n";
import type { SettingValue } from "./types";
import { useLlmConfig, type LlmCredential, type LlmProvider, type LlmTier } from "./use-llm-config";
import type { SettingsController } from "./use-settings";

export type { LlmTier } from "./use-llm-config";

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

const REASONING_KEYS = ["google_thinking_level", "openai_reasoning_effort", "anthropic_effort"];

function textValue(values: Record<string, SettingValue>, key: string): string {
  const value = values[key];
  return typeof value === "string" ? value : "";
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

function providerLabel(providers: LlmProvider[], id: string): string {
  return providers.find((provider) => provider.id === id)?.label ?? id;
}

function KeyStatus({ credential }: { credential: LlmCredential }) {
  const c = useT().settings.llm;
  if (!credential.supported) return <Badge variant="outline">{c.keyManagedElsewhere}</Badge>;
  if (!credential.configured) return <Badge variant="outline">{c.notConfigured}</Badge>;
  return (
    <Badge variant="secondary">
      {c.configured} {credential.masked ?? (credential.last4 ? `****${credential.last4}` : "")}
    </Badge>
  );
}

type FieldProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  error?: string;
  disabled?: boolean;
};

function SelectField({ id, label, value, options, onChange, hint, error, disabled }: FieldProps & {
  options: readonly { value: string; label: string }[];
}) {
  const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {hint ? <span id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</span> : null}
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-[invalid=true]:border-destructive"
      >
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      {error ? <span id={`${id}-error`} role="alert" className="text-xs text-destructive">{error}</span> : null}
    </label>
  );
}

function TextField({ id, label, value, placeholder, onChange, hint, error, disabled, type = "text" }: FieldProps & {
  placeholder?: string;
  type?: "text" | "password";
}) {
  const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1.5 text-sm">
      <span className="font-medium">{label}</span>
      {hint ? <span id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</span> : null}
      <input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        autoComplete={type === "password" ? "new-password" : "off"}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className="h-11 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm outline-none placeholder:text-muted-foreground/70 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-[invalid=true]:border-destructive"
      />
      {error ? <span id={`${id}-error`} role="alert" className="text-xs text-destructive">{error}</span> : null}
    </label>
  );
}

export function LlmConfigCard({
  controller,
  onDirtyChange,
}: {
  controller: SettingsController;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const t = useT();
  const c = t.settings.llm;
  const llm = useLlmConfig(onDirtyChange);
  const { load } = llm;
  const [selectedTier, setSelectedTier] = useState<LlmTier>("quick");
  const [advancedOpen, setAdvancedOpen] = useState(false);

  useEffect(() => {
    if (controller.saveState.kind !== "saved") return;
    const refreshId = setTimeout(() => void load(), 0);
    return () => clearTimeout(refreshId);
  }, [controller.saveState.kind, load]);

  const providers = useMemo(
    () => Object.values(llm.catalog?.providers ?? {}).sort((a, b) => a.label.localeCompare(b.label)),
    [llm.catalog]
  );
  const values = controller.formValues;
  const errors = controller.fieldErrors;
  const catalog = llm.catalog;
  const defaultProviderId = textValue(values, "llm_provider") || catalog?.tiers.quick.provider || "";
  const defaultProvider = catalog?.providers[defaultProviderId];
  const tierProviderOverride = textValue(values, settingKey(selectedTier, "provider"));
  const effectiveProviderId = tierProviderOverride || defaultProviderId || catalog?.tiers[selectedTier].provider || "";
  const effectiveProvider = catalog?.providers[effectiveProviderId];
  const tierModelKey = settingKey(selectedTier, "model");
  const tierBackendUrlKey = settingKey(selectedTier, "backend_url");
  const currentModel = textValue(values, tierModelKey);
  const modelOptions = effectiveProvider?.models[selectedTier] ?? [];
  const modelSelectValue = modelOptions.some((option) => option.id === currentModel) && currentModel !== "custom"
    ? currentModel : "custom";
  const currentTierUrl = textValue(values, tierBackendUrlKey);
  const currentDefaultUrl = textValue(values, "backend_url");
  const selectedReasoningKey = reasoningKey(effectiveProviderId);
  const selectedCredential = effectiveProvider?.credential[selectedTier];
  const selectedTierLabel = selectedTier === "quick" ? c.quickTier : c.deepTier;
  const selectedProviderLabel = providerLabel(providers, effectiveProviderId);
  const keyDraft = llm.keyDrafts[selectedTier].get(effectiveProviderId) ?? "";
  const keyBusy = llm.pending !== null || controller.saveState.kind === "saving";
  const credentialUnavailable = keyBusy || llm.errorMessage !== null;
  const providerIsDraft = Boolean(catalog && effectiveProviderId !== catalog.tiers[selectedTier].provider);
  const defaultProviderIsDraft = Boolean(controller.payload?.settings.some(
    (row) => row.key === "llm_provider" && String(row.value ?? "") !== textValue(values, "llm_provider")
  ));
  const pendingProviderChange = defaultProviderIsDraft || Boolean(catalog && (["quick", "deep"] as const).some((tier) => {
    const provider = textValue(values, settingKey(tier, "provider")) || defaultProviderId;
    return provider && provider !== catalog.tiers[tier].provider;
  }));

  const providerOptions = useMemo(() => {
    const known = new Set(providers.map((provider) => provider.id));
    const options = providers.map((provider) => ({ value: provider.id, label: provider.label }));
    for (const id of new Set([defaultProviderId, effectiveProviderId])) {
      if (id && !known.has(id)) options.push({ value: id, label: id });
    }
    return options;
  }, [defaultProviderId, effectiveProviderId, providers]);
  const tierProviderOptions = [
    { value: "", label: c.useDefaultProvider(providerLabel(providers, defaultProviderId)) },
    ...providerOptions,
  ];
  const tierHasErrors = (tier: LlmTier) => {
    const tierProvider = textValue(values, settingKey(tier, "provider")) || defaultProviderId;
    const key = reasoningKey(tierProvider);
    return [settingKey(tier, "provider"), settingKey(tier, "model"), settingKey(tier, "backend_url")]
      .some((field) => Boolean(errors[field])) || Boolean(key && errors[key]);
  };
  const hiddenReasoningErrors = REASONING_KEYS.filter((key) => key !== selectedReasoningKey && errors[key]);
  const advancedHasErrors = Boolean(errors.llm_provider || errors[tierBackendUrlKey] || errors.backend_url);

  if (llm.phase === "loading") {
    return (
      <Card aria-busy="true" aria-live="polite">
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><KeyRoundIcon className="size-4" aria-hidden="true" />{c.title}</CardTitle>
          <CardDescription>{c.loading}</CardDescription>
        </CardHeader>
        <CardContent><div className="h-48 animate-pulse rounded-lg bg-muted" /></CardContent>
      </Card>
    );
  }
  if (llm.phase === "error" || !catalog) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><TriangleAlertIcon className="size-4 shrink-0 text-destructive" aria-hidden="true" />{c.loadFailed}</CardTitle>
          <CardDescription>{llm.errorMessage ?? c.loadFailed}</CardDescription>
        </CardHeader>
        <CardContent><Button className="min-touch" onClick={() => void llm.load()}><RefreshCwIcon aria-hidden="true" />{t.common.retry}</Button></CardContent>
      </Card>
    );
  }

  const feedback = llm.feedback?.tier === selectedTier && llm.feedback.provider === effectiveProviderId
    ? llm.feedback : null;
  const feedbackText = feedback?.kind === "saved" ? c.keySavedOnly
    : feedback?.kind === "removed" ? c.keyRemovedOnly
    : feedback?.kind === "envAfterRemove" ? c.envAfterRemove
    : feedback?.kind === "notRemoved" ? c.keyNotRemoved
    : feedback?.kind === "failed" ? c.credentialFailed : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><KeyRoundIcon className="size-4" aria-hidden="true" />{c.title}</CardTitle>
        <CardDescription>{c.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex flex-wrap gap-2" role="group" aria-label={c.tierSelector}>
          {(["quick", "deep"] as const).map((tier) => (
            <Button
              key={tier}
              type="button"
              aria-pressed={tier === selectedTier}
              variant={tier === selectedTier ? "default" : "outline"}
              className="min-touch"
              disabled={keyBusy}
              onClick={() => setSelectedTier(tier)}
            >
              {tier === "quick" ? c.quickTier : c.deepTier}{tierHasErrors(tier) ? ` - ${c.hasErrors}` : ""}
            </Button>
          ))}
        </div>

        <section className="space-y-4" aria-labelledby="llm-tier-heading">
          <div>
            <h3 id="llm-tier-heading" className="text-sm font-semibold">{selectedTierLabel}</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">{selectedTier === "quick" ? c.quickTierHint : c.deepTierHint}</p>
          </div>
          <SelectField
            id={`llm-${selectedTier}-provider`}
            label={c.tierProvider(selectedTierLabel)}
            value={tierProviderOverride}
            options={tierProviderOptions}
            disabled={keyBusy}
            onChange={(value) => controller.setValue(settingKey(selectedTier, "provider"), value)}
            hint={c.tierProviderHint}
            error={errors[settingKey(selectedTier, "provider")]}
          />
          {pendingProviderChange ? <p className="text-xs text-muted-foreground">{c.providerChanged}</p> : null}
          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              id={`llm-${selectedTier}-model`}
              label={c.model}
              value={modelSelectValue}
              options={[
                ...modelOptions.map((option) => ({ value: option.id, label: option.label })),
                ...(modelOptions.some((option) => option.id === "custom") ? [] : [{ value: "custom", label: c.customModel }]),
              ]}
              onChange={(value) => controller.setValue(tierModelKey, value === "custom" ? "" : value)}
              hint={selectedProviderLabel}
              error={modelSelectValue === "custom" ? undefined : errors[tierModelKey]}
            />
            {modelSelectValue === "custom" ? (
              <TextField
                id={`llm-${selectedTier}-custom-model`}
                label={c.customModel}
                value={currentModel}
                placeholder={c.customModelPlaceholder}
                onChange={(value) => controller.setValue(tierModelKey, value)}
                error={errors[tierModelKey]}
              />
            ) : null}
          </div>

          <div className="rounded-lg border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">{c.apiKeyTitle}</p>
                <p className="mt-0.5 text-xs text-muted-foreground">{c.keyScope(selectedTierLabel, selectedProviderLabel)}</p>
                {providerIsDraft ? <p className="mt-1 text-xs text-destructive">{c.providerDraft(selectedTierLabel, selectedProviderLabel)}</p> : null}
              </div>
              {selectedCredential ? <KeyStatus credential={selectedCredential} /> : null}
            </div>
            {selectedCredential?.supported ? (
              <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-end">
                <TextField
                  id={`llm-${selectedTier}-api-key`}
                  label={c.apiKeyInput}
                  value={keyDraft}
                  placeholder={selectedCredential.configured ? c.apiKeyReplacePlaceholder : c.apiKeyPlaceholder}
                  onChange={(value) => llm.setKeyDraft(selectedTier, effectiveProviderId, value)}
                  disabled={keyBusy}
                  type="password"
                  hint={selectedCredential.source === "env" ? c.keyFromEnv : selectedCredential.source === "vault" ? c.keyFromVault : undefined}
                />
                <Button type="button" className="min-touch" disabled={credentialUnavailable || !keyDraft.trim()} onClick={() => void llm.saveCredential(selectedTier, effectiveProviderId)}>
                  {keyBusy ? <LoaderCircleIcon className="animate-spin" aria-hidden="true" /> : <SaveIcon aria-hidden="true" />}{c.apiKeySave}
                </Button>
                {selectedCredential.source === "vault" ? (
                  <Button
                    type="button"
                    variant="destructive"
                    className="min-touch"
                    disabled={credentialUnavailable}
                    onClick={() => { if (window.confirm(c.apiKeyRemoveConfirm)) void llm.removeCredential(selectedTier, effectiveProviderId); }}
                    aria-label={c.apiKeyRemove}
                  ><Trash2Icon aria-hidden="true" />{c.apiKeyRemove}</Button>
                ) : null}
              </div>
            ) : <p className="mt-3 text-xs text-muted-foreground">{selectedCredential?.required ? c.keyRequiredElsewhere : c.keyManagedElsewhere}</p>}
            {feedbackText ? <p role={feedback?.kind === "failed" || feedback?.kind === "notRemoved" ? "alert" : "status"} className="mt-3 text-sm">{feedbackText}</p> : null}
          </div>

          <details open={advancedOpen || advancedHasErrors} onToggle={(event) => {
            if (!advancedHasErrors) setAdvancedOpen(event.currentTarget.open);
          }}>
            <summary className="min-touch cursor-pointer text-sm font-medium focus-visible:outline-2 focus-visible:outline-ring">
              {c.advanced}{advancedHasErrors ? ` - ${c.hasErrors}` : ""}
            </summary>
            <div className="space-y-4 pt-3">
              <SelectField
                id="llm-default-provider"
                label={c.defaultProvider}
                value={defaultProviderId}
                options={providerOptions}
                disabled={keyBusy}
                onChange={(value) => controller.setValue("llm_provider", value)}
                hint={c.defaultProviderHint}
                error={errors.llm_provider}
              />
              <TextField
                id="llm-default-backend-url"
                label={c.defaultBackendUrl}
                value={currentDefaultUrl}
                placeholder={defaultProvider?.default_backend_url ?? c.backendUrlPlaceholder}
                onChange={(value) => controller.setValue("backend_url", value)}
                hint={c.defaultUrlHint}
                error={errors.backend_url}
              />
              <TextField
                id={`llm-${selectedTier}-backend-url`}
                label={c.tierBackendUrl(selectedTierLabel)}
                value={currentTierUrl}
                placeholder={effectiveProvider?.default_backend_url ?? c.backendUrlPlaceholder}
                onChange={(value) => controller.setValue(tierBackendUrlKey, value)}
                hint={effectiveProviderId === defaultProviderId ? c.tierUrlInherited : c.tierUrlSeparate}
                error={errors[tierBackendUrlKey]}
              />
            </div>
          </details>

          {selectedReasoningKey ? (
            <TextField
              id={`llm-${selectedTier}-reasoning`}
              label={c.reasoningLabel(effectiveProviderId)}
              value={textValue(values, selectedReasoningKey)}
              placeholder={c.reasoningPlaceholder}
              onChange={(value) => controller.setValue(selectedReasoningKey, value)}
              hint={c.reasoningShared}
              error={errors[selectedReasoningKey]}
            />
          ) : null}
          {hiddenReasoningErrors.length ? (
            <div role="alert" className="space-y-1 text-xs text-destructive">
              <p>{c.fieldErrors}</p>
              {hiddenReasoningErrors.map((key) => <p key={key}>{t.settings.settingMeta[key]?.label ?? key}: {errors[key]}</p>)}
            </div>
          ) : null}
        </section>

        {llm.errorMessage ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
            <span>{c.refreshFailed}</span>
            <Button type="button" variant="outline" size="sm" onClick={() => void llm.load()}><RefreshCwIcon aria-hidden="true" />{t.common.retry}</Button>
          </div>
        ) : null}
        <p className="text-xs text-muted-foreground">{c.saveHint}</p>
      </CardContent>
    </Card>
  );
}
