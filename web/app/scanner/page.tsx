import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { PageHeader } from "@/components/page-header";
import { ScannerScreen } from "@/components/scanner/scanner-screen";

export const metadata: Metadata = { title: messages.en.pages.scanner.title };

export default function ScannerPage() {
  return (
    <div>
      <PageHeader page="scanner" />
      <ScannerScreen />
    </div>
  );
}
