import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";

import { ApprovalsScreen } from "@/components/approvals/approvals-screen";
import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: messages.en.pages.approvals.title };

export default function ApprovalsPage() {
  return (
    <div>
      <PageHeader page="approvals" />
      <ApprovalsScreen />
    </div>
  );
}
