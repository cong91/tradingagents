// M4 "Order approvals" screen: queue, cards, dialogs, action errors.

export const vi = {
  status: {
    pending: "Chờ duyệt",
    approved: "Đã duyệt",
    rejected: "Đã từ chối",
    executed: "Đã thực thi",
    execute_failed: "Thực thi lỗi",
    expired: "Hết hạn",
  },
  sideBuy: "MUA",
  sideSell: "BÁN",
  sideUnknown: "Không rõ phía",
  signal: (signal: string) => `Tín hiệu: ${signal}`,
  planLive: "Lập kế hoạch ở chế độ LIVE",
  planDry: "Lập kế hoạch ở chế độ mô phỏng",
  mockPlan: "Kế hoạch mô phỏng (mock)",
  expiredBadge: "Quá hạn duyệt",
  errors: {
    mode_closed:
      "Auto Spot is disabled or this approval is no longer authorized. Enable Auto Spot in Settings, then try again.",
    gate_closed:
      "The standalone venue profile is unavailable for this operation. Check the venue URL and credentials, then try again.",
    risk_halted: (reason: string | null) =>
      `Bot đã dừng giao dịch hôm nay — không thể duyệt.${reason ? ` Lý do: ${reason}` : ""}`,
    conflict:
      "Kế hoạch đã được xử lý nơi khác (tab khác đã duyệt/từ chối). Danh sách sẽ được làm mới.",
    mock_not_executable:
      "Kế hoạch sinh từ job quét chế độ mô phỏng (mock) — dữ liệu tổng hợp không thể thành lệnh thật. Hãy từ chối kế hoạch này thay vì duyệt.",
    expired: "Kế hoạch đã quá hạn duyệt (24 giờ) và không còn hiệu lực.",
    audit_log_unreadable:
      "Không đọc được ghi nhận lịch sử — hệ thống cố ý từ chối duyệt khi thiếu ghi nhận (thiết kế an toàn). Kiểm tra quyền đọc file nhật ký ở máy chủ.",
    csrf_rejected: "Yêu cầu bị chặn CSRF — hãy mở bảng điều khiển qua localhost rồi thử lại.",
    unsupported_media_type: "Lỗi kỹ thuật: yêu cầu thiếu Content-Type application/json.",
    generic: (code: string, message: string) => `Lỗi (${code}): ${message}`,
    unknown: (code: string) => `Lỗi không xác định (${code}).`,
    badShape: "Phản hồi không đúng định dạng hợp đồng (thiếu items).",
    network: "Không kết nối được máy chủ — kiểm tra chương trình TradingAgents đã chạy chưa.",
    healthMissingHalted: "Phản hồi /api/health thiếu halted_today"
  },
  gate: {
    haltedTitle: "Bot đã dừng giao dịch hôm nay",
    haltedBody:
      "Mọi lệnh duyệt hôm nay sẽ bị máy chủ từ chối. Việc mở lại chỉ thực hiện được phía máy chủ — trang này cố ý không có nút mở lại.",
    healthError:
      "Không kiểm tra được trạng thái dừng giao dịch hôm nay — hãy bấm Làm mới.",
  },
  card: {
    proposedAt: (time: string) => `Đề xuất lúc ${time}`,
    deadline: (time: string) => `Hạn duyệt ${time}`,
    quantity: "Khối lượng",
    estPrice: "Giá ước tính",
    orderValue: "Giá trị lệnh",
    market: "Thị trường",
    leverage: (x: number) => ` · đòn bẩy ${x}×`,
    stopLoss: "Cắt lỗ (SL)",
    takeProfit: "Chốt lời (TP)",
    reason: (text: string) => `Lý do: ${text}`,
    reasonPrefix: "Lý do: ",
    reasonExpand: "Xem đầy đủ",
    reasonCollapse: "Thu gọn",
    reasonUnreadable: "Lỗi thực thi — chi tiết kỹ thuật không đọc được",
    mockNote:
      "Kế hoạch này sinh từ job quét chế độ mô phỏng — dữ liệu tổng hợp không thể thành lệnh thật: server sẽ từ chối duyệt (409), chỉ có thể từ chối kế hoạch.",
    approve: "Duyệt lệnh",
    reject: "Từ chối",
  },
  approveDialog: {
    title: (id: string) => `Xác nhận duyệt lệnh ${id}?`,
    liveDescription:
      "Chế độ \"Lệnh thật\" đang bật — kế hoạch sẽ đi thẳng vào đường gửi lệnh duy nhất của bot. Lệnh thật có thể được gửi lên sàn; hành động này không thể hoàn tác.",
    dryDescription:
      "Kế hoạch sẽ vào đường gửi lệnh duy nhất của bot. Hiện chế độ \"Lệnh thật\" đang ĐÓNG nên máy chủ sẽ từ chối lệnh duyệt — đây là cơ chế an toàn mặc định. Mở cổng phía máy chủ (xem Cấu hình → Cổng thực thi) rồi quay lại duyệt.",
    pair: "Cặp lệnh",
    proposedAt: "Đề xuất lúc",
    liveWarning: "Lệnh thật có thể được gửi lên sàn — kiểm tra kỹ số liệu trước khi duyệt.",
    cancel: "Hủy",
    submitting: "Đang duyệt…",
    confirm: "Xác nhận duyệt",
  },
  rejectDialog: {
    title: (id: string) => `Từ chối kế hoạch ${id}?`,
    description:
      "Không có lệnh nào được gửi lên sàn. Quyết định và lý do được lưu vào lịch sử.",
    reasonLabel: "Lý do (tùy chọn — lưu vào lịch sử)",
    reasonPlaceholder: "Ví dụ: không duyệt mức giá này",
    submitting: "Đang xử lý…",
    confirm: "Từ chối kế hoạch",
  },
  resolved: {
    title: "Đã xử lý gần đây",
    count: (n: number) => `${n} mục — mới giải quyết nhất trước.`,
    emptyHint: "Kế hoạch sau khi duyệt / từ chối sẽ xuất hiện ở đây.",
    empty: "Chưa có kế hoạch nào được xử lý.",
    order: (id: string) => `order ${id}`,
    dryFill: "Khớp thử — chưa gửi lên sàn",
  },
  feedback: {
    dismissError: "Đóng thông báo lỗi",
    dismiss: "Đóng thông báo",
    warningLabel: "CẢNH BÁO:",
    warningTail: (id: string) =>
      `Tuyệt đối không duyệt lại kế hoạch ${id} — lệnh có thể đã tồn tại trên sàn.`,
    approved: (id: string) => `Đã duyệt ${id}.`,
    orderId: (id: string) => `Mã lệnh trên sàn: ${id}.`,
    execMode: (live: boolean) => `Chế độ thực thi: ${live ? "LIVE" : "mô phỏng (dry)"}.`,
    auditGranted: "Đã ghi nhận hành động duyệt vào lịch sử.",
    rejected: (id: string) =>
      `Đã từ chối ${id} — không có lệnh nào được gửi, lý do đã ghi vào audit.`,
  },
  queue: {
    title: "Hàng đợi chờ duyệt",
    description:
      "Lệnh trong hàng đợi do vòng quét hằng ngày của bot tạo ra — bạn không cần gọi gì thêm. Duyệt là bước duy nhất đưa lệnh vào thực thi.",
    updated: (time: string) => `Cập nhật ${time}`,
    loading: "Đang tải hàng đợi phê duyệt…",
    loadFailed: "Không tải được hàng đợi phê duyệt",
    backendHint:
      "Máy chủ có thể chưa chạy. Khởi động máy chủ bằng lệnh sau trong thư mục dự án rồi bấm Thử lại:",
    backendCommandLabel: "Lệnh khởi động máy chủ:",
    emptyTitle: "Chưa có kế hoạch lệnh nào chờ duyệt",
    emptyBody:
      "Kế hoạch lệnh chỉ được tạo khi vòng quét chạy và phát hiện coin có tín hiệu giao dịch. Hãy mở trang Hoạt động bot và bấm Quét ngay (hoặc đợi vòng tự động), rồi quay lại đây để duyệt.",
    openScanner: "Mở Hoạt động bot",
    paused: "Đang tạm dừng làm mới tự động khi có hộp thoại hành động mở.",
  },
};

export const en: typeof vi = {
  status: {
    pending: "Pending",
    approved: "Approved",
    rejected: "Rejected",
    executed: "Executed",
    execute_failed: "Execution failed",
    expired: "Expired",
  },
  sideBuy: "BUY",
  sideSell: "SELL",
  sideUnknown: "Unknown side",
  signal: (signal) => `Signal: ${signal}`,
  planLive: "Planned in LIVE mode",
  planDry: "Planned in simulation mode",
  mockPlan: "Simulated plan (mock)",
  expiredBadge: "Approval window expired",
  errors: {
    mode_closed:
      "Auto Spot is disabled or this approval is no longer authorized. Enable Auto Spot in Settings, then try again.",
    gate_closed:
      "The standalone venue profile is unavailable for this operation. Check the venue URL and credentials, then try again.",
    risk_halted: (reason) =>
      `The bot has halted trading for today — approval is not possible.${reason ? ` Reason: ${reason}` : ""}`,
    conflict:
      "This plan was already handled elsewhere (another tab approved/rejected it). The list will refresh.",
    mock_not_executable:
      "This plan comes from a simulation (mock) scan job — synthetic data can never become a real order. Reject the plan instead of approving it.",
    expired: "The plan's approval window (24 hours) has passed and it is no longer valid.",
    audit_log_unreadable:
      "The execution records are unreadable — the system deliberately refuses approvals without them (a safety design). Check the log-file read permission on the server.",
    csrf_rejected: "Request blocked by CSRF protection — open the control panel via localhost and retry.",
    unsupported_media_type: "Technical error: the request is missing Content-Type application/json.",
    generic: (code, message) => `Error (${code}): ${message}`,
    unknown: (code) => `Unknown error (${code}).`,
    badShape: "Response does not match the contract (missing items).",
    network: "Cannot reach the server — check that the TradingAgents app is running.",
    healthMissingHalted: "/api/health response is missing halted_today"
  },
  gate: {
    haltedTitle: "The bot has halted trading for today",
    haltedBody:
      "Every approval today will be refused by the server. Only the server side can lift the halt — this screen intentionally has no reset button.",
    healthError:
      "Could not check today's trading-halt status — press Refresh.",
  },
  card: {
    proposedAt: (time) => `Proposed at ${time}`,
    deadline: (time) => `Approve by ${time}`,
    quantity: "Quantity",
    estPrice: "Est. price",
    orderValue: "Order value",
    market: "Market",
    leverage: (x) => ` · leverage ${x}×`,
    stopLoss: "Stop loss (SL)",
    takeProfit: "Take profit (TP)",
    reason: (text) => `Reason: ${text}`,
    reasonPrefix: "Reason: ",
    reasonExpand: "Show full reason",
    reasonCollapse: "Collapse",
    reasonUnreadable: "Execution error — technical detail unreadable",
    mockNote:
      "This plan comes from a simulation scan job — synthetic data can never become a real order: the server will refuse approval (409); the plan can only be rejected.",
    approve: "Approve order",
    reject: "Reject",
  },
  approveDialog: {
    title: (id) => `Approve order ${id}?`,
    liveDescription:
      "\"Real orders\" mode is on — the plan enters the bot's single order-send path. A real order may reach the exchange; this action cannot be undone.",
    dryDescription:
      "The plan enters the bot's single order-send path. \"Real orders\" mode is currently CLOSED, so the server will refuse approvals — this is the default safety design. Open the gate server-side (see Settings → Execution gates), then come back to approve.",
    pair: "Pair",
    proposedAt: "Proposed at",
    liveWarning: "A real order may be sent to the exchange — double-check the numbers before approving.",
    cancel: "Cancel",
    submitting: "Approving…",
    confirm: "Confirm approval",
  },
  rejectDialog: {
    title: (id) => `Reject plan ${id}?`,
    description:
      "No order is sent to the exchange. The decision and reason are saved to the history.",
    reasonLabel: "Reason (optional — saved to the history)",
    reasonPlaceholder: "E.g. not approving at this price",
    submitting: "Processing…",
    confirm: "Reject plan",
  },
  resolved: {
    title: "Recently resolved",
    count: (n) => `${n} ${n === 1 ? "item" : "items"} — most recently resolved first.`,
    emptyHint: "Plans appear here after being approved / rejected.",
    empty: "No plans resolved yet.",
    order: (id) => `order ${id}`,
    dryFill: "Simulated fill — not sent to the exchange",
  },
  feedback: {
    dismissError: "Dismiss error",
    dismiss: "Dismiss",
    warningLabel: "WARNING:",
    warningTail: (id) =>
      `Do NOT approve plan ${id} again — the order may already exist on the exchange.`,
    approved: (id) => `Approved ${id}.`,
    orderId: (id) => `Exchange order ID: ${id}.`,
    execMode: (live) => `Execution mode: ${live ? "LIVE" : "simulated (dry)"}.`,
    auditGranted: "The approval action was recorded in the history.",
    rejected: (id) => `Rejected ${id} — no order was sent; the reason is recorded in the audit log.`,
  },
  queue: {
    title: "Approval queue",
    description:
      "Queue orders are produced by the bot's daily scan — you don't need to trigger anything. Approving is the only step that sends an order to execution.",
    updated: (time) => `Updated ${time}`,
    loading: "Loading approval queue…",
    loadFailed: "Could not load the approval queue",
    backendHint:
      "The server may not be running. Start it with the command below from the project directory, then press Retry:",
    backendCommandLabel: "Server start command:",
    emptyTitle: "No order plans awaiting approval",
    emptyBody:
      "Order plans are only created when a scan runs and finds coins with a trading signal. Open Bot activity and press \"Scan now\" (or wait for the automatic loop), then come back here to approve.",
    openScanner: "Open Bot activity",
    paused: "Auto-refresh is paused while an action dialog is open.",
  },
};
