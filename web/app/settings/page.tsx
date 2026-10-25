import type { Metadata } from "next";
import { messages } from "@/lib/i18n/messages";
import { SettingsScreen } from "@/components/settings/settings-screen";

export const metadata: Metadata = { title: messages.en.pages.settings.title };

// Đọc/ghi cài đặt qua GET/PUT /api/settings (docs/ui-api-contract.md §1).
// Trang server component chỉ giữ metadata; toàn bộ state máy khách nằm ở SettingsScreen.
export default function SettingsPage() {
  return <SettingsScreen />;
}
