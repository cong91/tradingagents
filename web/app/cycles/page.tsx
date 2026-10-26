import type { Metadata } from "next";
import { FuturesAutoloopPanel } from "@/components/scanner/futures-autoloop-panel";
import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: "Chu kỳ quét" };

export default function CyclesPage() {
  return (
    <div>
      <PageHeader
        title="Chu kỳ quét"
        description="Phễu kết quả mọi chu kỳ autoloop và lý do từng mã bị loại hoặc được triển khai — click một mã để xem chi tiết từng chu kỳ."
      />
      <FuturesAutoloopPanel />
    </div>
  );
}
