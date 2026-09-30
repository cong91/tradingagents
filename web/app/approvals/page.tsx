import type { Metadata } from "next";

import { ApprovalsScreen } from "@/components/approvals/approvals-screen";
import { PageHeader } from "@/components/page-header";

export const metadata: Metadata = { title: "Duyệt lệnh" };

export default function ApprovalsPage() {
  return (
    <div>
      <PageHeader
        title="Duyệt lệnh"
        description="Hàng đợi phê duyệt kế hoạch lệnh từ pipeline hằng ngày. Duyệt là con đường duy nhất đưa lệnh vào thực thi; từ chối không đụng sàn."
      />
      <ApprovalsScreen />
    </div>
  );
}
