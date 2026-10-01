// Nhãn & badge dùng chung trong màn Duyệt lệnh. Màu không bao giờ là tín hiệu
// duy nhất (WCAG 1.4.1): side/signal luôn kèm chữ, số tiền luôn kèm dấu +/−.

import { Badge } from "@/components/ui/badge";
import { cn } from "cn";

import type { ApprovalStatus } from "./types";

export const STATUS_LABEL: Record<ApprovalStatus, string> = {
  pending: "Chờ duyệt",
  approved: "Đã duyệt",
  rejected: "Đã từ chối",
  executed: "Đã thực thi",
  execute_failed: "Thực thi lỗi",
  expired: "Hết hạn",
};

const STATUS_CLASS: Record<ApprovalStatus, string> = {
  pending: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100",
  approved: "border-transparent bg-primary text-primary-foreground",
  rejected: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  executed: "border-transparent bg-primary text-primary-foreground",
  execute_failed: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  expired: "border-border text-muted-foreground",
};

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  return (
    <Badge variant="outline" className={cn(STATUS_CLASS[status])}>
      {STATUS_LABEL[status]}
    </Badge>
  );
}

/** Chip phía lệnh: MUA (màu dương) / BÁN (màu âm) / phía lạ → hiện nguyên văn. */
export function SideChip({ side }: { side: string | null }) {
  if (side === "buy") {
    return <Badge className="bg-profit/15 text-profit">MUA</Badge>;
  }
  if (side === "sell") {
    return <Badge className="bg-loss/15 text-loss">BÁN</Badge>;
  }
  return <Badge variant="outline">{side ?? "Không rõ phía"}</Badge>;
}

/** Badge tín hiệu engine (giữ nguyên giá trị gốc: Buy/Hold/Underweight/REVIEW…). */
export function SignalBadge({ signal }: { signal: string | null }) {
  if (!signal) return null;
  const tone =
    signal === "Buy"
      ? "text-profit"
      : signal === "Sell"
        ? "text-loss"
        : undefined;
  return (
    <Badge variant="outline" className={tone}>
      Tín hiệu: {signal}
    </Badge>
  );
}

/** Badge chế độ khi lập kế hoạch (mode_at_plan từ contract §4). */
export function PlanModeBadge({ mode }: { mode: string | null }) {
  if (mode === "live") {
    return (
      <Badge variant="outline" className="text-loss">
        Lập kế hoạch ở chế độ LIVE
      </Badge>
    );
  }
  if (mode === "dry") {
    return <Badge variant="secondary">Lập kế hoạch ở chế độ mô phỏng</Badge>;
  }
  return null;
}

/** Kế hoạch demo từ job mock (contract §4 rev 3) — không bao giờ thành lệnh thật. */
export function MockPlanBadge() {
  return (
    <Badge variant="outline" className="border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
      Kế hoạch mô phỏng (mock)
    </Badge>
  );
}

/** Badge suy ra (contract §4): item còn pending nhưng expires_at đã qua —
 * server không lưu trạng thái "expired", nó trả 410 khi cố resolve. */
export function ExpiredBadge() {
  return (
    <Badge variant="outline" className="border-border text-muted-foreground">
      Quá hạn duyệt
    </Badge>
  );
}
