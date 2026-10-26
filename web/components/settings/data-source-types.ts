// Shape của GET/PUT/DELETE /api/data-sources — mirror của server/data_sources.py.
// Cookie chỉ hiện dạng tên + số lượng; giá trị không bao giờ rời máy chủ.

export type DataSourceProvider = "reddit" | "x";

export type DataSourceSummary = {
  provider: DataSourceProvider;
  configured: boolean;
  /** false = key mã hoá đã đổi hoặc mất: phải import lại cookie. */
  decryptable: boolean;
  cookie_count: number;
  cookie_names: string[];
  option_keys: string[];
  /** Tên các option là bí mật (vd bearer_token) — chỉ hiện trạng thái. */
  secret_options: string[];
  updated_at: string | null;
  updated_by: string | null;
  supported_options: string[];
};

export type DataSourcesPayload = {
  generated_at: string;
  providers: DataSourceSummary[];
  key_path: string;
};

export const PROVIDER_META: Record<
  DataSourceProvider,
  { title: string; description: string; options: { key: string; label: string; hint?: string }[] }
> = {
  reddit: {
    title: "Reddit",
    description:
      "Cookie phiên đăng nhập cho phép tìm kiếm bằng JSON (có điểm và số bình luận). Không có cookie thì hệ thống dùng RSS công khai, bị giới hạn ~1 request/phút và không có điểm.",
    options: [],
  },
  x: {
    title: "X (Twitter)",
    description:
      "X từ chối mọi tìm kiếm ẩn danh. Dùng bearer token của API v2 (ổn định, có hạn mức) hoặc cookie phiên đăng nhập kèm graphql_query_id.",
    options: [
      {
        key: "bearer_token",
        label: "Bearer token (API v2)",
        hint: "Ưu tiên dùng nếu có. Bỏ trống thì hệ thống dùng cookie phiên.",
      },
      {
        key: "graphql_query_id",
        label: "GraphQL query id",
        hint: "Chỉ cần khi dùng cookie. X đổi id này mỗi lần deploy — chép lại từ x.com khi tìm kiếm báo lỗi 403.",
      },
    ],
  },
};

export const PROVIDER_ORDER: readonly DataSourceProvider[] = ["reddit", "x"];
