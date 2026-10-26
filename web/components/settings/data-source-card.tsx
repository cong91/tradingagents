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
import {
  PROVIDER_META,
  PROVIDER_ORDER,
  type DataSourceSummary,
  type DataSourcesPayload,
} from "./data-source-types";
import { formatDateTimeVi } from "./format-time";

type Feedback = { role: "status" | "alert"; text: string };

function ProviderCard({
  summary,
  onSaved,
}: {
  summary: DataSourceSummary;
  onSaved: (payload: DataSourcesPayload) => void;
}) {
  const meta = PROVIDER_META[summary.provider];
  const [raw, setRaw] = useState("");
  const [options, setOptions] = useState<Record<string, string>>({});
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
        text: "Nội dung không phải JSON hợp lệ. Dán nguyên văn export từ extension (bắt đầu bằng [ hoặc {).",
      });
      return;
    }
    setBusy(true);
    try {
      const payload = await dataSourceApi.saveCookies(summary.provider, parsed, options);
      onSaved(payload);
      setRaw("");
      setFeedback({ role: "status", text: `Đã lưu cookie ${meta.title}.` });
    } catch (err) {
      const failure = err instanceof DataSourceApiError ? err : toDataSourceError(err);
      setFeedback({ role: "alert", text: failure.message });
    } finally {
      setBusy(false);
    }
  }, [meta.title, onSaved, options, raw, summary.provider]);

  const remove = useCallback(async () => {
    setBusy(true);
    setFeedback(null);
    try {
      const payload = await dataSourceApi.remove(summary.provider);
      onSaved(payload);
      setFeedback({ role: "status", text: `Đã xoá cookie ${meta.title}.` });
    } catch (err) {
      const failure = err instanceof DataSourceApiError ? err : toDataSourceError(err);
      setFeedback({ role: "alert", text: failure.message });
    } finally {
      setBusy(false);
    }
  }, [meta.title, onSaved, summary.provider]);

  return (
    <div className="space-y-3 border-t pt-3 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{meta.title}</p>
        {summary.configured ? (
          summary.decryptable ? (
            <Badge variant="secondary">
              Đã cấu hình · {summary.cookie_count} cookie
            </Badge>
          ) : (
            <Badge variant="destructive">Không giải mã được — import lại</Badge>
          )
        ) : (
          <Badge variant="outline">Chưa cấu hình</Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{meta.description}</p>

      {summary.configured ? (
        <dl className="grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
          <div>
            <dt className="text-muted-foreground">Cookie đã lưu</dt>
            <dd className="font-mono break-all">
              {summary.cookie_names.length > 0 ? summary.cookie_names.join(", ") : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Tuỳ chọn</dt>
            <dd className="font-mono break-all">
              {summary.option_keys.length > 0
                ? summary.option_keys
                    .map((key) =>
                      summary.secret_options.includes(key) ? `${key} (đã lưu)` : key
                    )
                    .join(", ")
                : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Cập nhật</dt>
            <dd>
              {summary.updated_at ? formatDateTimeVi(summary.updated_at) : "—"}
            </dd>
          </div>
        </dl>
      ) : null}

      {meta.options.length > 0 ? (
        <div className="grid gap-2 sm:grid-cols-2">
          {meta.options.map((option) => (
            <label key={option.key} className="block text-xs">
              <span className="text-muted-foreground">{option.label}</span>
              <input
                type={option.key === "bearer_token" ? "password" : "text"}
                value={options[option.key] ?? ""}
                disabled={busy}
                onChange={(event) =>
                  setOptions((prev) => ({ ...prev, [option.key]: event.target.value }))
                }
                className="mt-1 h-10 w-full min-w-0 rounded-lg border border-input bg-background px-3 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60"
                autoComplete="off"
              />
              {option.hint ? (
                <span className="mt-1 block text-[11px] leading-snug text-muted-foreground">
                  {option.hint}
                </span>
              ) : null}
            </label>
          ))}
        </div>
      ) : null}

      <label className="block text-xs">
        <span className="text-muted-foreground">
          Dán JSON cookie (Cookie-Editor / EditThisCookie export)
        </span>
        <textarea
          value={raw}
          disabled={busy}
          onChange={(event) => setRaw(event.target.value)}
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
          Lưu cookie
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
            Xoá
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

export function DataSourceCard() {
  const [payload, setPayload] = useState<DataSourcesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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
          Cookie nguồn dữ liệu
        </CardTitle>
        <CardDescription>
          Reddit và X chỉ trả dữ liệu đầy đủ khi có phiên đăng nhập. Cookie được mã hoá
          trước khi lưu PostgreSQL và không bao giờ hiển thị lại — chỉ tên và số lượng.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <LoaderCircleIcon className="size-4 animate-spin" aria-hidden="true" />
            Đang tải trạng thái cookie…
          </p>
        ) : null}
        {error ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>{error}</p>
            <Button variant="outline" size="sm" className="min-touch" onClick={() => void load()}>
              Thử lại
            </Button>
          </div>
        ) : null}
        {payload
          ? PROVIDER_ORDER.map((provider) => {
              const summary = byProvider.get(provider);
              if (!summary) return null;
              return (
                <ProviderCard key={provider} summary={summary} onSaved={setPayload} />
              );
            })
          : null}
        {payload ? (
          <p className="text-[11px] text-muted-foreground">
            Khoá mã hoá: <span className="font-mono break-all">{payload.key_path}</span> — nằm
            ngoài database; mất khoá này thì phải import lại cookie.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
