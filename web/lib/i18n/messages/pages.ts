// Page headers (title + lead paragraph) for every route.

type PageCopy = { title: string; description: string };

export const vi = {
  overview: {
    title: "Tổng quan lợi nhuận",
    description:
      "Alpha tích luỹ của các quyết định đã chốt, hiệu suất theo thời gian và lệnh thực thi gần đây.",
  },
  approvals: {
    title: "Duyệt lệnh",
    description:
      "Hàng đợi phê duyệt kế hoạch lệnh do vòng quét hằng ngày của bot tạo ra. Duyệt là con đường duy nhất đưa lệnh vào thực thi; từ chối không đụng sàn.",
  },
  audit: {
    title: "Lịch sử & quyết định",
    description:
      "Nhật ký thực thi của bot (các bước của cùng một lệnh live gộp chung một hàng), quyết định đã lưu và tổng kết backtest. Trang chỉ đọc — mọi hành động đặt lệnh nằm ở Duyệt lệnh.",
  },
  cycles: {
    title: "Hoạt động bot",
    description:
      "Mọi thứ bot đang làm: chạy phân tích, danh sách theo dõi, vòng quét tự động và kết quả từng chu kỳ. Click một mã để xem lý do được triển khai hoặc bị loại.",
  },
  settings: {
    title: "Cấu hình",
    description:
      "Cấu hình mô hình phân tích, giới hạn rủi ro và xem trạng thái các cổng an toàn. Thay đổi áp dụng cho phiên phân tích mới; khóa API và cổng «Lệnh thật» chỉ xem, không bật được từ đây.",
  },
} satisfies Record<string, PageCopy>;

export const en: typeof vi = {
  overview: {
    title: "P&L overview",
    description:
      "Cumulative alpha of resolved decisions, performance over time and recent executed orders.",
  },
  approvals: {
    title: "Order approvals",
    description:
      "Approval queue for order plans produced by the bot's daily scan. Approving is the only path to execution; rejecting never touches the exchange.",
  },
  audit: {
    title: "History & decisions",
    description:
      "The bot's execution log (steps of the same live order grouped into one row), saved decisions and backtest summaries. Read-only — every order action lives on Approvals.",
  },
  cycles: {
    title: "Bot activity",
    description:
      "Everything the bot is doing: run analyses, manage the watchlist, the automatic scan loop and the outcome of every cycle. Click a symbol to see why it was deployed or rejected.",
  },
  settings: {
    title: "Settings",
    description:
      "Configure the analysis models, risk limits and view the safety gates. Changes apply to new analysis runs; API keys and the \"Real orders\" gate are read-only here.",
  },
};

export type PageKey = keyof typeof vi;
