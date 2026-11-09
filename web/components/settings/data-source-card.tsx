"use client";

// Thẻ nhập cookie phiên cho Reddit và X: dán JSON export từ extension, server
// chuẩn hoá + mã hoá rồi lưu PostgreSQL. Chỉ trạng thái được hiển thị lại —
// không bao giờ có giá trị cookie, đúng quy tắc của thẻ Khóa API.

import { useCallback, useEffect, useState } from "react";
import { CookieIcon, LoaderCircleIcon, Trash2Icon, UploadIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  dataSourceApi,
  DataSourceApiError,
  toDataSourceError,
} from "./data-source-api";
import { useT } from "@/lib/i18n";
import {
  PROVIDER_ORDER,
  type DataSourceSummary,
  type DataSourcesPayload,
} from "./data-source-types";
import { formatDateTimeVi } from "./format-time";

type Feedback = { role: "status" | "alert"; text: string };

function ProviderCard({
  summary,
  onSaved,
  onRemoved,
  onDirtyChange,
}: {
  summary: DataSourceSummary;
  onSaved: (payload: DataSourcesPayload) => void;
  onRemoved: (provider: DataSourceSummary["provider"]) => void;
  onDirtyChange: (provider: string, dirty: boolean) => void;
}) {
  const t = useT();
  const c = t.dataSources;
  const meta = c.providers[summary.provider];
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  const save = useCallback(async () => {
    setFeedback(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      setFeedback({
        role: "alert",
        text: c.invalidJson,
      });
      return;
    }
    setBusy(true);
    try {
      const payload = await dataSourceApi.saveCookies(summary.provider, parsed);
      onSaved(payload);
      setRaw("");
      onDirtyChange(summary.provider, false);
      setFeedback({ role: "status", text: c.saved(meta.title) });
    } catch (err) {
      const failure = err instanceof DataSourceApiError ? err : toDataSourceError(err);
      setFeedback({ role: "alert", text: failure.message });
    } finally {
      setBusy(false);
    }
  }, [c, meta.title, onDirtyChange, onSaved, raw, summary.provider]);

  const remove = useCallback(async () => {
    const confirmation = raw.length > 0
      ? `${c.removeConfirm(meta.title)}\n\n${c.discardBeforeRemove}`
      : c.removeConfirm(meta.title);
    if (!window.confirm(confirmation)) return;
    setFeedback(null);
    setBusy(true);
    try {
      const payload = await dataSourceApi.remove(summary.provider);
      const providers = Array.isArray(payload?.providers) ? payload.providers : [];
      const matching = providers.filter((item) => item?.provider === summary.provider);
      const confirmed = payload?.removed === true || (
        payload?.removed === undefined && matching.length === 1 && matching[0].configured === false
      );
      if (!confirmed) {
        setFeedback({ role: "alert", text: c.removeUnconfirmed(meta.title) });
        return;
      }
      const completeSnapshot = typeof payload.generated_at === "string" &&
        typeof payload.key_path === "string" && providers.length === PROVIDER_ORDER.length &&
        matching.length === 1 && matching[0].configured === false &&
        PROVIDER_ORDER.every((provider) => {
          const matches = providers.filter((item) => item?.provider === provider);
          const item = matches[0];
          return matches.length === 1 && typeof item.configured === "boolean" &&
            typeof item.decryptable === "boolean" && typeof item.valid === "boolean" &&
            typeof item.cookie_count === "number" && Array.isArray(item.cookie_names) &&
            item.cookie_names.every((name) => typeof name === "string") &&
            (item.updated_at === null || typeof item.updated_at === "string");
        });
      setRaw("");
      onDirtyChange(summary.provider, false);
      if (completeSnapshot) onSaved(payload);
      else onRemoved(summary.provider);
      setFeedback({ role: "status", text: c.removed(meta.title) });
    } catch {
      setFeedback({ role: "alert", text: c.removeUnconfirmed(meta.title) });
    } finally {
      setBusy(false);
    }
  }, [c, meta.title, onDirtyChange, onRemoved, onSaved, raw, summary.provider]);

  return (
    <div className="space-y-3 border-t pt-3 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{meta.title}</p>
        {summary.configured ? (
          !summary.decryptable ? (
            <Badge variant="destructive">{c.undecryptable}</Badge>
          ) : summary.valid ? (
            <Badge variant="secondary">{c.configured(summary.cookie_count)}</Badge>
          ) : (
            <Badge variant="destructive">{c.missingRequired}</Badge>
          )
        ) : (
          <Badge variant="outline">{c.notConfigured}</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{meta.description}</p>

      {summary.configured ? (
        <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground">{c.storedCookies}</dt>
            <dd className="font-mono break-all">
              {summary.cookie_names.length > 0 ? summary.cookie_names.join(", ") : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">{c.updated}</dt>
            <dd>
              {summary.updated_at ? formatDateTimeVi(summary.updated_at) : "—"}
            </dd>
          </div>
        </dl>
      ) : null}

      <label className="block text-xs">
        <span className="text-muted-foreground">{c.pasteLabel}</span>
        <textarea
          value={raw}
          disabled={busy}
          onChange={(event) => {
            const value = event.target.value;
            setRaw(value);
            onDirtyChange(summary.provider, value.trim().length > 0);
          }}
          rows={4}
          spellCheck={false}
          placeholder={'[{"name":"auth_token","value":"…"},{"name":"ct0","value":"…"}]'}
          className="mt-1 w-full min-w-0 rounded-lg border border-input bg-background p-3 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60"
        />
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          className="min-touch"
          disabled={busy || raw.trim().length === 0}
          onClick={() => void save()}
        >
          {busy ? (
            <LoaderCircleIcon className="animate-spin" aria-hidden="true" />
          ) : (
            <UploadIcon aria-hidden="true" />
          )}
          {c.save}
        </Button>
        {summary.configured ? (
          <Button
            variant="outline"
            size="sm"
            className="min-touch"
            disabled={busy}
            onClick={() => void remove()}
          >
            <Trash2Icon aria-hidden="true" />
            {c.remove}
          </Button>
        ) : null}
        {feedback ? (
          <p
            role={feedback.role}
            className={
              feedback.role === "alert"
                ? "text-xs text-destructive"
                : "text-xs text-muted-foreground"
            }
          >
            {feedback.text}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function DataSourceCard({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const t = useT();
  const c = t.dataSources;
  const [payload, setPayload] = useState<DataSourcesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [dirtyProviders, setDirtyProviders] = useState<Record<string, boolean>>({});

  useEffect(() => {
    onDirtyChange?.(Object.values(dirtyProviders).some(Boolean));
  }, [dirtyProviders, onDirtyChange]);

  const updateDirty = useCallback((provider: string, dirty: boolean) => {
    setDirtyProviders((previous) => ({ ...previous, [provider]: dirty }));
  }, []);

  const markRemoved = useCallback((provider: DataSourceSummary["provider"]) => {
    setPayload((previous) => previous && ({
      ...previous,
      providers: previous.providers.map((item) => item.provider === provider
        ? { ...item, configured: false, decryptable: false, valid: false,
            cookie_count: 0, cookie_names: [], updated_at: null, updated_by: null }
        : item),
    }));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPayload(await dataSourceApi.get());
      setError(null);
    } catch (err) {
      const failure = err instanceof DataSourceApiError ? err : toDataSourceError(err);
      setError(failure.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Nạp lần đầu trong callback (không setState đồng bộ trong effect body), cùng
  // pattern với AutoloopCard.
  useEffect(() => {
    const initialId = setTimeout(() => void load(), 0);
    return () => clearTimeout(initialId);
  }, [load]);

  const byProvider = new Map(
    (payload?.providers ?? []).map((item) => [item.provider, item])
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CookieIcon className="size-4" aria-hidden="true" />
          {c.title}
        </CardTitle>
        <CardDescription>{c.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
            {c.loading}
          </p>
        ) : null}
        {error ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>{error}</p>
            <Button variant="outline" size="sm" className="min-touch" onClick={() => void load()}>
              {t.common.retry}
            </Button>
          </div>
        ) : null}
        {payload
          ? PROVIDER_ORDER.map((provider) => {
              const summary = byProvider.get(provider);
              if (!summary) return null;
              return (
                <ProviderCard
                  key={provider}
                  summary={summary}
                  onSaved={setPayload}
                  onRemoved={markRemoved}
                  onDirtyChange={updateDirty}
                />
              );
            })
          : null}
        {payload ? (
          <p className="text-[11px] text-muted-foreground">
            {c.keyPathPrefix} <span className="font-mono break-all">{payload.key_path}</span>{" "}
            {c.keyPathSuffix}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
