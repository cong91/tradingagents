// Bản dịch thông báo lỗi của hành động duyệt/từ chối theo locale đang chọn
// (lib/i18n/messages/approvals.ts).
// Mã lỗi khớp docs/ui-api-contract.md §4 và server/approvals.py.

import { getMessages } from "@/lib/i18n/messages";

export type ActionError = {
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export function describeActionError(error: ActionError): string {
  const m = getMessages().approvals.errors;
  switch (error.code) {
    case "gate_closed":
      // server/approvals.py:150-152 — chính sách bản dựng này: duyệt chỉ được
      // khi cổng FR5 (exec_live) là True trong config; cổng không chỉnh được qua UI.
      return m.gate_closed;
    case "risk_halted": {
      const reason =
        typeof error.details.reason === "string" ? error.details.reason : null;
      return m.risk_halted(reason);
    }
    case "conflict":
      return m.conflict;
    case "mock_not_executable":
      return m.mock_not_executable;
    case "expired":
      return m.expired;
    case "audit_log_unreadable":
      return m.audit_log_unreadable;
    case "csrf_rejected":
      return m.csrf_rejected;
    case "unsupported_media_type":
      return m.unsupported_media_type;
    case "network":
      return error.message;
    default:
      return error.message
        ? m.generic(error.code, error.message)
        : m.unknown(error.code);
  }
}
