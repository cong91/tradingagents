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
    gate_closed:
      "Cổng FR5 (exec_live) đang đóng — server từ chối phê duyệt. Operator phải đặt exec_live=True trong .env/môi trường (không chỉnh được qua màn Cấu hình) rồi thử lại.",
    risk_halted: (reason: string | null) =>
      `RiskGuard đã dừng giao dịch hôm nay — không thể duyệt.${reason ? ` Lý do: ${reason}` : ""}`,
    conflict:
      "Kế hoạch đã được xử lý nơi khác (tab khác đã duyệt/từ chối). Danh sách sẽ được làm mới.",
    mock_not_executable:
      "Kế hoạch sinh từ job quét chế độ mô phỏng (mock) — dữ liệu tổng hợp không thể thành lệnh thật. Hãy từ chối kế hoạch này thay vì duyệt.",
    expired: "Kế hoạch đã quá hạn duyệt (24 giờ) và không còn hiệu lực.",
    audit_log_unreadable:
      "Audit log không đọc được — server từ chối fail-closed. Kiểm tra quyền đọc file audit của engine.",
    csrf_rejected: "Yêu cầu bị chặn CSRF — hãy mở bảng điều khiển qua localhost rồi thử lại.",
    unsupported_media_type: "Lỗi kỹ thuật: yêu cầu thiếu Content-Type application/json.",
    generic: (code: string, message: string) => `Lỗi (${code}): ${message}`,
    unknown: (code: string) => `Lỗi không xác định (${code}).`,
    badShape: "Phản hồi không đúng định dạng hợp đồng (thiếu items).",
    network: "Không kết nối được backend — kiểm tra server FastAPI đang chạy.",
    healthMissingHalted: "Phản hồi /api/health thiếu halted_today",
  },
  gate: {
    haltedTitle: "RiskGuard đã dừng giao dịch hôm nay",
    haltedBody:
      "Mọi phê duyệt sẽ bị server từ chối (403 risk_halted). Việc mở lại halt chỉ do operator thực hiện phía server — màn này cố ý không có nút reset.",
    healthError:
      "Không đọc được GET /api/health — chưa kiểm tra được trạng thái halt của RiskGuard.",
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
    mockNote:
      "Kế hoạch này sinh từ job quét chế độ mô phỏng — dữ liệu tổng hợp không thể thành lệnh thật: server sẽ từ chối duyệt (409), chỉ có thể từ chối kế hoạch.",
    approve: "Duyệt lệnh",
    reject: "Từ chối",
  },
  approveDialog: {
    title: (id: string) => `Xác nhận duyệt lệnh ${id}?`,
    liveDescription:
      "Cổng thực thi đang LIVE — kế hoạch sẽ đi vào đường thực thi duy nhất của hệ thống. Hành động này không thể hoàn tác.",
    dryDescription:
      "Kế hoạch sẽ đi vào đường thực thi duy nhất của hệ thống. Theo chính sách server hiện tại, duyệt chỉ được chấp nhận khi cổng exec_live=True; nếu cổng đóng, server sẽ từ chối (412 gate_closed).",
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
      "Không có lệnh nào được gửi lên sàn. Quyết định và lý do được ghi vào audit (approval_denied).",
    reasonLabel: "Lý do (tùy chọn — được ghi vào audit)",
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
    dryFill: "dry-fill (chưa gửi sàn)",
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
    auditGranted: "Đã ghi audit approval_granted.",
    rejected: (id: string) =>
      `Đã từ chối ${id} — không có lệnh nào được gửi, lý do đã ghi vào audit.`,
  },
  queue: {
    title: "Hàng đợi chờ duyệt",
    description:
      "Nguồn duy nhất của hàng đợi là pipeline hằng ngày (POST /api/daily). Duyệt là đường duy nhất đưa lệnh vào thực thi.",
    updated: (time: string) => `Cập nhật ${time}`,
    loading: "Đang tải hàng đợi phê duyệt…",
    loadFailed: "Không tải được hàng đợi phê duyệt",
    backendHint: "Backend FastAPI có thể chưa chạy. Khởi động từ thư mục dự án:",
    emptyTitle: "Chưa có kế hoạch lệnh nào chờ duyệt",
    emptyBody:
      "Kế hoạch lệnh chỉ được tạo khi pipeline hằng ngày chạy và phát hiện coin có tín hiệu giao dịch. Hãy chạy pipeline từ màn M3 — Tìm cặp giao dịch, rồi quay lại đây để duyệt.",
    openScanner: "Mở màn Tìm cặp giao dịch",
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
    gate_closed:
      "The FR5 gate (exec_live) is closed — the server refused the approval. An operator must set exec_live=True in .env/environment (not editable from Settings), then retry.",
    risk_halted: (reason) =>
      `RiskGuard has halted trading for today — approval is not possible.${reason ? ` Reason: ${reason}` : ""}`,
    conflict:
      "This plan was already handled elsewhere (another tab approved/rejected it). The list will refresh.",
    mock_not_executable:
      "This plan comes from a simulation (mock) scan job — synthetic data can never become a real order. Reject the plan instead of approving it.",
    expired: "The plan's approval window (24 hours) has passed and it is no longer valid.",
    audit_log_unreadable:
      "The audit log is unreadable — the server failed closed. Check the engine's read permission on the audit file.",
    csrf_rejected: "Request blocked by CSRF protection — open the control panel via localhost and retry.",
    unsupported_media_type: "Technical error: the request is missing Content-Type application/json.",
    generic: (code, message) => `Error (${code}): ${message}`,
    unknown: (code) => `Unknown error (${code}).`,
    badShape: "Response does not match the contract (missing items).",
    network: "Cannot reach the backend — check that the FastAPI server is running.",
    healthMissingHalted: "/api/health response is missing halted_today",
  },
  gate: {
    haltedTitle: "RiskGuard has halted trading for today",
    haltedBody:
      "Every approval will be refused by the server (403 risk_halted). Only an operator can lift the halt on the server side — this screen intentionally has no reset button.",
    healthError:
      "Cannot read GET /api/health — RiskGuard halt status could not be checked.",
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
    mockNote:
      "This plan comes from a simulation scan job — synthetic data can never become a real order: the server will refuse approval (409); the plan can only be rejected.",
    approve: "Approve order",
    reject: "Reject",
  },
  approveDialog: {
    title: (id) => `Approve order ${id}?`,
    liveDescription:
      "The execution gate is LIVE — the plan enters the system's only execution path. This action cannot be undone.",
    dryDescription:
      "The plan enters the system's only execution path. Under the current server policy, approval is only accepted when exec_live=True; if the gate is closed the server refuses it (412 gate_closed).",
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
      "No order is sent to the exchange. The decision and reason are recorded in the audit log (approval_denied).",
    reasonLabel: "Reason (optional — recorded in the audit log)",
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
    dryFill: "dry-fill (not sent to exchange)",
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
    auditGranted: "Audit entry approval_granted recorded.",
    rejected: (id) => `Rejected ${id} — no order was sent; the reason is recorded in the audit log.`,
  },
  queue: {
    title: "Approval queue",
    description:
      "The queue is fed only by the daily pipeline (POST /api/daily). Approving is the only path to execution.",
    updated: (time) => `Updated ${time}`,
    loading: "Loading approval queue…",
    loadFailed: "Could not load the approval queue",
    backendHint: "The FastAPI backend may not be running. Start it from the project directory:",
    emptyTitle: "No order plans awaiting approval",
    emptyBody:
      "Order plans are only created when the daily pipeline runs and finds coins with a trading signal. Run the pipeline from screen M3 — Pair scanner, then come back here to approve.",
    openScanner: "Open Pair scanner",
    paused: "Auto-refresh is paused while an action dialog is open.",
  },
};
