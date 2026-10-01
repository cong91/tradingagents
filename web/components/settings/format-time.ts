// Định dạng thời gian hiển thị (vi-VN). Hợp đồng §0: ISO-8601 UTC ("Z").
// Primitive dùng chung ở lib/format.ts.

import { formatClock, formatDateTime } from "@/lib/format";

export function formatTimeVi(iso: string): string {
  return formatClock(iso);
}

export function formatDateTimeVi(iso: string): string {
  return formatDateTime(iso);
}
