// Settings → "Data source cookies" card (Reddit / X session cookies).

type ProviderCopy = { title: string; description: string };

export const vi = {
  title: "Cookie nguồn dữ liệu",
  description:
    "Reddit và X chỉ trả dữ liệu đầy đủ khi có phiên đăng nhập. Cookie được mã hoá trước khi lưu PostgreSQL và không bao giờ hiển thị lại — chỉ tên và số lượng.",
  providers: {
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
  } satisfies Record<"reddit" | "x", ProviderCopy>,
  invalidJson:
    "Nội dung không phải JSON hợp lệ. Dán nguyên văn export từ extension (bắt đầu bằng [ hoặc {).",
  saved: (provider: string) => `Đã lưu cookie ${provider}.`,
  removed: (provider: string) => `Đã xoá cookie ${provider}.`,
  undecryptable: "Không giải mã được — import lại",
  configured: (n: number) => `Đã cấu hình · ${n} cookie`,
  missingRequired: "Thiếu cookie bắt buộc — import lại",
  notConfigured: "Chưa cấu hình",
  storedCookies: "Cookie đã lưu",
  updated: "Cập nhật",
  pasteLabel: "Dán JSON cookie (Cookie-Editor / EditThisCookie export)",
  save: "Lưu cookie",
  remove: "Xoá",
  loading: "Đang tải trạng thái cookie…",
  keyPathPrefix: "Khoá mã hoá:",
  keyPathSuffix: "— nằm ngoài database; mất khoá này thì phải import lại cookie.",
};

export const en: typeof vi = {
  title: "Data source cookies",
  description:
    "Reddit and X only return full data with a signed-in session. Cookies are encrypted before being stored in PostgreSQL and are never shown again — only names and counts.",
  providers: {
    reddit: {
      title: "Reddit",
      description:
        "A session cookie enables JSON search (with scores and comment counts). Without it the system falls back to public RSS, limited to ~1 request/minute and without scores.",
    },
    x: {
      title: "X (Twitter)",
      description:
        "X needs a session cookie with auth_token and ct0. Paste the cookie JSON from x.com; the system handles search with the stored session.",
    },
  },
  invalidJson:
    "The content is not valid JSON. Paste the extension export verbatim (starting with [ or {).",
  saved: (provider) => `Saved ${provider} cookies.`,
  removed: (provider) => `Removed ${provider} cookies.`,
  undecryptable: "Cannot decrypt — import again",
  configured: (n) => `Configured · ${n} ${n === 1 ? "cookie" : "cookies"}`,
  missingRequired: "Required cookies missing — import again",
  notConfigured: "Not configured",
  storedCookies: "Stored cookies",
  updated: "Updated",
  pasteLabel: "Paste cookie JSON (Cookie-Editor / EditThisCookie export)",
  save: "Save cookies",
  remove: "Remove",
  loading: "Loading cookie status…",
  keyPathPrefix: "Encryption key:",
  keyPathSuffix: "— stored outside the database; if this key is lost, cookies must be imported again.",
};
