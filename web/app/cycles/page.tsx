import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { FuturesAutoloopPanel } from "@/components/scanner/futures-autoloop-panel";
import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: messages.en.pages.cycles.title };

export default function CyclesPage() {
  return (
    <div>
      <PageHeader page="cycles" />
      <FuturesAutoloopPanel />
    </div>
  );
}
