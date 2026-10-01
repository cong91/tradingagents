// Bản dịch thông báo lỗi của hành động duyệt/từ chối sang tiếng Việt.
// Mã lỗi khớp docs/ui-api-contract.md §4 và server/approvals.py.

export type ActionError = {
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export function describeActionError(error: ActionError): string {
  switch (error.code) {
    case "gate_closed":
      // server/approvals.py:150-152 — chính sách bản dựng này: duyệt chỉ được
      // khi cổng FR5 (exec_live) là True trong config; cổng không chỉnh được qua UI.
      return "Cổng FR5 (exec_live) đang đóng — server từ chối phê duyệt. Operator phải đặt exec_live=True trong .env/môi trường (không chỉnh được qua màn Cấu hình) rồi thử lại.";
    case "risk_halted": {
      const reason =
        typeof error.details.reason === "string" ? error.details.reason : null;
      return `RiskGuard đã dừng giao dịch hôm nay — không thể duyệt.${
        reason ? ` Lý do: ${reason}` : ""
      }`;
    }
    case "conflict":
      return "Kế hoạch đã được xử lý nơi khác (tab khác đã duyệt/từ chối). Danh sách sẽ được làm mới.";
    case "mock_not_executable":
      return "Kế hoạch sinh từ job quét chế độ mô phỏng (mock) — dữ liệu tổng hợp không thể thành lệnh thật. Hãy từ chối kế hoạch này thay vì duyệt.";
    case "expired":
      return "Kế hoạch đã quá hạn duyệt (24 giờ) và không còn hiệu lực.";
    case "audit_log_unreadable":
      return "Audit log không đọc được — server từ chối fail-closed. Kiểm tra quyền đọc file audit của engine.";
    case "csrf_rejected":
      return "Yêu cầu bị chặn CSRF — hãy mở bảng điều khiển qua localhost rồi thử lại.";
    case "unsupported_media_type":
      return "Lỗi kỹ thuật: yêu cầu thiếu Content-Type application/json.";
    case "network":
      return error.message;
    default:
      return error.message
        ? `Lỗi (${error.code}): ${error.message}`
        : `Lỗi không xác định (${error.code}).`;
  }
}
