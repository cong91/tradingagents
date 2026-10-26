// Fetch cho thẻ "Cookie nguồn dữ liệu" (Reddit, X).
import { NETWORK_HINT, parseErrorResponse } from "@/lib/api";

import type { DataSourceProvider, DataSourcesPayload } from "./data-source-types";

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

export class DataSourceApiError extends Error {
  status: number;
  code: string | null;

  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "DataSourceApiError";
    this.status = status;
    this.code = code;
  }
}

export function toDataSourceError(err: unknown): DataSourceApiError {
  if (err instanceof DataSourceApiError) return err;
  return new DataSourceApiError(0, "network", NETWORK_HINT);
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw toDataSourceError(err);
  }
  if (!res.ok) {
    const body = await parseErrorResponse(res);
    throw new DataSourceApiError(res.status, body.code, body.message);
  }
  return (await res.json()) as T;
}

export const dataSourceApi = {
  get(): Promise<DataSourcesPayload> {
    return requestJson<DataSourcesPayload>("/api/data-sources", { cache: "no-store" });
  },

  /**
   * `cookies` là nội dung JSON dán từ extension (Cookie-Editor/EditThisCookie):
   * mảng {name, value}, hoặc map {name: value}. Server chuẩn hoá và mã hoá.
   */
  saveCookies(
    provider: DataSourceProvider,
    cookies: unknown,
    options?: Record<string, string>
  ): Promise<DataSourcesPayload> {
    return requestJson<DataSourcesPayload>(
      `/api/data-sources/${encodeURIComponent(provider)}`,
      {
        method: "PUT",
        headers: JSON_HEADERS,
        body: JSON.stringify({ cookies, options: options ?? {} }),
      }
    );
  },

  remove(provider: DataSourceProvider): Promise<DataSourcesPayload & { removed: boolean }> {
    return requestJson<DataSourcesPayload & { removed: boolean }>(
      `/api/data-sources/${encodeURIComponent(provider)}`,
      { method: "DELETE", headers: JSON_HEADERS, body: "{}" }
    );
  },
};
