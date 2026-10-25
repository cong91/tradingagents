// Page headers (title + lead paragraph) for every route.

type PageCopy = { title: string; description: string };

export const vi = {
  overview: {
    title: "Tổng quan lợi nhuận",
    description:
      "Alpha tích luỹ của các quyết định đã chốt, hiệu suất theo thời gian và lệnh thực thi gần đây — đọc trực tiếp từ backend qua /api/*.",
  },
  approvals: {
    title: "Duyệt lệnh",
    description:
      "Hàng đợi phê duyệt kế hoạch lệnh từ pipeline hằng ngày. Duyệt là con đường duy nhất đưa lệnh vào thực thi; từ chối không đụng sàn.",
  },
  audit: {
    title: "Lịch sử & Audit",
    description:
      "Nhật ký kiểm soát thực thi, quyết định đã lưu và tổng kết backtest. Màn chỉ đọc — mọi hành động đặt lệnh nằm ở màn Duyệt lệnh.",
  },
  pipeline: {
    title: "Agent Pipeline",
    description:
      "Bắt đầu một phiên phân tích (POST /api/runs) và theo dõi luồng agent theo thời gian thực qua SSE: analyst → tranh luận → trader → rủi ro → quyết định cuối.",
  },
  scanner: {
    title: "Tìm cặp giao dịch",
    description:
      "Quản lý danh sách theo dõi (watchlist) và chạy quét pipeline cho từng mã trước khi đưa vào hàng duyệt lệnh.",
  },
  settings: {
    title: "Cấu hình",
    description:
      "Đọc và ghi cài đặt engine qua GET / PUT /api/settings. Thay đổi áp dụng cho phiên phân tích mới; khóa API và cổng LIVE chỉ-đọc theo thiết kế.",
  },
} satisfies Record<string, PageCopy>;

export const en: typeof vi = {
  overview: {
    title: "P&L overview",
    description:
      "Cumulative alpha of resolved decisions, performance over time and recent executed orders — read live from the backend via /api/*.",
  },
  approvals: {
    title: "Order approvals",
    description:
      "Approval queue for order plans produced by the daily pipeline. Approving is the only path to execution; rejecting never touches the exchange.",
  },
  audit: {
    title: "History & Audit",
    description:
      "Execution control log, saved decisions and backtest summaries. Read-only screen — every order action lives on the Order approvals screen.",
  },
  pipeline: {
    title: "Agent Pipeline",
    description:
      "Start an analysis run (POST /api/runs) and follow the agent flow in real time over SSE: analysts → debate → trader → risk → final decision.",
  },
  scanner: {
    title: "Pair scanner",
    description:
      "Manage the watchlist and run pipeline scans per ticker before they enter the approval queue.",
  },
  settings: {
    title: "Settings",
    description:
      "Read and write engine settings via GET / PUT /api/settings. Changes apply to new analysis runs; API keys and the LIVE gate are read-only by design.",
  },
};

export type PageKey = keyof typeof vi;
