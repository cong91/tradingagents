// Tầng lỗi chung cho mọi fetch của control panel.
//
// Hợp đồng docs/ui-api-contract.md §0.1 quy định HTTP error luôn mang envelope
// { error: { code, message, details } }; FastAPI chuẩn trả { detail: "..." }
// khi route không tồn tại. Cả hai shape được chuẩn hoá ở đúng MỘT chỗ này,
// thay vì mỗi màn tự viết một bản parse (trước đây bị nhân bản ~6 lần và chỉ
// một bản xử lý được {detail}).

export type ParsedErrorBody = {
  /** Code máy chủ trả, hoặc null khi body không phải JSON / chỉ có {detail}. */
  code: string | null;
  message: string;
  details: Record<string, unknown>;
};

/** Mọi throw của fetch ở client: mạng đứt, backend chưa chạy, hoặc HTTP !ok. */
export class ApiRequestError extends Error {
  /** 0 = fetch không nhận được response (mạng/backend không tới được). */
  readonly status: number;
  readonly code: string | null;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string | null,
    message: string,
    details: Record<string, unknown> = {}
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export const NETWORK_HINT =
  "Không kết nối được backend FastAPI (port 8000) — chạy uvicorn server.main:app rồi bấm Thử lại.";

/** Parse body lỗi: envelope §0.1, {detail} của FastAPI, hoặc fallback theo status. */
export async function parseErrorResponse(res: Response): Promise<ParsedErrorBody> {
  const fallback: ParsedErrorBody = {
    code: null,
    message: `Máy chủ trả lỗi HTTP ${res.status}.`,
    details: {},
  };
  try {
    const body: unknown = await res.json();
    if (body === null || typeof body !== "object") return fallback;
    const obj = body as Record<string, unknown>;
    const err = obj.error;
    if (err !== null && typeof err === "object") {
      const e = err as Record<string, unknown>;
      return {
        code: typeof e.code === "string" ? e.code : null,
        message: typeof e.message === "string" ? e.message : fallback.message,
        details:
          e.details !== null && typeof e.details === "object"
            ? (e.details as Record<string, unknown>)
            : {},
      };
    }
    // FastAPI mặc định trả {detail: "..."} khi route không tồn tại.
    if (typeof obj.detail === "string") {
      return { code: null, message: obj.detail, details: {} };
    }
    return fallback;
  } catch {
    // Body không phải JSON (vd lỗi proxy) — giữ fallback theo status.
    return fallback;
  }
}

/** Chuẩn hoá mọi throw của fetch thành ApiRequestError (network). */
export function toRequestError(err: unknown): ApiRequestError {
  if (err instanceof ApiRequestError) return err;
  return new ApiRequestError(0, "network", NETWORK_HINT);
}
