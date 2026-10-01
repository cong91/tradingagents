import type { Metadata } from "next";
import { PageHeader } from "@/components/page-header";
import { OverviewDashboard } from "@/components/overview/dashboard";

export const metadata: Metadata = { title: "Tổng quan lợi nhuận" };

export default function OverviewPage() {
  return (
    <div>
      <PageHeader
        title="Tổng quan lợi nhuận"
        description="Alpha tích luỹ của các quyết định đã chốt, hiệu suất theo thời gian và lệnh thực thi gần đây — đọc trực tiếp từ backend qua /api/*."
      />
      <OverviewDashboard />
    </div>
  );
}
