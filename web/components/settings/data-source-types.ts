// Shape của GET/PUT/DELETE /api/data-sources — mirror của server/data_sources.py.
// Cookie chỉ hiện dạng tên + số lượng; giá trị không bao giờ rời máy chủ.

export type DataSourceProvider = "reddit" | "x";

export type DataSourceSummary = {
  provider: DataSourceProvider;
  configured: boolean;
  /** false = key mã hoá đã đổi hoặc mất: phải import lại cookie. */
  decryptable: boolean;
  /** Cookie bắt buộc đã có; không xác nhận phiên còn hiệu lực trên nhà cung cấp. */
  valid: boolean;
  cookie_count: number;
  cookie_names: string[];
  updated_at: string | null;
  updated_by: string | null;
};

export type DataSourcesPayload = {
  generated_at: string;
  providers: DataSourceSummary[];
  key_path: string;
};

export const PROVIDER_META: Record<
  DataSourceProvider,
  { title: string; description: string }
> = {
  reddit: {
    title: "Reddit",
    description:
      "Cookie phiên đăng nhập cho phép tìm kiếm bằng JSON (có điểm và số bình luận). Không có cookie thì hệ thống dùng RSS công khai, bị giới hạn ~1 request/phút và không có điểm.",
  },
  x: {
    title: "X (Twitter)",
    description:
      "X cần cookie phiên đăng nhập gồm auth_token và ct0. Dán JSON cookie từ x.com; hệ thống tự xử lý tìm kiếm bằng phiên đã lưu.",
  },
};

export const PROVIDER_ORDER: readonly DataSourceProvider[] = ["reddit", "x"];
