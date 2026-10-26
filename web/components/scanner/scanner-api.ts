// Lớp fetch cho màn Scanner. Mọi POST/DELETE gửi kèm Content-Type: application/json
// kể cả body rỗng — quy tắc CSRF bắt buộc của backend (contract §0, server/main.py:48-65).
import type {
  ApiErrorInfo,
  DailyJobListResponse,
  DailyJobStart,
  DailyJobStatusResponse,
  FuturesCyclesResponse,
  WatchlistResponse,
} from "./scanner-types";
import { parseErrorResponse } from "@/lib/api";

const JSON_HEADERS = { "Content-Type": "application/json" } as const;

/** Lỗi HTTP/network chuẩn hoá để UI hiển thị thông điệp + nút retry. */
export class ApiHttpError extends Error implements ApiErrorInfo {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiHttpError";
    this.status = status;
    this.code = code;
  }
}

/** Chuẩn hoá mọi throw của fetch thành ApiHttpError. */
export function toApiHttpError(err: unknown): ApiHttpError {
  if (err instanceof ApiHttpError) return err;
  // fetch throw TypeError khi network đứt hoặc rewrite không tới được backend.
  return new ApiHttpError(
    0,
    "network",
    "Không kết nối được backend — hãy chắc chắn server FastAPI đang chạy (uvicorn server.main:app --port 8000).",
  );
}

async function parseHttpError(res: Response): Promise<ApiHttpError> {
  // Parse envelope §0.1/{detail} ở lib/api.ts — chung cho cả 6 màn.
  const body = await parseErrorResponse(res);
  return new ApiHttpError(res.status, body.code ?? `http_${res.status}`, body.message);
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    throw toApiHttpError(err);
  }
  if (!res.ok) throw await parseHttpError(res);
  return (await res.json()) as T;
}

export const scannerApi = {
  watchlist(): Promise<WatchlistResponse> {
    return requestJson<WatchlistResponse>("/api/watchlist");
  },
  addSymbol(symbol: string): Promise<WatchlistResponse> {
    return requestJson<WatchlistResponse>("/api/watchlist", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ symbol }),
    });
  },
  removeSymbol(symbol: string): Promise<WatchlistResponse> {
    return requestJson<WatchlistResponse>(
      `/api/watchlist/${encodeURIComponent(symbol)}`,
      { method: "DELETE" },
    );
  },
  /** Body tối thiểu `{}` (CSRF §0): bỏ tickers → server dùng watchlist hiện hành. */
  startDaily(): Promise<DailyJobStart> {
    return requestJson<DailyJobStart>("/api/daily", {
      method: "POST",
      headers: JSON_HEADERS,
      body: "{}",
    });
  },
  /** Quét tự động (rev 4): server tự sàng universe crypto + stock rồi chạy
   *  pipeline trên top N ứng viên — không cần watchlist. */
  startAutoDaily(topN: number): Promise<DailyJobStart> {
    return requestJson<DailyJobStart>("/api/daily", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ auto: true, top_n: topN }),
    });
  },
  dailyJob(jobId: string): Promise<DailyJobStatusResponse> {
    return requestJson<DailyJobStatusResponse>(
      `/api/daily/${encodeURIComponent(jobId)}`,
    );
  },
  /** Lịch sử job + job đang chạy (contract §5 rev 7). */
  dailyJobs(pageSize = 20): Promise<DailyJobListResponse> {
    return requestJson<DailyJobListResponse>(`/api/daily?page_size=${pageSize}`);
  },
  /** Có `symbol` thì chỉ lấy chu kỳ của mã đó (drill-down); tổng hợp
   *  summary/symbols luôn là toàn bộ dữ liệu. */
  futuresCycles(limit = 20, symbol?: string): Promise<FuturesCyclesResponse> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (symbol) params.set("symbol", symbol);
    return requestJson<FuturesCyclesResponse>(`/api/autoloop/cycles?${params}`, {
      cache: "no-store",
    });
  },
  /** Dừng job đang chạy — trả về ngay khi job được đánh dấu dừng, không chờ
   *  coin đang chạy dở kết thúc (một thread không thể bị kill an toàn). */
  cancelDailyJob(jobId: string): Promise<DailyJobStatusResponse> {
    return requestJson<DailyJobStatusResponse>(
      `/api/daily/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST", headers: JSON_HEADERS, body: "{}" },
    );
  },
};
