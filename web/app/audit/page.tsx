import type { Metadata } from "next";
import { PageHeader } from "@/components/page-header";
import { AuditLogCard } from "@/components/audit/audit-log";
import { DecisionHistoryCard } from "@/components/audit/decision-history";
import { BacktestRunsCard } from "@/components/audit/backtest-runs";

export const metadata: Metadata = { title: "Lịch sử & Audit" };

export default function AuditPage() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="Lịch sử & Audit"
        description="Nhật ký kiểm soát thực thi, quyết định đã lưu và tổng kết backtest. Màn chỉ đọc — mọi hành động đặt lệnh nằm ở màn Duyệt lệnh."
      />
      <AuditLogCard />
      <DecisionHistoryCard />
      <BacktestRunsCard />
    </div>
  );
}
