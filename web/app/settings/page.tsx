import type { Metadata } from "next";
import { SettingsScreen } from "@/components/settings/settings-screen";

export const metadata: Metadata = { title: "Cấu hình" };

// Đọc/ghi cài đặt qua GET/PUT /api/settings (docs/ui-api-contract.md §1).
// Trang server component chỉ giữ metadata; toàn bộ state máy khách nằm ở SettingsScreen.
export default function SettingsPage() {
  return <SettingsScreen />;
}
