import type { Metadata } from "next";
import { PageHeader } from "@/components/page-header";
import { PipelineScreen } from "@/components/pipeline/pipeline-screen";

export const metadata: Metadata = { title: "Agent Pipeline" };

export default function PipelinePage() {
  return (
    <div>
      <PageHeader
        title="Agent Pipeline"
        description="Bắt đầu một phiên phân tích (POST /api/runs) và theo dõi luồng agent theo thời gian thực qua SSE: analyst → tranh luận → trader → rủi ro → quyết định cuối."
      />
      <PipelineScreen />
    </div>
  );
}
