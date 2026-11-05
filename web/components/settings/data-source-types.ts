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

// Provider titles/descriptions live in lib/i18n/messages/data-sources.ts.
export const PROVIDER_ORDER: readonly DataSourceProvider[] = ["reddit", "x"];
