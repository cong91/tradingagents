"use client";

// Reason backend trả có thể chứa HTML thô (body lỗi 502 của sàn được ccxt nối
// nguyên vào message). Hiển thị qua lib/format.formatReason — dữ liệu API giữ
// nguyên; 1 dòng cắt ~140 ký tự, nút "Xem đầy đủ" mở rộng inline.
// React escape nên không có rủi ro XSS.

import { useState } from "react";

import { formatReason } from "@/lib/format";

const PREVIEW_CHARS = 140;

export function ReasonText({
  text,
  expandLabel,
  collapseLabel,
  unreadableLabel,
  emptyLabel,
  className,
}: {
  text: string | null | undefined;
  expandLabel: string;
  collapseLabel: string;
  /** Có text nhưng strip xong rỗng (chỉ toàn thẻ) — không đọc được. */
  unreadableLabel: string;
  /** Không có text nào (null/"") — hiển thị placeholder, vd "—". */
  emptyLabel?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  if (!text) {
    return emptyLabel ? <span className={className}>{emptyLabel}</span> : null;
  }
  const cleaned = formatReason(text);
  if (!cleaned) {
    return <span className={className}>{unreadableLabel}</span>;
  }
  const truncated = cleaned.length > PREVIEW_CHARS;
  return (
    <span className={className}>
      {truncated && !open ? `${cleaned.slice(0, PREVIEW_CHARS).trimEnd()}…` : cleaned}
      {truncated && (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
          className="ml-1 rounded px-1 text-xs underline underline-offset-2 hover:bg-muted"
        >
          {open ? collapseLabel : expandLabel}
        </button>
      )}
    </span>
  );
}
