"use client";

// Hook fetch một section chỉ-đọc với 3 trạng thái hạng nhất (loading/ready/error),
// tuân thủ §0.2 hợp đồng: URL đổi → dữ liệu cũ bị coi là hết hạn (loading) cho tới
// khi response về; "Làm mới" giữ dữ liệu cũ trên màn (không nháy trắng).
//
// Trạng thái loading KHÔNG được ghi bằng setState trong effect — nó được suy ra
// (derive) từ việc state đã lưu có khớp url hiện tại hay không (pattern React:
// derive state thay vì sync trong effect).

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError, parseErrorResponse } from "@/lib/api";

export type SectionError = { code: string | null; message: string };

export type SectionState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T; fetchedAt: number }
  | { status: "error"; error: SectionError };

type StoredState<T> =
  | { status: "idle" }
  | { status: "ready"; data: T; fetchedAt: number; url: string }
  | { status: "error"; error: SectionError; url: string };

async function fetchSection<T>(url: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch {
    throw new ApiRequestError(
      0,
      "network",
      "Không kết nối được máy chủ — backend FastAPI (port 8000) chưa chạy hoặc lỗi mạng."
    );
  }
  if (!res.ok) {
    // Parse envelope §0.1/{detail} ở lib/api.ts — chung cho cả 6 màn.
    const body = await parseErrorResponse(res);
    throw new ApiRequestError(res.status, body.code, body.message, body.details);
  }
  return (await res.json()) as T;
}

function toError(err: unknown): SectionError {
  if (err instanceof ApiRequestError) {
    return { code: err.code, message: err.message };
  }
  return {
    code: null,
    message: err instanceof Error ? err.message : "Lỗi không xác định.",
  };
}

export function useSectionData<T>(url: string): {
  state: SectionState<T>;
  refresh: () => void;
  refreshing: boolean;
} {
  const [stored, setStored] = useState<StoredState<T>>({ status: "idle" });
  const [refreshing, setRefreshing] = useState(false);
  // Chỉ response của lần gọi mới nhất được phép ghi state (chống race khi
  // người dùng đổi filter liên tiếp).
  const seqRef = useRef(0);

  useEffect(() => {
    const seq = ++seqRef.current;
    fetchSection<T>(url)
      .then((data) => {
        if (seqRef.current === seq) {
          setStored({ status: "ready", data, fetchedAt: Date.now(), url });
        }
      })
      .catch((err: unknown) => {
        if (seqRef.current === seq) {
          setStored({ status: "error", error: toError(err), url });
        }
      });
  }, [url]);

  const refresh = useCallback(() => {
    setRefreshing(true);
    const seq = ++seqRef.current;
    fetchSection<T>(url)
      .then((data) => {
        if (seqRef.current === seq) {
          setStored({ status: "ready", data, fetchedAt: Date.now(), url });
        }
      })
      .catch((err: unknown) => {
        if (seqRef.current === seq) {
          setStored({ status: "error", error: toError(err), url });
        }
      })
      .finally(() => setRefreshing(false));
  }, [url]);

  // Derive: state cũ của url khác → hiện loading, không nhảy lỗi/dữ liệu lệch.
  const state: SectionState<T> =
    stored.status === "ready" && stored.url === url
      ? { status: "ready", data: stored.data, fetchedAt: stored.fetchedAt }
      : stored.status === "error" && stored.url === url
        ? { status: "error", error: stored.error }
        : { status: "loading" };

  return { state, refresh, refreshing };
}
