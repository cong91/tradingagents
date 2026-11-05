// Nhãn & badge dùng chung trong màn Duyệt lệnh. Màu không bao giờ là tín hiệu
// duy nhất (WCAG 1.4.1): side/signal luôn kèm chữ, số tiền luôn kèm dấu +/−.

import { Badge } from "@/components/ui/badge";
import { cn } from "cn";

import { useT } from "@/lib/i18n";

import type { ApprovalStatus } from "./types";

const STATUS_CLASS: Record<ApprovalStatus, string> = {
  pending: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100",
  approved: "border-transparent bg-primary text-primary-foreground",
  rejected: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  executed: "border-transparent bg-primary text-primary-foreground",
  execute_failed: "bg-destructive/10 text-destructive dark:bg-destructive/20",
  expired: "border-border text-muted-foreground",
};

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  const t = useT();
  return (
    <Badge variant="outline" className={cn(STATUS_CLASS[status])}>
      {t.approvals.status[status] ?? status}
    </Badge>
  );
}

/** Chip phía lệnh: MUA (màu dương) / BÁN (màu âm) / phía lạ → hiện nguyên văn. */
export function SideChip({ side }: { side: string | null }) {
  const t = useT();
  if (side === "buy") {
    return <Badge className="bg-profit/15 text-profit">{t.approvals.sideBuy}</Badge>;
  }
  if (side === "sell") {
    return <Badge className="bg-loss/15 text-loss">{t.approvals.sideSell}</Badge>;
  }
  return <Badge variant="outline">{side ?? t.approvals.sideUnknown}</Badge>;
}

/** Badge tín hiệu engine (giữ nguyên giá trị gốc: Buy/Hold/Underweight/REVIEW…). */
export function SignalBadge({ signal }: { signal: string | null }) {
  const t = useT();
  if (!signal) return null;
  const tone =
    signal === "Buy"
      ? "text-profit"
      : signal === "Sell"
        ? "text-loss"
        : undefined;
  return (
    <Badge variant="outline" className={tone}>
      {t.approvals.signal(signal)}
    </Badge>
  );
}

/** Badge chế độ khi lập kế hoạch (mode_at_plan từ contract §4). */
export function PlanModeBadge({ mode }: { mode: string | null }) {
  const t = useT();
  if (mode === "live") {
    return (
      <Badge variant="outline" className="text-loss">
        {t.approvals.planLive}
      </Badge>
    );
  }
  if (mode === "dry") {
    return <Badge variant="secondary">{t.approvals.planDry}</Badge>;
  }
  return null;
}

/** Kế hoạch demo từ job mock (contract §4 rev 3) — không bao giờ thành lệnh thật. */
export function MockPlanBadge() {
  const t = useT();
  return (
    <Badge variant="outline" className="border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100">
      {t.approvals.mockPlan}
    </Badge>
  );
}

/** Badge suy ra (contract §4): item còn pending nhưng expires_at đã qua —
 * server không lưu trạng thái "expired", nó trả 410 khi cố resolve. */
export function ExpiredBadge() {
  const t = useT();
  return (
    <Badge variant="outline" className="border-border text-muted-foreground">
      {t.approvals.expiredBadge}

    </Badge>
  );
}
