import type { Metadata } from "next";
import { PageHeader } from "@/components/page-header";
import { ScannerScreen } from "@/components/scanner/scanner-screen";

export const metadata: Metadata = { title: "Tìm cặp giao dịch" };

export default function ScannerPage() {
  return (
    <div>
      <PageHeader
        title="Tìm cặp giao dịch"
        description="Quản lý danh sách theo dõi (watchlist) và chạy quét pipeline cho từng mã trước khi đưa vào hàng duyệt lệnh."
      />
      <ScannerScreen />
    </div>
  );
}
