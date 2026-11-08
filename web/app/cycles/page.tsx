import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { PageHeader } from "@/components/page-header";
import { PipelineScreen } from "@/components/pipeline/pipeline-screen";
import { ScannerScreen } from "@/components/scanner/scanner-screen";
import { FuturesAutoloopPanel } from "@/components/scanner/futures-autoloop-panel";

export const metadata: Metadata = { title: messages.en.pages.cycles.title };

// Trang gộp: chạy phân tích → quét & danh sách theo dõi → vòng tự động/phễu
// (chu kỳ gần nhất nằm trong FuturesAutoloopPanel).
export default function CyclesPage() {
  return (
    <div className="space-y-8">
      <PageHeader page="cycles" />
      <PipelineScreen />
      <ScannerScreen />
      <FuturesAutoloopPanel />
    </div>
  );
}
