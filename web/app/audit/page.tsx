import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { PageHeader } from "@/components/page-header";
import { AuditLogCard } from "@/components/audit/audit-log";
import { DecisionHistoryCard } from "@/components/audit/decision-history";
import { BacktestRunsCard } from "@/components/audit/backtest-runs";

export const metadata: Metadata = { title: messages.en.pages.audit.title };

export default function AuditPage() {
  return (
    <div className="space-y-4">
      <PageHeader page="audit" />
      <AuditLogCard />
      <DecisionHistoryCard />
      <BacktestRunsCard />
    </div>
  );
}
