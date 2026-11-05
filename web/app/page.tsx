import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { PageHeader } from "@/components/page-header";
import { OverviewDashboard } from "@/components/overview/dashboard";

export const metadata: Metadata = { title: messages.en.pages.overview.title };

export default function OverviewPage() {
  return (
    <div>
      <PageHeader page="overview" />
      <OverviewDashboard />
    </div>
  );
}
